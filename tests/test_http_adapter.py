"""HTTP bridge adapter tests: request shape, telemetry mapping, acks, e-stop."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from robotctl.adapters.http import HttpRobotAdapter
from robotctl.config import Settings
from robotctl.schemas import ActionStep


def make_settings(**overrides: object) -> Settings:
    base = {
        "_env_file": None,
        "robot_base_url": "http://robot.test",
        "robot_poll_hz": 50.0,  # fast polling so tests see telemetry quickly
        "step_timeout_s": 2.0,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


class RobotStub:
    """Routes for httpx.MockTransport: records requests, serves canned replies."""

    def __init__(self) -> None:
        self.command_bodies: list[dict] = []
        self.stop_hits = 0
        self.telemetry_payload: dict = {"motion_status": "idle", "battery_pct": 88.0}
        self.command_status: int = 200
        self.command_body: dict = {}
        self.command_delay_s: float = 0.0
        self.fail_stop: bool = False
        self.drop_connection: bool = False

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.drop_connection:
            raise httpx.ConnectError("link down", request=request)
        if request.url.path == "/telemetry":
            return httpx.Response(200, json=self.telemetry_payload)
        if request.url.path == "/command":
            self.command_bodies.append(json.loads(request.content.decode()))
            if self.command_delay_s:
                await asyncio.sleep(self.command_delay_s)
            return httpx.Response(self.command_status, json=self.command_body)
        if request.url.path == "/stop":
            self.stop_hits += 1
            if self.fail_stop:
                return httpx.Response(500)
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)


async def wait_until(predicate, timeout_s: float = 2.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


# -- telemetry ------------------------------------------------------------------


async def test_telemetry_is_polled_and_mapped():
    stub = RobotStub()
    stub.telemetry_payload = {
        "motion_status": "moving",
        "pose": {"x_m": 1.25, "y_m": -0.5, "heading_deg": 45.0},
        "battery_pct": 55.5,
        "obstacle_distance_m": 0.8,
        "linear_speed_mps": 0.3,
        "timestamp": 1234.0,
    }
    adapter = HttpRobotAdapter(make_settings(), transport=httpx.MockTransport(stub))
    await adapter.connect()
    try:
        await wait_until(lambda: adapter.telemetry() is not None)
        snapshot = adapter.telemetry()
        assert snapshot is not None
        assert snapshot.connected is True
        assert snapshot.motion_status == "moving"
        assert snapshot.pose is not None and snapshot.pose.x_m == 1.25
        assert snapshot.battery_pct == 55.5
        assert snapshot.obstacle_distance_m == 0.8
        assert snapshot.linear_speed_mps == 0.3
        assert snapshot.timestamp == 1234.0
    finally:
        await adapter.disconnect()


async def test_unknown_motion_status_maps_to_error():
    stub = RobotStub()
    stub.telemetry_payload = {"motion_status": "warp drive"}
    adapter = HttpRobotAdapter(make_settings(), transport=httpx.MockTransport(stub))
    await adapter.connect()
    try:
        await wait_until(lambda: adapter.telemetry() is not None)
        assert adapter.telemetry() is not None
        assert adapter.telemetry().motion_status == "error"
    finally:
        await adapter.disconnect()


async def test_missing_optional_telemetry_fields_are_none():
    stub = RobotStub()
    stub.telemetry_payload = {"motion_status": "idle"}
    adapter = HttpRobotAdapter(make_settings(), transport=httpx.MockTransport(stub))
    await adapter.connect()
    try:
        await wait_until(lambda: adapter.telemetry() is not None)
        snapshot = adapter.telemetry()
        assert snapshot is not None
        assert snapshot.pose is None
        assert snapshot.battery_pct is None
        assert snapshot.obstacle_distance_m is None
    finally:
        await adapter.disconnect()


async def test_connection_loss_marks_adapter_disconnected():
    stub = RobotStub()
    adapter = HttpRobotAdapter(make_settings(), transport=httpx.MockTransport(stub))
    await adapter.connect()
    try:
        await wait_until(lambda: adapter.telemetry() is not None)
        stub.drop_connection = True
        await wait_until(lambda: adapter.telemetry() is None or not adapter.telemetry().connected)
    finally:
        await adapter.disconnect()


# -- execute -----------------------------------------------------------------------


async def test_execute_posts_the_validated_step_json():
    stub = RobotStub()
    adapter = HttpRobotAdapter(make_settings(), transport=httpx.MockTransport(stub))
    await adapter.connect()
    try:
        await wait_until(lambda: adapter.telemetry() is not None and adapter.telemetry().connected)
        await adapter.execute(
            ActionStep.model_validate({"action": "move", "distance_m": 0.5, "speed_mps": 0.2})
        )
        assert stub.command_bodies == [{"action": "move", "distance_m": 0.5, "speed_mps": 0.2}]
    finally:
        await adapter.disconnect()


async def test_execute_http_error_is_adapter_error():
    stub = RobotStub()
    stub.command_status = 400
    adapter = HttpRobotAdapter(make_settings(), transport=httpx.MockTransport(stub))
    await adapter.connect()
    try:
        await wait_until(lambda: adapter.telemetry() is not None and adapter.telemetry().connected)
        with pytest.raises(Exception, match="HTTP 400"):
            await adapter.execute(ActionStep.model_validate({"action": "stop"}))
    finally:
        await adapter.disconnect()


async def test_execute_failure_ack_is_adapter_error():
    stub = RobotStub()
    stub.command_body = {"ok": False, "error": "motor overheated"}
    adapter = HttpRobotAdapter(make_settings(), transport=httpx.MockTransport(stub))
    await adapter.connect()
    try:
        await wait_until(lambda: adapter.telemetry() is not None and adapter.telemetry().connected)
        with pytest.raises(Exception, match="motor overheated"):
            await adapter.execute(ActionStep.model_validate({"action": "move", "distance_m": 1.0}))
    finally:
        await adapter.disconnect()


async def test_execute_failed_status_is_adapter_error():
    stub = RobotStub()
    stub.command_body = {"status": "failed", "error": "gripper stuck"}
    adapter = HttpRobotAdapter(make_settings(), transport=httpx.MockTransport(stub))
    await adapter.connect()
    try:
        await wait_until(lambda: adapter.telemetry() is not None and adapter.telemetry().connected)
        with pytest.raises(Exception, match="gripper stuck"):
            await adapter.execute(ActionStep.model_validate({"action": "grab", "force": 10}))
    finally:
        await adapter.disconnect()


async def test_execute_without_connection_fails_safe():
    adapter = HttpRobotAdapter(make_settings(), transport=httpx.MockTransport(RobotStub()))
    # not connected: no poll loop has produced telemetry yet
    with pytest.raises(Exception, match="not connected"):
        await adapter.execute(ActionStep.model_validate({"action": "stop"}))


async def test_execute_when_link_drops_is_adapter_error():
    stub = RobotStub()
    adapter = HttpRobotAdapter(make_settings(), transport=httpx.MockTransport(stub))
    await adapter.connect()
    try:
        await wait_until(lambda: adapter.telemetry() is not None and adapter.telemetry().connected)
        stub.drop_connection = True
        with pytest.raises(Exception, match="robot request failed"):
            await adapter.execute(ActionStep.model_validate({"action": "wait", "seconds": 5}))
    finally:
        await adapter.disconnect()


async def test_cancellation_halts_motion_before_propagating():
    stub = RobotStub()
    stub.command_delay_s = 5.0
    adapter = HttpRobotAdapter(make_settings(), transport=httpx.MockTransport(stub))
    await adapter.connect()
    try:
        await wait_until(lambda: adapter.telemetry() is not None and adapter.telemetry().connected)
        task = asyncio.create_task(
            adapter.execute(ActionStep.model_validate({"action": "move", "distance_m": 2.0}))
        )
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stub.stop_hits == 1  # the robot was told to halt
    finally:
        await adapter.disconnect()


# -- emergency stop ------------------------------------------------------------------


async def test_stop_posts_to_the_stop_endpoint_and_never_raises():
    stub = RobotStub()
    adapter = HttpRobotAdapter(make_settings(), transport=httpx.MockTransport(stub))
    await adapter.connect()
    try:
        await adapter.stop()
        assert stub.stop_hits == 1
        stub.fail_stop = True  # robot 500s on stop: still must not raise
        await adapter.stop()
        assert stub.stop_hits == 2
    finally:
        await adapter.disconnect()


async def test_stop_without_configured_path_is_noop():
    stub = RobotStub()
    adapter = HttpRobotAdapter(make_settings(robot_stop_path=""), transport=httpx.MockTransport(stub))
    await adapter.stop()
    assert stub.stop_hits == 0


async def test_adapter_without_base_url_stays_disconnected():
    adapter = HttpRobotAdapter(make_settings(robot_base_url=""))
    await adapter.connect()
    assert adapter.telemetry() is None
    await adapter.disconnect()

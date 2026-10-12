"""HTTP bridge adapter tests: on-demand telemetry, request shape, acks, e-stop."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from robotctl.core import ActionStep, Settings
from robotctl.runtime import AdapterError, HttpRobotAdapter


def make_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "robot_base_url": "http://robot.test",
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


def make_adapter(stub: RobotStub | None = None, **overrides: object) -> HttpRobotAdapter:
    stub = stub if stub is not None else RobotStub()
    return HttpRobotAdapter(make_settings(**overrides), transport=httpx.MockTransport(stub))


# -- telemetry on demand -----------------------------------------------------------


async def test_fetch_telemetry_maps_the_robot_payload():
    stub = RobotStub()
    stub.telemetry_payload = {
        "motion_status": "moving",
        "pose": {"x_m": 1.25, "y_m": -0.5, "heading_deg": 45.0},
        "battery_pct": 55.5,
        "obstacle_distance_m": 0.8,
        "linear_speed_mps": 0.3,
        "timestamp": 1234.0,
    }
    adapter = make_adapter(stub)
    snapshot = await adapter.fetch_telemetry()
    assert snapshot is not None
    assert snapshot.connected is True
    assert snapshot.motion_status == "moving"
    assert snapshot.pose is not None and snapshot.pose.x_m == 1.25
    assert snapshot.battery_pct == 55.5
    assert snapshot.obstacle_distance_m == 0.8
    assert snapshot.linear_speed_mps == 0.3
    assert snapshot.timestamp == 1234.0
    await adapter.disconnect()


async def test_unknown_motion_status_maps_to_error():
    stub = RobotStub()
    stub.telemetry_payload = {"motion_status": "warp drive"}
    adapter = make_adapter(stub)
    snapshot = await adapter.fetch_telemetry()
    assert snapshot is not None and snapshot.motion_status == "error"
    await adapter.disconnect()


async def test_missing_optional_telemetry_fields_are_none():
    stub = RobotStub()
    stub.telemetry_payload = {"motion_status": "idle"}
    adapter = make_adapter(stub)
    snapshot = await adapter.fetch_telemetry()
    assert snapshot is not None
    assert snapshot.pose is None
    assert snapshot.battery_pct is None
    assert snapshot.obstacle_distance_m is None
    await adapter.disconnect()


async def test_unreachable_robot_yields_no_telemetry():
    stub = RobotStub()
    adapter = make_adapter(stub)
    assert await adapter.fetch_telemetry() is not None
    stub.drop_connection = True
    assert await adapter.fetch_telemetry() is None
    await adapter.disconnect()


async def test_adapter_without_base_url_has_no_telemetry():
    adapter = make_adapter(robot_base_url="")
    assert await adapter.fetch_telemetry() is None
    with pytest.raises(AdapterError, match="not configured"):
        await adapter.execute(ActionStep.model_validate({"action": "stop"}))
    await adapter.disconnect()


# -- execute -----------------------------------------------------------------------


async def test_execute_posts_the_validated_step_json():
    stub = RobotStub()
    adapter = make_adapter(stub)
    await adapter.execute(
        ActionStep.model_validate({"action": "move", "distance_m": 0.5, "speed_mps": 0.2})
    )
    assert stub.command_bodies == [{"action": "move", "distance_m": 0.5, "speed_mps": 0.2}]
    await adapter.disconnect()


async def test_execute_http_error_is_adapter_error():
    stub = RobotStub()
    stub.command_status = 400
    adapter = make_adapter(stub)
    with pytest.raises(Exception, match="HTTP 400"):
        await adapter.execute(ActionStep.model_validate({"action": "stop"}))
    await adapter.disconnect()


async def test_execute_failure_ack_is_adapter_error():
    stub = RobotStub()
    stub.command_body = {"ok": False, "error": "motor overheated"}
    adapter = make_adapter(stub)
    with pytest.raises(Exception, match="motor overheated"):
        await adapter.execute(ActionStep.model_validate({"action": "move", "distance_m": 1.0}))
    await adapter.disconnect()


async def test_execute_failed_status_is_adapter_error():
    stub = RobotStub()
    stub.command_body = {"status": "failed", "error": "gripper stuck"}
    adapter = make_adapter(stub)
    with pytest.raises(Exception, match="gripper stuck"):
        await adapter.execute(ActionStep.model_validate({"action": "grab", "force": 10}))
    await adapter.disconnect()


async def test_execute_when_link_drops_is_adapter_error():
    stub = RobotStub()
    adapter = make_adapter(stub)
    stub.drop_connection = True
    with pytest.raises(Exception, match="robot request failed"):
        await adapter.execute(ActionStep.model_validate({"action": "wait", "seconds": 5}))
    await adapter.disconnect()


async def test_cancellation_halts_motion_before_propagating():
    stub = RobotStub()
    stub.command_delay_s = 5.0
    adapter = make_adapter(stub)
    task = asyncio.create_task(
        adapter.execute(ActionStep.model_validate({"action": "move", "distance_m": 2.0}))
    )
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stub.stop_hits == 1  # the robot was told to halt
    await adapter.disconnect()


# -- emergency stop ------------------------------------------------------------------


async def test_stop_posts_to_the_stop_endpoint_and_never_raises():
    stub = RobotStub()
    adapter = make_adapter(stub)
    await adapter.stop()
    assert stub.stop_hits == 1
    stub.fail_stop = True  # robot 500s on stop: still must not raise
    await adapter.stop()
    assert stub.stop_hits == 2
    await adapter.disconnect()


async def test_stop_without_configured_path_is_noop():
    stub = RobotStub()
    adapter = make_adapter(stub, robot_stop_path="")
    await adapter.stop()
    assert stub.stop_hits == 0
    await adapter.disconnect()


async def test_disconnect_closes_and_stops():
    stub = RobotStub()
    adapter = make_adapter(stub)
    await adapter.disconnect()
    assert stub.stop_hits == 1  # disconnect halts motion best-effort
    assert await adapter.fetch_telemetry() is None  # client is closed

"""Generic HTTP bridge adapter — the universal way to reach a real robot.

The robot (its firmware or companion app) exposes three HTTP endpoints:

- ``GET  {ROBOT_TELEMETRY_PATH}`` → JSON telemetry snapshot (polled)
- ``POST {ROBOT_COMMAND_PATH}``   ← one validated action JSON; responds when
  the action finished (2xx) or failed (4xx/5xx, or 2xx with ``ok: false`` /
  ``status: "failed"|"error"``)
- ``POST {ROBOT_STOP_PATH}``      ← emergency halt (best effort)

The command payload is the validated step JSON, e.g.
``{"action": "move", "distance_m": 0.5, "speed_mps": 0.2}`` — exactly the
format the uploaded robot profile teaches the LLM.

Telemetry mapping: ``motion_status`` is required (one of idle/moving/
turning/stopped/error; unknown values map to ``error``), everything else
(``pose``, ``battery_pct``, ``obstacle_distance_m``, speeds,
``last_action_id``) is optional and omitted when absent.
"""

from __future__ import annotations

import asyncio
import time

import httpx

from robotctl.adapters.base import AdapterError, RobotAdapter
from robotctl.config import Settings
from robotctl.schemas import ActionStep, MotionStatus, Pose, Telemetry

_STOP_TIMEOUT_S = 2.0
# Consecutive failed telemetry polls before the adapter reports a disconnect;
# brief hiccups must not abort a running plan, real outages must.
_POLL_FAILURE_TOLERANCE = 3


class HttpRobotAdapter(RobotAdapter):
    """Talks to a robot's HTTP endpoints; caches polled telemetry."""

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._settings = settings
        self._base_url = settings.robot_base_url.strip().rstrip("/")
        self._client: httpx.AsyncClient | None = (
            httpx.AsyncClient(
                base_url=self._base_url, timeout=settings.robot_timeout_s, transport=transport
            )
            if self._base_url
            else None
        )
        self._telemetry: Telemetry | None = None
        self._connected = False
        self._poll_failures = 0
        self._poll_task: asyncio.Task[None] | None = None

    # -- lifecycle -------------------------------------------------------------

    async def connect(self) -> None:
        if self._client is None:
            return  # no robot configured: stay disconnected, report via /health
        if self._poll_task is None:
            self._poll_task = asyncio.create_task(self._poll_loop())

    async def disconnect(self) -> None:
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
        await self.stop()
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._connected = False

    # -- polling -----------------------------------------------------------------

    async def _poll_loop(self) -> None:
        period = 1.0 / max(self._settings.robot_poll_hz, 0.1)
        while True:
            await self._poll_once()
            await asyncio.sleep(period)

    async def _poll_once(self) -> None:
        assert self._client is not None
        try:
            response = await self._client.get(self._settings.robot_telemetry_path)
            response.raise_for_status()
            snapshot = _map_telemetry(response.json())
        except Exception:
            self._poll_failures += 1
            if self._poll_failures >= _POLL_FAILURE_TOLERANCE:
                self._connected = False
                if self._telemetry is not None:
                    # keep the last-known values but mark the link dead
                    self._telemetry = self._telemetry.model_copy(
                        update={"connected": False, "motion_status": "error"}
                    )
            return
        self._poll_failures = 0
        self._telemetry = snapshot
        self._connected = True

    # -- RobotAdapter API ----------------------------------------------------------

    def telemetry(self) -> Telemetry | None:
        return self._telemetry

    async def execute(self, action: ActionStep) -> None:
        if self._client is None or not self._connected:
            raise AdapterError("robot not connected (check ROBOT_BASE_URL and /health)")
        payload = {k: v for k, v in action.model_dump().items() if v is not None}
        try:
            response = await self._client.post(self._settings.robot_command_path, json=payload)
        except asyncio.CancelledError:
            await self.stop()  # halt motion before propagating the e-stop
            raise
        except httpx.TimeoutException as exc:
            raise AdapterError(
                f"robot did not finish the action within {self._settings.robot_timeout_s:g} s"
            ) from exc
        except httpx.HTTPError as exc:
            raise AdapterError(f"robot request failed: {exc}") from exc
        if response.status_code >= 400:
            raise AdapterError(f"robot rejected the action: HTTP {response.status_code}")
        _check_ack(response)

    async def stop(self) -> None:
        """Best-effort emergency halt via POST {ROBOT_STOP_PATH}; never raises."""
        if self._client is None or not self._settings.robot_stop_path.strip():
            return
        try:
            await asyncio.wait_for(
                self._client.post(self._settings.robot_stop_path), timeout=_STOP_TIMEOUT_S
            )
        except Exception:
            pass  # stop must never raise


def _check_ack(response: httpx.Response) -> None:
    """Treat an explicit failure in a 2xx acknowledgement body as an error."""
    try:
        body = response.json()
    except ValueError:
        return  # empty/non-JSON ack counts as success
    if not isinstance(body, dict):
        return
    if body.get("ok") is False:
        raise AdapterError(f"robot reported failure: {body.get('error', body)}")
    if body.get("status") in ("failed", "error"):
        raise AdapterError(f"robot reported failure: {body.get('error', body)}")


def _map_telemetry(data: object) -> Telemetry:
    """Map a robot telemetry JSON object onto the universal schema."""
    if not isinstance(data, dict):
        raise ValueError("telemetry payload is not a JSON object")
    status = data.get("motion_status")
    if status not in MotionStatus.__args__:  # type: ignore[attr-defined]
        status = "error"
    pose: Pose | None = None
    raw_pose = data.get("pose")
    if isinstance(raw_pose, dict):
        try:
            pose = Pose.model_validate(raw_pose)
        except Exception:
            pose = None
    battery = data.get("battery_pct")
    if isinstance(battery, (int, float)) and not isinstance(battery, bool):
        battery = max(0.0, min(float(battery), 100.0))
    else:
        battery = None

    def _number(key: str) -> float | None:
        value = data.get(key)
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    timestamp = _number("timestamp")
    return Telemetry(
        timestamp=timestamp if timestamp is not None else time.time(),
        monotonic=time.monotonic(),
        connected=bool(data.get("connected", True)),
        motion_status=status,
        pose=pose,
        linear_speed_mps=_number("linear_speed_mps"),
        angular_speed_dps=_number("angular_speed_dps"),
        battery_pct=battery,
        obstacle_distance_m=_number("obstacle_distance_m"),
        last_action_id=(
            data["last_action_id"] if isinstance(data.get("last_action_id"), str) else None
        ),
    )

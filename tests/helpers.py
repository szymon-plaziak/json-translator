"""Shared test helpers: robot profile, fake adapter/planner, stack assembly."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from robotctl.core import ActionStep, PlanResponse, Pose, ProfileManager, RobotProfile, Telemetry, parse_profile
from robotctl.runtime import AdapterError, Executor, PlannerError

if TYPE_CHECKING:
    from robotctl.core import Settings
    from robotctl.runtime import Command


# ---------------------------------------------------------------------------
# Robot profile factory — the stand-in for an uploaded profile.
# ---------------------------------------------------------------------------


def make_profile() -> RobotProfile:
    """A small universal test profile: move / turn / wait with one example."""
    return parse_profile(
        {
            "profile_version": 1,
            "robot": {"name": "Test Rover", "description": "two-wheeled test robot"},
            "actions": [
                {
                    "action": "move",
                    "description": "drive forward or backward",
                    "params": [
                        {"name": "distance_m", "type": "float", "unit": "m", "min": 0, "max": 10, "required": True},
                        {"name": "speed_mps", "type": "float", "unit": "m/s", "min": 0, "max": 2, "default": 0.5},
                    ],
                },
                {
                    "action": "turn",
                    "description": "rotate in place",
                    "params": [
                        {"name": "angle_deg", "type": "float", "unit": "deg", "min": -180, "max": 180, "required": True},
                        {"name": "speed_dps", "type": "float", "unit": "deg/s", "min": 0, "max": 180, "default": 90},
                    ],
                },
                {
                    "action": "wait",
                    "description": "pause",
                    "params": [
                        {"name": "seconds", "type": "float", "unit": "s", "min": 0, "max": 30, "required": True},
                    ],
                },
            ],
            "examples": [
                {
                    "version": 1,
                    "mode": "plan",
                    "steps": [
                        {"action": "turn", "angle_deg": 90},
                        {"action": "move", "distance_m": 0.5},
                    ],
                }
            ],
        }
    )


def make_profiles(profile: RobotProfile | None = None) -> ProfileManager:
    """In-memory profile manager preloaded with the test profile."""
    manager = ProfileManager(path="")
    manager.set(profile if profile is not None else make_profile())
    return manager


def step(action: str, **params: object) -> ActionStep:
    return ActionStep.model_validate({"action": action, **params})


def action_plan(*steps: object) -> PlanResponse:
    # dicts are coerced to ActionStep by pydantic
    return PlanResponse(version=1, mode="action", steps=list(steps))  # type: ignore[arg-type]


def multi_plan(*steps: object) -> PlanResponse:
    return PlanResponse(version=1, mode="plan", steps=list(steps))  # type: ignore[arg-type]


class FakePlanner:
    """Plan-provider double: returns a canned plan (or raises PlannerError)."""

    def __init__(self, plan: PlanResponse | None = None, error: str | None = None) -> None:
        self.canned = plan
        self.error = error
        self.calls: list[str] = []

    async def plan(self, command: str) -> PlanResponse:
        self.calls.append(command)
        if self.error is not None:
            raise PlannerError(self.error)
        assert self.canned is not None, "FakePlanner needs a plan or an error"
        return self.canned


class FakeAdapter:
    """Robot-adapter double: records executed steps, injects failures on demand."""

    def __init__(
        self,
        *,
        fail_on: tuple[str, ...] = (),
        hang_on: tuple[str, ...] = (),
        execute_delay_s: float = 0.0,
    ) -> None:
        self._link_up = True  # False simulates an unreachable robot
        self._halted = False
        self.executed: list[ActionStep] = []
        self.stops = 0
        self.fail_on = set(fail_on)
        self.hang_on = set(hang_on)
        self.execute_delay_s = execute_delay_s

    # -- adapter API ------------------------------------------------------------

    async def fetch_telemetry(self) -> Telemetry | None:
        if not self._link_up:
            return None
        return Telemetry(
            timestamp=time.time(),
            monotonic=time.monotonic(),
            connected=True,
            motion_status="idle",
            pose=self._pose(),
            battery_pct=100.0,
        )

    async def execute(self, action: ActionStep) -> None:
        self.executed.append(action)
        if action.action in self.hang_on:
            await asyncio.sleep(60)  # only the executor's wait_for ends this
        if self.execute_delay_s > 0:
            await asyncio.sleep(self.execute_delay_s)
        if action.action in self.fail_on:
            raise AdapterError(f"simulated failure of '{action.action}'")

    async def stop(self) -> None:
        self._halted = True
        self.stops += 1

    async def disconnect(self) -> None:
        self._link_up = False

    # -- test controls ------------------------------------------------------------

    def _pose(self) -> Pose:
        return Pose(x_m=0.0, y_m=0.0, heading_deg=0.0)

    def drop_link(self) -> None:
        self._link_up = False

    @property
    def halted(self) -> bool:
        return self._halted


@asynccontextmanager
async def stack(
    settings: "Settings",
    planner: Any,
    adapter: Any | None = None,
    profiles: ProfileManager | None = None,
) -> AsyncIterator[tuple[Executor, Any]]:
    """Executor over a fake adapter + profiles, torn down safely."""
    adapter = adapter if adapter is not None else FakeAdapter()
    profiles = profiles if profiles is not None else make_profiles()
    executor = Executor(settings, adapter, planner, profiles)
    try:
        yield executor, adapter
    finally:
        executor.stop()
        await adapter.disconnect()


async def wait_for_status(
    command: "Command",
    *statuses: str,
    timeout_s: float = 5.0,
) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if command.status in statuses:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(
        f"timed out waiting for {list(statuses)}; "
        f"status is {command.status} (error: {command.error})"
    )

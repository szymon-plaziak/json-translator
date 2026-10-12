"""Executor tests: happy paths, profile rejections, e-stop, timeouts, dead telemetry."""

from __future__ import annotations

import asyncio

import pytest

from robotctl.core import ProfileManager
from robotctl.runtime import ExecutorBusy

from helpers import (
    FakeAdapter,
    FakePlanner,
    action_plan,
    multi_plan,
    stack,
    wait_for_status,
)


async def test_single_action_completes(settings):
    plan = action_plan({"action": "move", "distance_m": 0.1, "speed_mps": 1.0})
    async with stack(settings, FakePlanner(plan)) as (executor, adapter):
        command = await executor.submit("move a bit")
        await wait_for_status(command, "completed")
        assert command.steps[0].status == "done"
        assert len(adapter.executed) == 1
        assert adapter.executed[0].action == "move"


async def test_multi_step_plan_completes(settings):
    plan = multi_plan(
        {"action": "move", "distance_m": 0.1, "speed_mps": 1.0},
        {"action": "turn", "angle_deg": 90, "speed_dps": 90},
        {"action": "move", "distance_m": 0.1, "speed_mps": 1.0},
    )
    async with stack(settings, FakePlanner(plan)) as (executor, adapter):
        command = await executor.submit("go around")
        await wait_for_status(command, "completed")
        assert [step_.status for step_ in command.steps] == ["done", "done", "done"]
        assert [s.action for s in adapter.executed] == ["move", "turn", "move"]


async def test_defaults_are_filled_before_execution(settings):
    plan = action_plan({"action": "move", "distance_m": 0.1})
    async with stack(settings, FakePlanner(plan)) as (executor, adapter):
        command = await executor.submit("move")
        await wait_for_status(command, "completed")
        # profile default speed_mps=0.5 was filled in for the adapter
        assert adapter.executed[0].params() == {"distance_m": 0.1, "speed_mps": 0.5}


async def test_out_of_profile_action_is_rejected(settings):
    plan = action_plan({"action": "teleport", "distance_m": 1.0})
    async with stack(settings, FakePlanner(plan)) as (executor, _adapter):
        command = await executor.submit("beam me")
        await wait_for_status(command, "rejected")
        assert "unknown action 'teleport'" in (command.error or "")


async def test_out_of_range_plan_is_rejected(settings):
    plan = action_plan({"action": "move", "distance_m": 99.0})
    async with stack(settings, FakePlanner(plan)) as (executor, _adapter):
        command = await executor.submit("go far")
        await wait_for_status(command, "rejected")
        assert "above maximum" in (command.error or "")


async def test_unknown_step_param_is_rejected(settings):
    plan = action_plan({"action": "move", "distance_m": 0.1, "payload": "rm -rf /"})
    async with stack(settings, FakePlanner(plan)) as (executor, _adapter):
        command = await executor.submit("sneaky")
        await wait_for_status(command, "rejected")
        assert "does not accept" in (command.error or "")


async def test_plan_length_limit_is_rejected(settings):
    plan = multi_plan(*[{"action": "wait", "seconds": 0.01} for _ in range(9)])
    async with stack(settings, FakePlanner(plan)) as (executor, _adapter):
        command = await executor.submit("too many steps")
        await wait_for_status(command, "rejected")
        assert "limit" in (command.error or "")


async def test_executor_rejects_when_profile_is_missing(settings):
    plan = action_plan({"action": "move", "distance_m": 0.1})
    empty_profiles = ProfileManager("")
    async with stack(settings, FakePlanner(plan), profiles=empty_profiles) as (executor, _adapter):
        command = await executor.submit("move")
        await wait_for_status(command, "rejected")
        assert "no robot profile" in (command.error or "")


async def test_executor_is_busy_while_running(settings):
    plan = multi_plan({"action": "wait", "seconds": 0.4}, {"action": "wait", "seconds": 0.4})
    async with stack(
        settings, FakePlanner(plan), FakeAdapter(execute_delay_s=0.2)
    ) as (executor, _adapter):
        first = await executor.submit("take your time")
        await asyncio.sleep(0.1)
        with pytest.raises(ExecutorBusy):
            await executor.submit("another")
        await wait_for_status(first, "completed")


async def test_emergency_stop_mid_plan(settings):
    plan = multi_plan(
        {"action": "wait", "seconds": 0.3},
        {"action": "move", "distance_m": 0.5, "speed_mps": 1.0},
        {"action": "wait", "seconds": 0.3},
    )
    adapter = FakeAdapter(execute_delay_s=0.3)
    async with stack(settings, FakePlanner(plan), adapter) as (executor, adapter_):
        command = await executor.submit("long trip")
        await asyncio.sleep(0.15)
        assert executor.stop() is True
        await wait_for_status(command, "stopped")
        assert command.error == "stopped by operator"
        skipped = [step_ for step_ in command.steps if step_.status == "skipped"]
        assert len(skipped) >= 1
        assert adapter_.halted  # the adapter was told to halt


async def test_stop_with_no_running_command_is_noop(settings):
    async with stack(settings, FakePlanner()) as (executor, _adapter):
        assert executor.stop() is False


async def test_step_timeout_stops_robot(settings):
    settings.step_timeout_s = 0.3
    plan = action_plan({"action": "move", "distance_m": 0.5, "speed_mps": 1.0})
    adapter = FakeAdapter(hang_on=("move",))
    async with stack(settings, FakePlanner(plan), adapter) as (executor, adapter_):
        command = await executor.submit("move")
        await wait_for_status(command, "failed")
        assert "timed out" in (command.error or "")
        assert adapter_.stops >= 1


async def test_dead_telemetry_aborts_plan(settings):
    plan = multi_plan(
        {"action": "wait", "seconds": 0.4},
        {"action": "move", "distance_m": 0.1, "speed_mps": 1.0},
    )
    adapter = FakeAdapter(execute_delay_s=0.4)
    async with stack(settings, FakePlanner(plan), adapter) as (executor, adapter_):
        command = await executor.submit("move after a pause")
        # let the first step finish, then kill the telemetry link
        while len(adapter_.executed) < 1:
            await asyncio.sleep(0.02)
        adapter_.drop_link()
        await wait_for_status(command, "failed")
        assert "aborted before step 2" in (command.error or "")
        assert adapter_.stops >= 1  # the robot was stopped safely


async def test_adapter_failure_stops_robot(settings):
    plan = action_plan({"action": "move", "distance_m": 2.0, "speed_mps": 1.0})
    adapter = FakeAdapter(fail_on=("move",))
    async with stack(settings, FakePlanner(plan), adapter) as (executor, adapter_):
        command = await executor.submit("drive into trouble")
        await wait_for_status(command, "failed")
        assert "simulated failure" in (command.error or "")
        assert adapter_.stops >= 1


async def test_planner_error_marks_command_failed(settings):
    async with stack(settings, FakePlanner(error="LLM unreachable")) as (executor, _adapter):
        command = await executor.submit("do a thing")
        await wait_for_status(command, "failed")
        assert "LLM unreachable" in (command.error or "")

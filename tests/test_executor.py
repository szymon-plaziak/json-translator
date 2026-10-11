"""Executor tests: happy paths, profile rejections, e-stop, timeouts, stale telemetry."""

from __future__ import annotations

import asyncio

import pytest

from robotctl.executor import CommandStatus, ExecutorBusy
from robotctl.profile import ProfileManager
from robotctl.schemas import PlanResponse

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
    async with stack(settings, FakePlanner(plan)) as (executor, adapter, _monitor):
        command = await executor.submit("move a bit", source="cli")
        await wait_for_status(command, CommandStatus.completed)
        assert command.steps[0].status == "done"
        assert len(adapter.executed) == 1
        assert adapter.executed[0].action == "move"


async def test_multi_step_plan_completes(settings):
    plan = multi_plan(
        {"action": "move", "distance_m": 0.1, "speed_mps": 1.0},
        {"action": "turn", "angle_deg": 90, "speed_dps": 90},
        {"action": "move", "distance_m": 0.1, "speed_mps": 1.0},
    )
    async with stack(settings, FakePlanner(plan)) as (executor, adapter, _monitor):
        command = await executor.submit("go around", source="cli")
        await wait_for_status(command, CommandStatus.completed)
        assert [step_.status for step_ in command.steps] == ["done", "done", "done"]
        assert [s.action for s in adapter.executed] == ["move", "turn", "move"]


async def test_defaults_are_filled_before_execution(settings):
    plan = action_plan({"action": "move", "distance_m": 0.1})
    async with stack(settings, FakePlanner(plan)) as (executor, adapter, _monitor):
        command = await executor.submit("move", source="cli")
        await wait_for_status(command, CommandStatus.completed)
        # profile default speed_mps=0.5 was filled in for the adapter
        assert adapter.executed[0].params() == {"distance_m": 0.1, "speed_mps": 0.5}


async def test_clarification_is_surfaced(settings):
    plan = PlanResponse(version=1, mode="clarification", question="Left or right?", options=["left", "right"])
    async with stack(settings, FakePlanner(plan)) as (executor, _adapter, _monitor):
        command = await executor.submit("go there", source="cli")
        await wait_for_status(command, CommandStatus.clarification)
        assert command.question == "Left or right?"


async def test_out_of_profile_action_is_rejected(settings):
    plan = action_plan({"action": "teleport", "distance_m": 1.0})
    async with stack(settings, FakePlanner(plan)) as (executor, _adapter, _monitor):
        command = await executor.submit("beam me", source="cli")
        await wait_for_status(command, CommandStatus.rejected)
        assert "unknown action 'teleport'" in (command.error or "")


async def test_out_of_range_plan_is_rejected(settings):
    plan = action_plan({"action": "move", "distance_m": 99.0})
    async with stack(settings, FakePlanner(plan)) as (executor, _adapter, _monitor):
        command = await executor.submit("go far", source="cli")
        await wait_for_status(command, CommandStatus.rejected)
        assert "above maximum" in (command.error or "")


async def test_unknown_step_param_is_rejected(settings):
    plan = action_plan({"action": "move", "distance_m": 0.1, "payload": "rm -rf /"})
    async with stack(settings, FakePlanner(plan)) as (executor, _adapter, _monitor):
        command = await executor.submit("sneaky", source="cli")
        await wait_for_status(command, CommandStatus.rejected)
        assert "does not accept" in (command.error or "")


async def test_plan_length_limit_is_rejected(settings):
    settings.max_plan_steps = 2
    plan = multi_plan(
        {"action": "wait", "seconds": 0.01},
        {"action": "wait", "seconds": 0.01},
        {"action": "wait", "seconds": 0.01},
    )
    async with stack(settings, FakePlanner(plan)) as (executor, _adapter, _monitor):
        command = await executor.submit("too many steps", source="cli")
        await wait_for_status(command, CommandStatus.rejected)
        assert "limit" in (command.error or "")


async def test_executor_rejects_when_profile_is_missing(settings):
    plan = action_plan({"action": "move", "distance_m": 0.1})
    empty_profiles = ProfileManager("")
    async with stack(settings, FakePlanner(plan), profiles=empty_profiles) as (executor, _adapter, _monitor):
        command = await executor.submit("move", source="cli")
        await wait_for_status(command, CommandStatus.rejected)
        assert "no robot profile" in (command.error or "")


async def test_executor_is_busy_while_running(settings):
    plan = multi_plan({"action": "wait", "seconds": 0.4}, {"action": "wait", "seconds": 0.4})
    async with stack(
        settings, FakePlanner(plan), FakeAdapter(execute_delay_s=0.2)
    ) as (executor, _adapter, _monitor):
        first = await executor.submit("take your time", source="cli")
        await asyncio.sleep(0.1)
        with pytest.raises(ExecutorBusy):
            await executor.submit("another", source="cli")
        await wait_for_status(first, CommandStatus.completed)


async def test_emergency_stop_mid_plan(settings):
    plan = multi_plan(
        {"action": "wait", "seconds": 0.3},
        {"action": "move", "distance_m": 0.5, "speed_mps": 1.0},
        {"action": "wait", "seconds": 0.3},
    )
    adapter = FakeAdapter(execute_delay_s=0.3)
    async with stack(settings, FakePlanner(plan), adapter) as (executor, _adapter, monitor):
        command = await executor.submit("long trip", source="cli")
        await asyncio.sleep(0.15)
        assert executor.stop() is True
        await wait_for_status(command, CommandStatus.stopped)
        assert command.error == "stopped by operator"
        skipped = [step_ for step_ in command.steps if step_.status == "skipped"]
        assert len(skipped) >= 1
        assert adapter.halted  # the adapter was told to halt


async def test_stop_with_no_running_command_is_noop(settings):
    async with stack(settings, FakePlanner()) as (executor, _adapter, _monitor):
        assert executor.stop() is False


async def test_step_timeout_stops_robot(settings):
    settings.step_timeout_s = 0.3
    plan = action_plan({"action": "move", "distance_m": 0.5, "speed_mps": 1.0})
    adapter = FakeAdapter(hang_on=("move",))
    async with stack(settings, FakePlanner(plan), adapter) as (executor, _adapter, _monitor):
        command = await executor.submit("move", source="cli")
        await wait_for_status(command, CommandStatus.failed)
        assert "timed out" in (command.error or "")
        assert adapter.stops >= 1


async def test_stale_telemetry_aborts_plan(settings):
    settings.telemetry_max_age_s = 0.5
    plan = multi_plan(
        {"action": "wait", "seconds": 0.4},
        {"action": "move", "distance_m": 0.1, "speed_mps": 1.0},
    )
    adapter = FakeAdapter(execute_delay_s=0.4)
    async with stack(settings, FakePlanner(plan), adapter) as (executor, adapter_, _monitor):
        command = await executor.submit("move after a pause", source="cli")
        # let the first step finish, then kill the telemetry link
        while len(adapter_.executed) < 1:
            await asyncio.sleep(0.02)
        adapter_.drop_link()
        await wait_for_status(command, CommandStatus.failed)
        assert "stale" in (command.error or "")


async def test_adapter_failure_stops_robot(settings):
    plan = action_plan({"action": "move", "distance_m": 2.0, "speed_mps": 1.0})
    adapter = FakeAdapter(fail_on=("move",))
    async with stack(settings, FakePlanner(plan), adapter) as (executor, _adapter, _monitor):
        command = await executor.submit("drive into trouble", source="cli")
        await wait_for_status(command, CommandStatus.failed)
        assert "simulated failure" in (command.error or "")
        assert adapter.stops >= 1


async def test_risky_action_denied_by_operator(settings):
    settings.risky_actions = ["move"]
    plan = action_plan({"action": "move", "distance_m": 0.1, "speed_mps": 1.0})

    async def deny(_command) -> bool:
        return False

    async with stack(settings, FakePlanner(plan)) as (executor, _adapter, _monitor):
        command = await executor.submit("move", source="cli", confirm=deny)
        await wait_for_status(command, CommandStatus.rejected)
        assert "rejected by operator" in (command.error or "")


async def test_risky_action_runs_after_confirmation(settings):
    settings.risky_actions = ["move"]
    plan = action_plan({"action": "move", "distance_m": 0.1, "speed_mps": 1.0})

    async def approve(_command) -> bool:
        return True

    async with stack(settings, FakePlanner(plan)) as (executor, _adapter, _monitor):
        command = await executor.submit("move", source="cli", confirm=approve)
        await wait_for_status(command, CommandStatus.completed)


async def test_api_source_cannot_run_risky_actions(settings):
    settings.risky_actions = ["move"]
    plan = action_plan({"action": "move", "distance_m": 0.1, "speed_mps": 1.0})
    async with stack(settings, FakePlanner(plan)) as (executor, _adapter, _monitor):
        command = await executor.submit("move", source="api")
        await wait_for_status(command, CommandStatus.rejected)
        assert "confirmation required" in (command.error or "")


async def test_planner_error_marks_command_failed(settings):
    async with stack(settings, FakePlanner(error="LLM unreachable")) as (executor, _adapter, _monitor):
        command = await executor.submit("do a thing", source="cli")
        await wait_for_status(command, CommandStatus.failed)
        assert "LLM unreachable" in (command.error or "")


async def test_submit_plan_skips_llm(settings):
    plan = action_plan({"action": "turn", "angle_deg": 10, "speed_dps": 90})
    planner = FakePlanner()
    async with stack(settings, planner) as (executor, adapter, _monitor):
        command = await executor.submit_plan(plan, raw_command="/do turn 10")
        await wait_for_status(command, CommandStatus.completed)
        assert planner.calls == []  # the LLM was never consulted
        assert adapter.executed[0].action == "turn"

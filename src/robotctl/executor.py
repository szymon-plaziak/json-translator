"""Executor: runs validated plans step by step with closed-loop safety checks.

The LLM only ever proposes plans; this module — plus the deterministic safety
layer — stays in control. The emergency stop cancels the running task and
invokes the adapter's stop mechanism directly, bypassing the LLM entirely.
"""

from __future__ import annotations

import asyncio
import enum
import time
import uuid
from typing import Awaitable, Callable, Literal

from pydantic import BaseModel, Field

from robotctl.adapters.base import AdapterError, RobotAdapter
from robotctl.config import Settings
from robotctl.monitor import StateMonitor
from robotctl.planner import PlannerError
from robotctl.profile import ProfileManager
from robotctl.schemas import ActionStep, PlanResponse, describe_step
from robotctl.safety import SafetyViolation, requires_confirmation, validate_plan


class ExecutorBusy(Exception):
    """Raised when a command is submitted while another is still running."""


class CommandStatus(str, enum.Enum):
    planning = "planning"
    clarification = "clarification"
    awaiting_confirmation = "awaiting_confirmation"
    executing = "executing"
    completed = "completed"
    failed = "failed"
    rejected = "rejected"
    stopped = "stopped"


class StepResult(BaseModel):
    index: int
    action: str
    detail: str
    status: Literal["done", "failed", "skipped"]
    error: str | None = None


class Command(BaseModel):
    id: str
    source: Literal["cli", "api"]
    raw_command: str
    status: CommandStatus
    created_at: float
    updated_at: float
    plan: PlanResponse | None = None
    steps: list[StepResult] = Field(default_factory=list)
    question: str | None = None
    options: list[str] = Field(default_factory=list)
    error: str | None = None


ConfirmCallback = Callable[[Command], Awaitable[bool]]
PlanProvider = Callable[[str], Awaitable[PlanResponse]]


class Executor:
    """Single-command-at-a-time plan executor with an always-available stop path."""

    def __init__(
        self,
        settings: Settings,
        adapter: RobotAdapter,
        monitor: StateMonitor,
        planner: PlanProvider,
        profiles: ProfileManager,
    ) -> None:
        self._settings = settings
        self._adapter = adapter
        self._monitor = monitor
        self._planner = planner
        self._profiles = profiles
        self._commands: dict[str, Command] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._current: str | None = None

    # -- public API ------------------------------------------------------------

    async def submit(
        self,
        raw_command: str,
        *,
        source: Literal["cli", "api"] = "cli",
        confirm: ConfirmCallback | None = None,
    ) -> Command:
        """Plan a natural-language command via the LLM, then execute it."""
        self._assert_idle()
        command = self._new_command(raw_command, source)
        self._tasks[command.id] = asyncio.create_task(self._run(command, None, confirm))
        return command

    async def submit_plan(
        self,
        plan: PlanResponse,
        *,
        raw_command: str = "/do",
        source: Literal["cli", "api"] = "cli",
        confirm: ConfirmCallback | None = None,
    ) -> Command:
        """Execute an already-formed plan (skips the LLM; still fully validated)."""
        self._assert_idle()
        command = self._new_command(raw_command, source)
        command.plan = plan
        self._tasks[command.id] = asyncio.create_task(self._run(command, plan, confirm))
        return command

    def stop(self, command_id: str | None = None) -> bool:
        """Emergency stop: cancel the running task; its handler halts the robot.

        Never talks to the LLM. Returns True if a running task was cancelled.
        """
        targets = [command_id] if command_id else ([self._current] if self._current else [])
        stopped = False
        for cid in targets:
            task = self._tasks.get(cid)
            if task is not None and not task.done():
                task.cancel()
                stopped = True
        return stopped

    def get(self, command_id: str) -> Command | None:
        return self._commands.get(command_id)

    def all(self) -> list[Command]:
        return list(self._commands.values())

    def current(self) -> Command | None:
        return self._commands.get(self._current) if self._current is not None else None

    # -- internals ---------------------------------------------------------------

    def _assert_idle(self) -> None:
        current = self.current()
        if current is not None:
            task = self._tasks[current.id]
            if not task.done():
                raise ExecutorBusy(
                    f"command {current.id} is still {current.status.value}; send 'stop' first"
                )

    def _new_command(self, raw_command: str, source: Literal["cli", "api"]) -> Command:
        now = time.time()
        command = Command(
            id=uuid.uuid4().hex[:12],
            source=source,
            raw_command=raw_command,
            status=CommandStatus.planning,
            created_at=now,
            updated_at=now,
        )
        self._commands[command.id] = command
        self._current = command.id
        return command

    async def _run(
        self,
        command: Command,
        plan: PlanResponse | None,
        confirm: ConfirmCallback | None,
    ) -> None:
        try:
            if plan is None:
                plan = await self._planner.plan(command.raw_command)
                command.plan = plan
            await self._execute_plan(command, plan, confirm)
        except PlannerError as exc:
            command.error = str(exc)
            self._update(command, CommandStatus.failed)
        except SafetyViolation as exc:
            command.error = str(exc)
            self._update(command, CommandStatus.rejected)
        except asyncio.CancelledError:
            # Emergency stop / shutdown path: never re-raise, land on 'stopped'.
            command.error = "stopped by operator"
            self._mark_pending_skipped(command)
            self._update(command, CommandStatus.stopped)
            await self._safe_stop()
        except Exception as exc:  # last-resort net: never leave the robot moving
            command.error = f"unexpected error: {exc}"
            self._update(command, CommandStatus.failed)
            await self._safe_stop()

    async def _execute_plan(
        self,
        command: Command,
        plan: PlanResponse,
        confirm: ConfirmCallback | None,
    ) -> None:
        if plan.mode == "clarification":
            command.question = plan.question
            command.options = list(plan.options)
            self._update(command, CommandStatus.clarification)
            return

        profile = self._profiles.current()
        if profile is None:
            command.error = "no robot profile loaded; upload one via POST /profile"
            self._update(command, CommandStatus.rejected)
            return
        steps = validate_plan(plan, profile, self._settings)  # raises SafetyViolation

        if requires_confirmation(plan, self._settings):
            if confirm is None:
                command.error = (
                    'plan contains risky actions; confirmation required (resend with "confirm": true)'
                )
                self._update(command, CommandStatus.rejected)
                return
            self._update(command, CommandStatus.awaiting_confirmation)
            if not await confirm(command):
                command.error = "rejected by operator"
                self._update(command, CommandStatus.rejected)
                return

        self._update(command, CommandStatus.executing)
        for index, step in enumerate(steps):
            if not self._monitor.is_fresh():
                command.error = (
                    f"telemetry is stale or the robot is disconnected; aborted before step {index + 1}"
                )
                self._update(command, CommandStatus.failed)
                await self._safe_stop()
                return
            result = StepResult(index=index, action=step.action, detail=describe_step(step), status="done")
            try:
                await asyncio.wait_for(self._adapter.execute(step), timeout=self._settings.step_timeout_s)
            except TimeoutError:
                result.status = "failed"
                result.error = f"exceeded the {self._settings.step_timeout_s:g} s step timeout"
                command.steps.append(result)
                command.error = f"step {index + 1} ({step.action}) timed out"
                self._update(command, CommandStatus.failed)
                await self._safe_stop()
                return
            except AdapterError as exc:
                result.status = "failed"
                result.error = str(exc)
                command.steps.append(result)
                command.error = f"step {index + 1} ({step.action}) failed: {exc}"
                self._update(command, CommandStatus.failed)
                await self._safe_stop()
                return
            except asyncio.CancelledError:
                raise
            command.steps.append(result)
        self._update(command, CommandStatus.completed)

    def _mark_pending_skipped(self, command: Command) -> None:
        """Record not-yet-executed steps as skipped when a command is stopped."""
        if command.plan is None or command.plan.mode == "clarification":
            return
        profile = self._profiles.current()
        if profile is None:
            return
        try:
            steps = validate_plan(command.plan, profile, self._settings)
        except SafetyViolation:
            return
        executed = len(command.steps)
        for index in range(executed, len(steps)):
            command.steps.append(
                StepResult(
                    index=index,
                    action=steps[index].action,
                    detail=describe_step(steps[index]),
                    status="skipped",
                )
            )

    def _update(self, command: Command, status: CommandStatus) -> None:
        command.status = status
        command.updated_at = time.time()

    async def _safe_stop(self) -> None:
        try:
            await self._adapter.stop()
        except AdapterError:
            pass

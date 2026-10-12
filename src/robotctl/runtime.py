"""Runtime: the generic HTTP bridge to the robot, the LLM planner and the
step-by-step executor.

The LLM only ever proposes plans; the executor — plus the deterministic
profile checks in `core` — stays in control. The emergency stop cancels the
running task and invokes the adapter's stop mechanism directly, bypassing
the LLM entirely.

Telemetry is fetched on demand (before planning, before every step, for
/state and /health) instead of being polled in the background: a snapshot
is fresh by construction, and a failed fetch simply aborts the plan safely.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from typing import Any, Callable, Literal

import httpx
from pydantic import BaseModel, Field, ValidationError

from robotctl.core import (
    MAX_PLAN_STEPS,
    SCHEMA_VERSION,
    ActionStep,
    MotionStatus,
    PlanResponse,
    Pose,
    ProfileError,
    ProfileManager,
    RobotProfile,
    SafetyViolation,
    Settings,
    Telemetry,
    describe_step,
)

# ---------------------------------------------------------------------------
# Generic HTTP bridge adapter — the universal way to reach a real robot.
#
# The robot (its firmware or companion app) exposes three HTTP endpoints:
#
# - ``GET  {ROBOT_TELEMETRY_PATH}`` → JSON telemetry snapshot (on demand)
# - ``POST {ROBOT_COMMAND_PATH}``   ← one validated action JSON; responds when
#   the action finished (2xx) or failed (4xx/5xx, or 2xx with ``ok: false`` /
#   ``status: "failed"|"error"``)
# - ``POST {ROBOT_STOP_PATH}``      ← emergency halt (best effort)
#
# The command payload is the validated step JSON, e.g.
# ``{"action": "move", "distance_m": 0.5, "speed_mps": 0.2}`` — exactly the
# format the uploaded robot profile teaches the LLM.
#
# Telemetry mapping: ``motion_status`` is required (one of idle/moving/
# turning/stopped/error; unknown values map to ``error``), everything else
# (``pose``, ``battery_pct``, ``obstacle_distance_m``, speeds,
# ``last_action_id``) is optional and omitted when absent.
# ---------------------------------------------------------------------------

_STOP_TIMEOUT_S = 2.0
_TELEMETRY_TIMEOUT_S = 3.0  # telemetry GETs must never block execution long


class AdapterError(Exception):
    """Raised when the adapter cannot complete an action."""


class HttpRobotAdapter:
    """Talks to a robot's HTTP endpoints; fetches telemetry on demand.

    An empty ROBOT_BASE_URL means "no robot configured": every call fails
    safe (telemetry fetches return None, execution raises) and /health
    reports it.
    """

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

    async def disconnect(self) -> None:
        """Halt the robot (best effort) and close the HTTP client."""
        await self.stop()
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- telemetry on demand ------------------------------------------------------

    async def fetch_telemetry(self) -> Telemetry | None:
        """GET the robot's telemetry endpoint; None on any failure."""
        if self._client is None:
            return None
        try:
            response = await self._client.get(
                self._settings.robot_telemetry_path, timeout=_TELEMETRY_TIMEOUT_S
            )
            response.raise_for_status()
            return _map_telemetry(response.json())
        except Exception:
            return None  # unreachable / broken payload: callers fail safe

    # -- execution ------------------------------------------------------------------

    async def execute(self, action: ActionStep) -> None:
        """Run one validated action to completion; raises AdapterError."""
        if self._client is None:
            raise AdapterError("robot not configured (check ROBOT_BASE_URL and /health)")
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


# ---------------------------------------------------------------------------
# LLM planner: converts a natural-language command into a validated
# PlanResponse.
#
# The planner is deliberately small: it formats one compact prompt, calls an
# OpenAI-compatible chat-completions endpoint over httpx (configurable base
# URL, API key and model id), and strictly validates the reply. Its output is
# never trusted: the executor re-validates every step against the profile.
# ---------------------------------------------------------------------------

# Response-format preference plus automatic downgrades for servers that lack
# structured-output support.
_FORMAT_FALLBACKS: dict[str, tuple[str | None, ...]] = {
    "json_schema": ("json_schema", "json_object", None),
    "json_object": ("json_object", None),
    "none": (None,),
}

# Think-tag strings are built by concatenation so tooling never strips them.
_THINK_OPEN = "<" + "think>"
_THINK_RE = re.compile(
    _THINK_OPEN + "(.*?)" + "<" + "/think>",
    re.DOTALL | re.IGNORECASE,
)

SYSTEM_PROMPT = """\
You convert a natural-language robot command into robot actions.
Respond with a single JSON object and nothing else: no prose, no markdown, no explanations.

Response envelope (version {version}):
{{
  "version": {version},
  "mode": "action" | "plan",
  "steps": [ ... ]
}}

Robot: {robot_name} — {robot_description}

Actions available for THIS robot (use ONLY these actions with ONLY these fields):
{actions_section}

Correct answer examples for THIS robot — mimic this format exactly:
{examples_section}

Rules:
- version is always {version}.
- mode "action": exactly one step, for simple commands.
- mode "plan": 2-{max_plan_steps} ordered steps, for compound commands.
- If a request exceeds a single action's limits or needs several actions, do
  not ask: reply with mode "plan" and split it into steps that each stay
  within the limits above.
- The command may be vague or informal: do not ask, decide yourself on the
  most reasonable interpretation. When unsure, pick conservative values
  (smaller distances, slower speeds) within the limits above.
- Base every decision only on the telemetry below. Never invent positions,
  obstacles, or success. Do not repeat or chain steps needlessly.
- Do not think out loud and do not explain: reply with the JSON object only.

Current telemetry: {telemetry}
"""


class PlannerError(Exception):
    """Raised when no valid plan could be obtained from the LLM, or when a
    reply is not valid JSON / violates the contract (triggers a retry)."""


class LLMPlanner:
    """Calls an OpenAI-compatible endpoint and validates the reply strictly."""

    def __init__(
        self,
        settings: Settings,
        profiles: ProfileManager,
        adapter: Any | None = None,
        client: Any | None = None,
    ) -> None:
        """`adapter` (anything with `async fetch_telemetry()`) feeds the prompt;
        `client` is an httpx-like double for tests."""
        self._settings = settings
        self._profiles = profiles
        self._adapter = adapter
        self._client = client if client is not None else httpx.AsyncClient(
            base_url=settings.llm_base_url,
            timeout=settings.llm_timeout_s,
            headers={"Authorization": f"Bearer {settings.llm_api_key}"},
        )
        self._owns_client = client is None
        self._extra_body = self._parse_extra_body(settings.llm_extra_body)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # -- public API ------------------------------------------------------------

    async def plan(self, command: str) -> PlanResponse:
        profile = self._profiles.current()
        if profile is None:
            raise PlannerError("no robot profile loaded; upload one via POST /profile")
        snapshot = (
            await self._adapter.fetch_telemetry() if self._adapter is not None else None
        )
        messages: list[dict[str, str]] = [
            {"role": "system", "content": self._system_prompt(profile, snapshot)},
            {"role": "user", "content": command},
        ]
        formats = _FORMAT_FALLBACKS.get(self._settings.llm_response_format, (None,))
        # One attempt per variant; the configured extra body (e.g. thinking
        # disabled) is tried first — if the server rejects it, the next variant
        # runs without it. A parse failure feeds a correction request into the
        # following attempts instead of failing the command outright.
        variants: list[tuple[str | None, dict[str, Any] | None]] = []
        for fmt in formats:
            for extra in ([self._extra_body, None] if self._extra_body else [None]):
                variants.append((fmt, extra))
        raw_answer = ""
        last_error = "no attempt made"
        for fmt, extra in variants:
            try:
                content = await self._complete(messages, fmt, extra, profile)
            except Exception as exc:  # transport/API error -> next variant
                last_error = f"LLM request failed: {exc}"
                continue
            raw_answer, _thinking = split_thinking(content)
            try:
                return parse_plan_response(raw_answer, profile)
            except PlannerError as exc:
                last_error = str(exc)
                messages = messages + [
                    {"role": "assistant", "content": raw_answer},
                    {"role": "user", "content": f"Invalid response: {exc}. Reply with corrected JSON only."},
                ]
        raise PlannerError(last_error)

    # -- internals ---------------------------------------------------------------

    async def _complete(
        self,
        messages: list[dict[str, str]],
        fmt: str | None,
        extra_body: dict[str, Any] | None,
        profile: RobotProfile,
    ) -> str:
        payload: dict[str, Any] = {
            "model": self._settings.llm_model,
            "messages": messages,
            "temperature": self._settings.llm_temperature,
            "max_tokens": self._settings.llm_max_tokens,
        }
        if fmt == "json_schema":
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "robot_plan",
                    "strict": True,
                    "schema": profile.response_schema(),
                },
            }
        elif fmt == "json_object":
            payload["response_format"] = {"type": "json_object"}
        if extra_body:
            payload.update(extra_body)
        response = await self._client.post("/chat/completions", json=payload)
        if response.status_code >= 400:
            raise RuntimeError(f"HTTP {response.status_code} from the LLM endpoint")
        data = response.json()
        return data["choices"][0]["message"].get("content") or ""

    @staticmethod
    def _parse_extra_body(raw: str) -> dict[str, Any] | None:
        if not raw.strip():
            return None
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"LLM_EXTRA_BODY is not valid JSON: {exc.msg}") from exc
        if not isinstance(value, dict):
            raise ValueError("LLM_EXTRA_BODY must be a JSON object")
        return value

    def _system_prompt(self, profile: RobotProfile, snapshot: Telemetry | None) -> str:
        return SYSTEM_PROMPT.format(
            version=SCHEMA_VERSION,
            max_plan_steps=MAX_PLAN_STEPS,
            robot_name=profile.robot.name,
            robot_description=profile.robot.description or "(no description)",
            actions_section=profile.prompt_section(),
            examples_section=profile.examples_section(),
            telemetry=_telemetry_brief(snapshot),
        )


def split_thinking(content: str) -> tuple[str, str | None]:
    """Separate reasoning-model thinking from the actual answer.

    Handles think-blocks (terminated and unterminated); also returns cleanly
    for replies without any thinking.
    """
    blocks = _THINK_RE.findall(content)
    answer = _THINK_RE.sub("", content)
    if _THINK_OPEN in answer:  # unterminated block: everything after the tag is reasoning
        head, _, tail = answer.partition(_THINK_OPEN)
        blocks.append(tail)
        answer = head
    thinking = "\n\n".join(block.strip() for block in blocks if block.strip()) or None
    return answer.strip(), thinking


def parse_plan_response(content: str, profile: RobotProfile | None = None) -> PlanResponse:
    """Parse raw LLM output into a PlanResponse. Tolerates markdown fences.

    With a profile, steps are additionally validated against the robot's
    action vocabulary here (not only in the executor) so the retry loop can
    auto-correct invalid actions.
    """
    text = content.strip()
    if text.startswith("```"):
        newline = text.find("\n")
        text = text[newline + 1 :] if newline != -1 else text.strip("`")
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise PlannerError("response contains no JSON object")
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise PlannerError(f"invalid JSON: {exc.msg}") from exc
    try:
        plan = PlanResponse.model_validate(data)
    except ValidationError as exc:
        raise PlannerError(f"schema violation: {_first_error(exc)}") from exc
    # Contract checks that pure JSON-schema validation cannot express. They
    # live here (not only in the executor) so the retry loop can auto-correct.
    if plan.mode == "action" and len(plan.steps) != 1:
        raise PlannerError(
            "mode 'action' must contain exactly one step; to use several steps, set mode to 'plan'"
        )
    if profile is not None:
        for step in plan.steps:
            try:
                profile.check_step(step)
            except ProfileError as exc:
                raise PlannerError(
                    f"invalid step for this robot: {exc}. Use only the actions "
                    "listed in the system prompt."
                ) from exc
    return plan


def _first_error(exc: ValidationError) -> str:
    error = exc.errors()[0]
    location = ".".join(str(part) for part in error["loc"])
    return f"{location or 'plan'}: {error['msg']}"


def _telemetry_brief(snapshot: Telemetry | None) -> str:
    if snapshot is None:
        return "unavailable (no telemetry)"

    def part(label: str, value: float | None, fmt: str) -> str:
        return "n/a" if value is None else format(value, fmt)

    pose = snapshot.pose
    pose_text = (
        "n/a"
        if pose is None
        else f"({pose.x_m:.2f}, {pose.y_m:.2f}) m, heading {pose.heading_deg:.1f} deg"
    )
    return (
        f"timestamp={snapshot.timestamp:.0f}, connected={snapshot.connected}, "
        f"pose={pose_text}, "
        f"speed={part('speed', snapshot.linear_speed_mps, '.2f')} m/s, "
        f"battery={part('battery', snapshot.battery_pct, '.0f')}%, "
        f"front_obstacle={part('obstacle', snapshot.obstacle_distance_m, '.2f')} m, "
        f"motion={snapshot.motion_status}"
    )


# ---------------------------------------------------------------------------
# Executor: runs validated plans step by step.
#
# The LLM only ever proposes plans; this module — plus the deterministic
# profile checks — stays in control. The emergency stop cancels the running
# task and invokes the adapter's stop mechanism directly, bypassing the LLM
# entirely.
# ---------------------------------------------------------------------------


class ExecutorBusy(Exception):
    """Raised when a command is submitted while another is still running."""


class StepResult(BaseModel):
    index: int
    action: str
    detail: str
    status: Literal["done", "failed", "skipped"]
    error: str | None = None


class Command(BaseModel):
    id: str
    raw_command: str
    status: str
    created_at: float
    updated_at: float
    plan: PlanResponse | None = None
    steps: list[StepResult] = Field(default_factory=list)
    error: str | None = None


class Executor:
    """Single-command-at-a-time plan executor with an always-available stop path."""

    def __init__(
        self,
        settings: Settings,
        adapter: HttpRobotAdapter | Any,
        planner: Callable[[str], Any],
        profiles: ProfileManager,
    ) -> None:
        self._settings = settings
        self._adapter = adapter
        self._planner = planner
        self._profiles = profiles
        self._commands: dict[str, Command] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._current: str | None = None

    # -- public API ------------------------------------------------------------

    async def submit(self, raw_command: str) -> Command:
        """Plan a natural-language command via the LLM, then execute it."""
        self._assert_idle()
        command = self._new_command(raw_command)
        self._tasks[command.id] = asyncio.create_task(self._run(command))
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

    def current(self) -> Command | None:
        return self._commands.get(self._current) if self._current is not None else None

    # -- internals ---------------------------------------------------------------

    def _assert_idle(self) -> None:
        current = self.current()
        if current is not None:
            task = self._tasks[current.id]
            if not task.done():
                raise ExecutorBusy(
                    f"command {current.id} is still {current.status}; POST /stop first"
                )

    def _new_command(self, raw_command: str) -> Command:
        now = time.time()
        command = Command(
            id=uuid.uuid4().hex[:12],
            raw_command=raw_command,
            status="planning",
            created_at=now,
            updated_at=now,
        )
        self._commands[command.id] = command
        self._current = command.id
        return command

    async def _run(self, command: Command) -> None:
        try:
            plan = await self._planner.plan(command.raw_command)
            command.plan = plan
            await self._execute_plan(command, plan)
        except PlannerError as exc:
            command.error = str(exc)
            self._update(command, "failed")
        except SafetyViolation as exc:
            command.error = str(exc)
            self._update(command, "rejected")
        except asyncio.CancelledError:
            # Emergency stop / shutdown path: never re-raise, land on 'stopped'.
            command.error = "stopped by operator"
            self._mark_pending_skipped(command)
            self._update(command, "stopped")
            await self._safe_stop()
        except Exception as exc:  # last-resort net: never leave the robot moving
            command.error = f"unexpected error: {exc}"
            self._update(command, "failed")
            await self._safe_stop()

    def _validated_steps(self, plan: PlanResponse, profile: RobotProfile) -> list[ActionStep]:
        """Profile-driven safety gate; raises SafetyViolation."""
        steps = plan.steps
        if not steps:
            raise SafetyViolation("plan contains no steps")
        if plan.mode == "action" and len(steps) != 1:
            raise SafetyViolation("mode 'action' must contain exactly one step")
        if len(steps) > MAX_PLAN_STEPS:
            raise SafetyViolation(
                f"plan has {len(steps)} steps; limit is {MAX_PLAN_STEPS}"
            )
        checked: list[ActionStep] = []
        for step in steps:
            try:
                checked.append(profile.check_step(step))
            except ProfileError as exc:
                raise SafetyViolation(str(exc)) from exc
        return checked

    async def _execute_plan(self, command: Command, plan: PlanResponse) -> None:
        profile = self._profiles.current()
        if profile is None:
            command.error = "no robot profile loaded; upload one via POST /profile"
            self._update(command, "rejected")
            return
        steps = self._validated_steps(plan, profile)  # raises SafetyViolation

        self._update(command, "executing")
        for index, step in enumerate(steps):
            snapshot = await self._adapter.fetch_telemetry()
            if snapshot is None or not snapshot.connected:
                command.error = (
                    f"telemetry unavailable or the robot is disconnected; "
                    f"aborted before step {index + 1}"
                )
                self._update(command, "failed")
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
                self._update(command, "failed")
                await self._safe_stop()
                return
            except AdapterError as exc:
                result.status = "failed"
                result.error = str(exc)
                command.steps.append(result)
                command.error = f"step {index + 1} ({step.action}) failed: {exc}"
                self._update(command, "failed")
                await self._safe_stop()
                return
            except asyncio.CancelledError:
                raise
            command.steps.append(result)
        self._update(command, "completed")

    def _mark_pending_skipped(self, command: Command) -> None:
        """Record not-yet-executed steps as skipped when a command is stopped."""
        if command.plan is None:
            return
        profile = self._profiles.current()
        if profile is None:
            return
        try:
            steps = self._validated_steps(command.plan, profile)
        except SafetyViolation:
            return
        for index in range(len(command.steps), len(steps)):
            command.steps.append(
                StepResult(
                    index=index,
                    action=steps[index].action,
                    detail=describe_step(steps[index]),
                    status="skipped",
                )
            )

    def _update(self, command: Command, status: str) -> None:
        command.status = status
        command.updated_at = time.time()

    async def _safe_stop(self) -> None:
        try:
            await self._adapter.stop()
        except AdapterError:
            pass

"""LLM planner: converts a natural-language command into a validated PlanResponse.

The planner is deliberately small: it formats one compact prompt, calls an
OpenAI-compatible chat-completions endpoint (configurable base URL, API key
and model id), and strictly validates the reply. Its output is never trusted:
the executor re-validates every step through the safety layer.

Every interaction (command sent, any thinking, raw answer, parsed JSON or the
error) is recorded in memory for `/logs` and appended to LLM_LOG_FILE as
JSON lines.
"""

from __future__ import annotations

import json
import re
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

from robotctl.config import Settings
from robotctl.profile import ProfileError, ProfileManager, RobotProfile
from robotctl.schemas import SCHEMA_VERSION, PlanResponse, Telemetry

# Response-format preference plus automatic downgrades for servers that lack
# structured-output support.
_FORMAT_FALLBACKS: dict[str, tuple[str | None, ...]] = {
    "json_schema": ("json_schema", "json_object", None),
    "json_object": ("json_object", None),
    "none": (None,),
}

_MAX_ATTEMPTS_PER_FORMAT = 2
_MAX_LOGGED_INTERACTIONS = 50
_MAX_CLARIFICATION_HISTORY = 3

_THINK_RE = re.compile(
    r'<think>(.*?)</think>',
    re.DOTALL | re.IGNORECASE,
)

SYSTEM_PROMPT = """\
You convert a natural-language robot command into robot actions.
Respond with a single JSON object and nothing else: no prose, no markdown, no explanations.

Response envelope (version {version}):
{{
  "version": {version},
  "mode": "action" | "plan" | "clarification",
  "steps": [ ... ],
  "question": "...",
  "options": ["...", ...]
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
- mode "clarification": the command is ambiguous, contradictory, or unsafe
  for this robot; ask one short question and give short options. Never ask
  merely because a limit is exceeded - split it into a plan instead.
- Base every decision only on the telemetry below. Never invent positions,
  obstacles, or success. Do not repeat or chain steps needlessly.
- Do not think out loud and do not explain: reply with the JSON object only.

Current telemetry: {telemetry}
"""


class PlannerError(Exception):
    """Raised when no valid plan could be obtained from the LLM."""


class PlanParseError(Exception):
    """Raised when the LLM reply is not valid JSON or violates the contract."""


class LLMInteraction(BaseModel):
    """One planner call: what was sent, what came back, what was parsed."""

    timestamp: float
    duration_s: float
    model: str
    response_format: str | None
    command: str
    attempts: int
    raw_answer: str
    thinking: str | None = None
    parsed: PlanResponse | None = None
    error: str | None = None
    clarification_context: bool = False


def split_thinking(content: str) -> tuple[str, str | None]:
    """Separate reasoning-model thinking from the actual answer.

    Handles `<think>…</think>` blocks and unterminated blocks; also returns cleanly
    for replies without any thinking.
    """
    blocks = _THINK_RE.findall(content)
    answer = _THINK_RE.sub("", content)
    if '<think>' in answer:  # unterminated block: everything after the tag is reasoning
        head, _, tail = answer.partition('<think>')
        blocks.append(tail)
        answer = head
    thinking = "\n\n".join(block.strip() for block in blocks if block.strip()) or None
    return answer.strip(), thinking


def parse_plan_response(content: str, profile: RobotProfile | None = None) -> PlanResponse:
    """Parse raw LLM output into a PlanResponse. Tolerates markdown fences.

    With a profile, steps are additionally validated against the robot's
    action vocabulary here (not only in safety) so the retry loop can
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
        raise PlanParseError("response contains no JSON object")
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise PlanParseError(f"invalid JSON: {exc.msg}") from exc
    try:
        plan = PlanResponse.model_validate(data)
    except ValidationError as exc:
        raise PlanParseError(f"schema violation: {_first_error(exc)}") from exc
    # Contract checks that pure JSON-schema validation cannot express. They
    # live here (not only in safety) so the retry loop can auto-correct.
    if plan.mode == "action" and len(plan.steps) != 1:
        raise PlanParseError(
            "mode 'action' must contain exactly one step; to use several steps, set mode to 'plan'"
        )
    if plan.mode == "plan" and not plan.steps:
        raise PlanParseError("mode 'plan' requires at least one step")
    if profile is not None and plan.mode != "clarification":
        for step in plan.steps:
            try:
                profile.check_step(step)
            except ProfileError as exc:
                raise PlanParseError(
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


class LLMPlanner:
    """Calls an OpenAI-compatible endpoint and validates the reply strictly."""

    def __init__(
        self,
        settings: Settings,
        telemetry_provider: Callable[[], Telemetry | None],
        profiles: ProfileManager,
        client: Any | None = None,
    ) -> None:
        self._settings = settings
        self._telemetry_provider = telemetry_provider
        self._profiles = profiles
        self._client = client if client is not None else AsyncOpenAI(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            timeout=settings.llm_timeout_s,
        )
        self._entries: deque[LLMInteraction] = deque(maxlen=_MAX_LOGGED_INTERACTIONS)
        self._extra_body = self._parse_extra_body(settings.llm_extra_body)
        self._log_path = self._prepare_log_path(settings.llm_log_file)
        # Unresolved clarification (question the model asked); the next command
        # gets it as context so multi-turn clarifications work.
        self._pending_clarification: dict[str, Any] | None = None

    # -- public API ------------------------------------------------------------

    async def plan(self, command: str) -> PlanResponse:
        profile = self._profiles.current()
        if profile is None:
            raise PlannerError("no robot profile loaded; upload one via POST /profile")
        started = time.monotonic()
        formats = _FORMAT_FALLBACKS.get(self._settings.llm_response_format, (None,))
        # Try the configured extra body first (e.g. thinking disabled); if the
        # server rejects it, retry without it before downgrading the format.
        variants: list[tuple[str | None, dict[str, Any] | None]] = []
        for fmt in formats:
            for extra in ([self._extra_body, None] if self._extra_body else [None]):
                variants.append((fmt, extra))

        pending = self._pending_clarification
        self._pending_clarification = None  # consumed on success; restored on failure
        messages: list[dict[str, str]] = [
            {"role": "system", "content": self._system_prompt(profile)},
            {"role": "user", "content": self._user_message(command, pending)},
        ]
        attempts = 0
        used_format: str | None = None
        raw_answer = ""
        thinking: str | None = None
        parsed: PlanResponse | None = None
        last_error = "no attempt made"
        for fmt, extra in variants:
            for _ in range(_MAX_ATTEMPTS_PER_FORMAT):
                try:
                    content, reasoning = await self._complete(messages, fmt, extra, profile)
                except Exception as exc:  # transport/API error -> next variant
                    last_error = f"LLM request failed: {exc}"
                    break
                attempts += 1
                used_format = fmt
                raw_answer, tag_thinking = split_thinking(content)
                thinking = tag_thinking or reasoning
                try:
                    parsed = parse_plan_response(raw_answer, profile)
                except PlanParseError as exc:
                    last_error = str(exc)
                    messages = messages + [
                        {"role": "assistant", "content": raw_answer},
                        {"role": "user", "content": f"Invalid response: {exc}. Reply with corrected JSON only."},
                    ]
                    continue
                self._remember_clarification(command, pending, parsed)
                self._record(command, started, attempts, used_format, raw_answer, thinking, parsed, None,
                             context_used=pending is not None)
                return parsed
        self._pending_clarification = pending  # not consumed: keep the context for a retry
        self._record(command, started, attempts, used_format, raw_answer, thinking, None, last_error,
                     context_used=pending is not None)
        raise PlannerError(last_error)

    def entries(self) -> list[LLMInteraction]:
        """Recent planner interactions, oldest first (for the CLI `/logs`)."""
        return list(self._entries)

    # -- internals ---------------------------------------------------------------

    def _user_message(self, command: str, pending: dict[str, Any] | None) -> str:
        """Raw command, or the command plus carried clarification context."""
        if pending is None:
            return command
        options = f' (options: {", ".join(pending["options"])})' if pending["options"] else ""
        lines = [
            f'Context: for your earlier command "{pending["command"]}" you asked the '
            f'clarification "{pending["question"]}"{options}.'
        ]
        for question, answer in pending["history"]:
            lines.append(f'Earlier you asked "{question}" and the user replied "{answer}".')
        lines.append(
            "If the reply below answers your question, produce the plan for the "
            "original command; otherwise treat it as a new command."
        )
        lines.append(f'User reply: {command}')
        return "\n".join(lines)

    def _remember_clarification(self, command: str, pending: dict[str, Any] | None, parsed: PlanResponse) -> None:
        """Track an unresolved clarification so the next command carries context.

        A chain of clarifications keeps the original command and accumulates
        the question/answer exchanges (bounded). A resolved plan clears it.
        """
        if parsed.mode != "clarification" or not parsed.question:
            return  # nothing pending; a consumed clarification stays consumed
        history = list(pending["history"]) if pending else []
        if pending is not None:
            history.append((pending["question"], command))
        self._pending_clarification = {
            "command": pending["command"] if pending else command,
            "history": history[-_MAX_CLARIFICATION_HISTORY:],
            "question": parsed.question,
            "options": list(parsed.options),
        }

    async def _complete(
        self,
        messages: list[dict[str, str]],
        fmt: str | None,
        extra_body: dict[str, Any] | None,
        profile: RobotProfile,
    ) -> tuple[str, str | None]:
        kwargs: dict[str, Any] = {
            "model": self._settings.llm_model,
            "messages": messages,
            "temperature": self._settings.llm_temperature,
            "max_tokens": self._settings.llm_max_tokens,
        }
        if fmt == "json_schema":
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "robot_plan",
                    "strict": True,
                    "schema": profile.response_schema(),
                },
            }
        elif fmt == "json_object":
            kwargs["response_format"] = {"type": "json_object"}
        if extra_body:
            kwargs["extra_body"] = extra_body
        response = await self._client.chat.completions.create(**kwargs)
        message = response.choices[0].message
        reasoning = getattr(message, "reasoning_content", None)
        content = message.content
        return (content or ""), (reasoning if isinstance(reasoning, str) else None)

    def _record(
        self,
        command: str,
        started: float,
        attempts: int,
        used_format: str | None,
        raw_answer: str,
        thinking: str | None,
        parsed: PlanResponse | None,
        error: str | None,
        *,
        context_used: bool = False,
    ) -> None:
        entry = LLMInteraction(
            timestamp=time.time(),
            duration_s=time.monotonic() - started,
            model=self._settings.llm_model,
            response_format=used_format,
            command=command,
            attempts=attempts,
            raw_answer=raw_answer,
            thinking=thinking,
            parsed=parsed,
            error=error,
            clarification_context=context_used,
        )
        self._entries.append(entry)
        if self._log_path is not None:
            try:
                with self._log_path.open("a", encoding="utf-8") as handle:
                    handle.write(entry.model_dump_json())
                    handle.write("\n")
            except OSError:
                pass  # logging must never break planning

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

    @staticmethod
    def _prepare_log_path(path_str: str) -> Path | None:
        if not path_str.strip():
            return None
        path = Path(path_str)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None
        return path

    def _system_prompt(self, profile: RobotProfile) -> str:
        return SYSTEM_PROMPT.format(
            version=SCHEMA_VERSION,
            max_plan_steps=self._settings.max_plan_steps,
            robot_name=profile.robot.name,
            robot_description=profile.robot.description or "(no description)",
            actions_section=profile.prompt_section(),
            examples_section=profile.examples_section(),
            telemetry=_telemetry_brief(self._telemetry_provider()),
        )

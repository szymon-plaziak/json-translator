"""Robot profile: the uploaded, robot-specific control contract.

The core app is robot-agnostic. Everything it knows about a particular robot
— which actions exist, which parameters they take, what a correct answer
looks like — comes from a profile JSON uploaded via `POST /profile` (or
preloaded from `ROBOT_PROFILE` at startup). The profile drives:

- the LLM system prompt (action vocabulary + example answers, verbatim),
- the strict structured-output JSON schema (generated per profile),
- safety validation of every step (types, required params, min/max),
- manual `/do` parsing.

Profile shape (v1)::

    {
      "profile_version": 1,
      "robot": {"name": "Rover X", "description": "4-wheel rover"},
      "actions": [
        {"action": "move", "description": "drive forward/backward",
         "params": [{"name": "distance_m", "type": "float",
                     "min": 0, "max": 10, "required": true}]}
      ],
      "examples": [
        {"version": 1, "mode": "plan",
         "steps": [{"action": "move", "distance_m": 0.5}]}
      ]
    }

`examples` must be exactly-correct answers in the universal response envelope
whose steps validate against `actions` — the LLM is shown them verbatim.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, ValidationError, model_validator

from robotctl.schemas import SCHEMA_VERSION, ActionStep, PlanResponse, StrictModel

ACTION_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
PARAM_NAME_RE = ACTION_NAME_RE
PARAM_TYPES = ("int", "float", "str", "bool")


class ProfileError(ValueError):
    """Raised when a robot profile is invalid (maps to HTTP 422)."""


class ParamDef(StrictModel):
    """One action parameter, with the limits the safety layer enforces."""

    name: str = Field(pattern=PARAM_NAME_RE.pattern)
    type: Literal["int", "float", "str", "bool"]
    description: str = Field(default="", max_length=200)
    unit: str | None = None
    min: float | None = None
    max: float | None = None
    required: bool = False
    default: int | float | str | bool | None = None

    @model_validator(mode="after")
    def _check(self) -> "ParamDef":
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError(f"param '{self.name}': min ({self.min}) > max ({self.max})")
        if self.default is not None:
            _check_value(self, self.default, context=f"param '{self.name}' default")
        if self.required and self.default is not None:
            raise ValueError(f"param '{self.name}': cannot be both required and have a default")
        return self

    def prompt(self) -> str:
        """Human-readable parameter spec for the LLM prompt."""
        bits = [self.type]
        bits.append("required" if self.required else "optional")
        if self.default is not None:
            bits.append(f"default {_fmt_number(self.default)}")
        if self.min is not None and self.max is not None:
            bits.append(f"{_fmt_number(self.min)}..{_fmt_number(self.max)}")
        elif self.min is not None:
            bits.append(f">= {_fmt_number(self.min)}")
        elif self.max is not None:
            bits.append(f"<= {_fmt_number(self.max)}")
        if self.unit:
            bits.append(self.unit)
        return f"<{', '.join(bits)}>"


class ActionDef(StrictModel):
    """One robot action the LLM may propose and the adapter may receive."""

    action: str = Field(pattern=ACTION_NAME_RE.pattern)
    description: str = Field(default="", max_length=300)
    params: list[ParamDef] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> "ActionDef":
        names = [param.name for param in self.params]
        if len(names) != len(set(names)):
            raise ValueError(f"action '{self.action}': duplicate parameter names")
        return self

    def param(self, name: str) -> ParamDef | None:
        return next((p for p in self.params if p.name == name), None)

    def strict_step_schema(self) -> dict[str, Any]:
        """JSON schema for one step of this action (strict structured output)."""
        properties: dict[str, Any] = {"action": {"enum": [self.action]}}
        for param in self.params:
            properties[param.name] = _param_schema(param)
        return {
            "type": "object",
            "properties": properties,
            "required": ["action", *[p.name for p in self.params if p.required]],
            "additionalProperties": False,
        }


def _fmt_number(value: float) -> str:
    """Render whole numbers without a trailing '.0' (0.0 -> 0, 2.5 -> 2.5)."""
    return str(int(value)) if float(value).is_integer() else str(value)


def _param_schema(param: ParamDef) -> dict[str, Any]:
    schema: dict[str, Any]
    if param.type == "int":
        schema = {"type": "integer"}
    elif param.type == "float":
        schema = {"type": "number"}
    elif param.type == "bool":
        schema = {"type": "boolean"}
    else:
        schema = {"type": "string"}
    if param.min is not None:
        schema["minimum"] = param.min
    if param.max is not None:
        schema["maximum"] = param.max
    return schema


def _check_value(param: ParamDef, value: Any, *, context: str) -> None:
    """Type/range check of a value against a ParamDef; raises ProfileError."""
    kind = param.type
    valid: bool
    if kind == "int":
        valid = isinstance(value, int) and not isinstance(value, bool)
    elif kind == "float":
        valid = isinstance(value, (int, float)) and not isinstance(value, bool)
    elif kind == "bool":
        valid = isinstance(value, bool)
    else:
        valid = isinstance(value, str)
    if not valid:
        raise ProfileError(f"{context}: expected {kind}, got {type(value).__name__}")
    if kind in ("int", "float"):
        if param.min is not None and value < param.min:
            raise ProfileError(f"{context}: {value} below minimum {param.min}")
        if param.max is not None and value > param.max:
            raise ProfileError(f"{context}: {value} above maximum {param.max}")


class RobotMeta(StrictModel):
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=500)


class RobotProfile(StrictModel):
    """The full robot-specific control contract."""

    profile_version: Literal[1] = 1
    robot: RobotMeta
    actions: list[ActionDef] = Field(min_length=1, max_length=32)
    examples: list[dict[str, Any]] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def _check(self) -> "RobotProfile":
        names = [entry.action for entry in self.actions]
        if len(names) != len(set(names)):
            raise ProfileError(f"duplicate action names: {sorted(names)}")
        for index, example in enumerate(self.examples, start=1):
            self._check_example(index, example)
        return self

    def _check_example(self, index: int, example: dict[str, Any]) -> None:
        try:
            plan = PlanResponse.model_validate(example)
        except ValidationError as exc:
            location = ".".join(str(part) for part in exc.errors()[0]["loc"])
            raise ProfileError(
                f"example {index} is not a valid response envelope ({location or 'plan'}); "
                f"examples must look like exact answers: "
                f'{{"version": {SCHEMA_VERSION}, "mode": ..., "steps": [...]}}'
            ) from exc
        try:
            for step in plan.steps:
                self.check_step(step)
        except ProfileError as exc:
            raise ProfileError(f"example {index}: {exc}") from exc

    # -- lookups ---------------------------------------------------------------

    def action(self, name: str) -> ActionDef | None:
        return next((entry for entry in self.actions if entry.action == name), None)

    def action_names(self) -> list[str]:
        return [entry.action for entry in self.actions]

    def check_step(self, step: ActionStep) -> ActionStep:
        """Validate one step against this profile; return the normalized step.

        Unknown actions/parameters and out-of-range or wrong-typed values raise
        ProfileError. Missing optional parameters are filled from their
        declared defaults, so the adapter always receives complete commands.
        """
        action_def = self.action(step.action)
        if action_def is None:
            raise ProfileError(
                f"unknown action '{step.action}' for this robot "
                f"(available: {', '.join(self.action_names())})"
            )
        payload = step.model_dump()
        known = {"action", *(param.name for param in action_def.params)}
        unknown = sorted(set(payload) - known)
        if unknown:
            raise ProfileError(
                f"action '{step.action}' does not accept: {', '.join(unknown)}"
            )
        normalized: dict[str, Any] = {"action": step.action}
        for param in action_def.params:
            value = payload.get(param.name)
            if value is None:
                if param.required:
                    raise ProfileError(
                        f"action '{step.action}' requires parameter '{param.name}'"
                    )
                if param.default is not None:
                    value = param.default
                else:
                    continue
            else:
                try:
                    _check_value(param, value, context=f"action '{step.action}' param '{param.name}'")
                except ProfileError:
                    raise
            normalized[param.name] = value
        return ActionStep.model_validate(normalized)

    # -- LLM-facing renderings ---------------------------------------------------

    def prompt_section(self) -> str:
        """Action vocabulary for the system prompt."""
        lines: list[str] = []
        for entry in self.actions:
            head = '{"action": "' + entry.action + '"'
            params = ", ".join(f'"{param.name}": {param.prompt()}' for param in entry.params)
            line = "- " + (head + ", " + params if params else head) + "}"
            if entry.description:
                line += " — " + entry.description
            lines.append(line)
        return "\n".join(lines)

    def examples_section(self) -> str:
        """The example answers, verbatim — the LLM must mimic these."""
        return "\n".join(
            json.dumps(example, indent=2) for example in self.examples
        )

    def response_schema(self) -> dict[str, Any]:
        """OpenAI strict-mode JSON schema for the envelope + this robot's steps."""
        return {
            "type": "object",
            "properties": {
                "version": {"enum": [SCHEMA_VERSION]},
                "mode": {"enum": ["action", "plan", "clarification"]},
                "steps": {
                    "type": "array",
                    "items": {
                        "anyOf": [entry.strict_step_schema() for entry in self.actions]
                    },
                },
                "question": {"type": ["string", "null"]},
                "options": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["version", "mode", "steps", "question", "options"],
            "additionalProperties": False,
        }


def parse_profile(data: Any) -> RobotProfile:
    """Validate raw JSON (dict) into a RobotProfile; raises ProfileError."""
    try:
        return RobotProfile.model_validate(data)
    except ValidationError as exc:
        error = exc.errors()[0]
        location = ".".join(str(part) for part in error["loc"])
        raise ProfileError(f"{location or 'profile'}: {error['msg']}") from exc


class ProfileManager:
    """Holds the current robot profile; optional persistence to disk.

    `path=""` keeps the profile in memory only. Otherwise the profile is
    loaded at startup (if the file exists) and every upload overwrites it,
    so the profile survives restarts.
    """

    def __init__(self, path: str = "") -> None:
        self._path = Path(path) if path.strip() else None
        self._profile: RobotProfile | None = None
        if self._path is not None and self._path.is_file():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return  # startup must not die on a corrupt profile file
            try:
                self._profile = parse_profile(data)
            except ProfileError:
                pass  # caller can surface the problem via current() being None

    def current(self) -> RobotProfile | None:
        return self._profile

    def set(self, profile: RobotProfile) -> None:
        """Install a validated profile and persist it when a path is set."""
        self._profile = profile
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(profile.model_dump(), indent=2) + "\n", encoding="utf-8"
            )
        except OSError:
            pass  # persistence must never break an upload

    @property
    def path(self) -> Path | None:
        return self._path


def parse_do_action(args: list[str], profile: RobotProfile) -> ActionStep:
    """Parse a manual `/do` action positionally against the profile.

    `/do <action> <param1> <param2> ...` — values are mapped onto the
    profile-declared parameters in declaration order and type-checked.
    Raises ValueError with usage on bad input.
    """
    if not args:
        raise ValueError(
            "usage: /do <action> [param ...] — available actions: "
            + ", ".join(profile.action_names())
        )
    name, rest = args[0], args[1:]
    action_def = profile.action(name)
    if action_def is None:
        raise ValueError(
            f"unknown action {name!r} for this robot (available: {', '.join(profile.action_names())})"
        )
    given = len(rest)
    required = sum(1 for param in action_def.params if param.required)
    if not required <= given <= len(action_def.params):
        expected = (
            f"{required}-{len(action_def.params)}"
            if required != len(action_def.params)
            else str(len(action_def.params))
        )
        raise ValueError(
            f"action '{name}' takes {expected} parameter(s) "
            f"({', '.join(param.name for param in action_def.params) or 'none'})"
        )
    values: dict[str, Any] = {}
    for param, raw in zip(action_def.params, rest):
        try:
            if param.type == "int":
                value: Any = int(raw)
            elif param.type == "float":
                value = float(raw)
            elif param.type == "bool":
                value = raw.lower() in ("1", "true", "yes", "on")
            else:
                value = raw
        except ValueError as exc:
            raise ValueError(f"parameter '{param.name}' expects {param.type}, got {raw!r}") from exc
        values[param.name] = value
    try:
        return profile.check_step(ActionStep.model_validate({"action": name, **values}))
    except ProfileError as exc:
        raise ValueError(str(exc)) from exc

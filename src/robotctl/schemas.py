"""Typed schemas: the versioned LLM response contract and robot telemetry.

The response envelope is universal and fixed; the actions inside a step are
NOT — they come from the uploaded robot profile (see `profile.py`) and are
validated against it by the safety layer. Every contract model forbids
unknown fields at the envelope level, so anything the LLM adds beyond the
contract is rejected before it can ever reach the executor.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = 1

MotionStatus = Literal["idle", "moving", "turning", "stopped", "error"]


class StrictModel(BaseModel):
    """Base for all contract types: unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Action steps — universal frame, profile-defined payload.
# ---------------------------------------------------------------------------


class ActionStep(BaseModel):
    """One robot action: `action` plus the profile-declared parameters.

    This model deliberately does NOT pin the parameters (extra="allow"):
    what a `grab` or `move` step carries is defined by the robot profile.
    The safety layer validates every step against the profile before the
    executor may touch the adapter.
    """

    model_config = ConfigDict(extra="allow")

    action: str = Field(min_length=1, description="Action name from the robot profile.")

    def params(self) -> dict[str, object]:
        """The profile-defined parameters (everything except `action`)."""
        return {key: value for key, value in self.model_dump().items() if key != "action"}


# ---------------------------------------------------------------------------
# LLM response contract (version 1) — universal envelope
# ---------------------------------------------------------------------------


class PlanResponse(StrictModel):
    """The only structure the LLM is allowed to return.

    `steps` entries are profile-defined actions; the envelope itself
    (version/mode/question/options) is universal.
    """

    version: Literal[1] = 1
    mode: Literal["action", "plan", "clarification"]
    steps: list[ActionStep] = Field(default_factory=list, max_length=16)
    question: str | None = None
    options: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_mode(self) -> "PlanResponse":
        if self.mode == "clarification":
            if not self.question:
                raise ValueError("clarification mode requires a 'question'")
            if self.steps:
                raise ValueError("clarification mode must not contain steps")
        else:
            if not self.steps:
                raise ValueError(f"mode '{self.mode}' requires at least one step")
            if self.question is not None:
                raise ValueError("'question' is only allowed in clarification mode")
        return self


# ---------------------------------------------------------------------------
# Telemetry — universal minimal contract for real robots.
# ---------------------------------------------------------------------------


class Pose(StrictModel):
    x_m: float
    y_m: float
    heading_deg: float


class Telemetry(StrictModel):
    """Telemetry snapshot.

    Only `timestamp`, `monotonic`, `connected` and `motion_status` are
    required; everything else is optional because not every robot can
    provide it. The HTTP adapter maps the robot's telemetry endpoint
    onto this schema.
    """

    timestamp: float = Field(description="Unix time (wall clock) of the sample.")
    monotonic: float = Field(description="time.monotonic() of the sample; used for staleness.")
    connected: bool
    motion_status: MotionStatus
    pose: Pose | None = None
    linear_speed_mps: float | None = None
    angular_speed_dps: float | None = None
    battery_pct: float | None = Field(default=None, ge=0, le=100)
    obstacle_distance_m: float | None = Field(
        default=None, description="Front/range sensor reading, if the robot has one."
    )
    last_action_id: str | None = None


def describe_step(step: ActionStep) -> str:
    """Short human-readable rendering used in step results (robot-agnostic)."""
    params = step.params()
    if not params:
        return step.action
    rendered = " ".join(f"{key}={params[key]}" for key in sorted(params))
    return f"{step.action} {rendered}"

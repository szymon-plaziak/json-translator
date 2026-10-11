"""Deterministic safety layer.

Every plan — from the LLM or from manual `/do` commands — passes through here
before the executor may touch the adapter. What counts as a legal step is
defined entirely by the robot profile: unknown actions, unknown parameters,
wrong types and out-of-range values are rejected outright.
"""

from __future__ import annotations

from robotctl.config import Settings
from robotctl.profile import ProfileError, RobotProfile
from robotctl.schemas import ActionStep, PlanResponse


class SafetyViolation(Exception):
    """A plan violated the robot profile / configured limits; must not run."""


def validate_plan(
    plan: PlanResponse,
    profile: RobotProfile,
    settings: Settings,
) -> list[ActionStep]:
    """Validate a parsed plan against the profile; return normalized steps."""
    if plan.mode == "clarification":
        return []
    if not plan.steps:
        raise SafetyViolation("plan contains no steps")
    if plan.mode == "action" and len(plan.steps) != 1:
        raise SafetyViolation("mode 'action' must contain exactly one step")
    if len(plan.steps) > settings.max_plan_steps:
        raise SafetyViolation(
            f"plan has {len(plan.steps)} steps; limit is {settings.max_plan_steps}"
        )
    return [_check_step(step, profile) for step in plan.steps]


def _check_step(step: ActionStep, profile: RobotProfile) -> ActionStep:
    try:
        return profile.check_step(step)
    except ProfileError as exc:
        raise SafetyViolation(str(exc)) from exc


def requires_confirmation(plan: PlanResponse, settings: Settings) -> bool:
    """True when the plan contains a configured risky action."""
    if plan.mode == "clarification":
        return False
    return any(step.action in settings.risky_actions for step in plan.steps)

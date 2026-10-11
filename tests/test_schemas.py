"""Schema tests: the universal envelope rejects everything not explicitly allowed.

Action payloads are deliberately NOT validated here — they come from the
robot profile (see test_profile.py / test_executor.py). The envelope only
pins version, mode, step count and the clarification rules.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from robotctl.core import ActionStep, PlanResponse, describe_step


def plan_json(**overrides: object) -> dict:
    base: dict = {
        "version": 1,
        "mode": "action",
        "steps": [{"action": "move", "distance_m": 0.5, "speed_mps": 0.2}],
    }
    base.update(overrides)
    return base


def test_valid_action_plan_parses():
    plan = PlanResponse.model_validate(plan_json())
    assert plan.mode == "action"
    assert plan.steps[0].action == "move"
    assert plan.version == 1


def test_unknown_top_level_field_is_rejected():
    payload = plan_json(extra=1)
    with pytest.raises(ValidationError):
        PlanResponse.model_validate(payload)


def test_wrong_version_is_rejected():
    with pytest.raises(ValidationError):
        PlanResponse.model_validate({**plan_json(), "version": 2})


def test_action_mode_requires_steps():
    with pytest.raises(ValidationError):
        PlanResponse.model_validate({"version": 1, "mode": "action", "steps": []})


def test_clarification_requires_question():
    with pytest.raises(ValidationError):
        PlanResponse.model_validate({"version": 1, "mode": "clarification", "steps": []})


def test_clarification_must_not_carry_steps():
    payload = {
        "version": 1,
        "mode": "clarification",
        "question": "which way?",
        "steps": [{"action": "stop"}],
    }
    with pytest.raises(ValidationError):
        PlanResponse.model_validate(payload)


def test_action_step_is_profile_agnostic():
    """The envelope accepts ANY action shape; the profile validates it later."""
    payload = {"version": 1, "mode": "action", "steps": [{"action": "grab", "force": 30}]}
    plan = PlanResponse.model_validate(payload)
    assert plan.steps[0].action == "grab"
    assert plan.steps[0].params() == {"force": 30}


def test_action_step_requires_a_name():
    with pytest.raises(ValidationError):
        ActionStep.model_validate({"distance_m": 1.0})


def test_describe_step_renders_generic_details():
    assert describe_step(ActionStep.model_validate({"action": "stop"})) == "stop"
    assert (
        describe_step(ActionStep.model_validate({"action": "move", "distance_m": 0.5, "speed_mps": 0.25}))
        == "move distance_m=0.5 speed_mps=0.25"
    )


def test_optional_telemetry_fields_default_to_none():
    from robotctl.core import Telemetry

    snapshot = Telemetry(timestamp=1.0, monotonic=0.0, connected=True, motion_status="idle")
    assert snapshot.pose is None
    assert snapshot.battery_pct is None
    assert snapshot.obstacle_distance_m is None

"""Profile tests: validation, step checking, prompt/schema rendering, persistence."""

from __future__ import annotations

import json

import pytest

from robotctl.core import (
    ProfileError,
    ProfileManager,
    PlanResponse,
    parse_profile,
)

from helpers import make_profile, step


# -- profile validation ----------------------------------------------------------


def test_valid_profile_parses():
    profile = make_profile()
    assert profile.robot.name == "Test Rover"
    assert profile.action_names() == ["move", "turn", "wait"]
    assert profile.action("move") is not None
    assert profile.action("teleport") is None


@pytest.mark.parametrize(
    "mutation",
    [
        {"robot": {"name": ""}},
        {"actions": []},
        {"examples": []},
    ],
)
def test_profile_rejects_missing_essentials(mutation):
    data = json.loads(json.dumps(make_profile().model_dump()))
    data.update(mutation)
    with pytest.raises(ProfileError):
        parse_profile(data)


def test_profile_rejects_duplicate_actions():
    data = json.loads(json.dumps(make_profile().model_dump()))
    data["actions"].append(data["actions"][0])
    with pytest.raises(ProfileError, match="duplicate action"):
        parse_profile(data)


def test_profile_rejects_bad_param_type():
    data = json.loads(json.dumps(make_profile().model_dump()))
    data["actions"][0]["params"][0]["type"] = "list"
    with pytest.raises(ProfileError):
        parse_profile(data)


def test_profile_rejects_min_above_max():
    data = json.loads(json.dumps(make_profile().model_dump()))
    data["actions"][0]["params"][0]["min"] = 10
    data["actions"][0]["params"][0]["max"] = 0
    with pytest.raises(ProfileError, match="min"):
        parse_profile(data)


def test_profile_rejects_required_param_with_default():
    data = json.loads(json.dumps(make_profile().model_dump()))
    data["actions"][0]["params"][0]["required"] = True
    data["actions"][0]["params"][0]["default"] = 1.0
    with pytest.raises(ProfileError, match="required"):
        parse_profile(data)


def test_profile_rejects_default_outside_range():
    data = json.loads(json.dumps(make_profile().model_dump()))
    data["actions"][0]["params"][1]["default"] = 99.0  # speed_mps max is 2
    with pytest.raises(ProfileError, match="maximum"):
        parse_profile(data)


def test_profile_rejects_action_with_duplicate_params():
    data = json.loads(json.dumps(make_profile().model_dump()))
    data["actions"][0]["params"].append(dict(data["actions"][0]["params"][0]))
    with pytest.raises(ProfileError, match="duplicate parameter"):
        parse_profile(data)


# -- examples must be exactly-correct answers --------------------------------------


def test_example_must_be_a_valid_envelope():
    data = json.loads(json.dumps(make_profile().model_dump()))
    data["examples"] = [{"cmd": "move", "x": 1}]  # not the universal envelope
    with pytest.raises(ProfileError, match="example 1"):
        parse_profile(data)


def test_example_with_unknown_action_is_rejected():
    data = json.loads(json.dumps(make_profile().model_dump()))
    data["examples"] = [
        {"version": 1, "mode": "action", "steps": [{"action": "teleport", "distance_m": 1}]}
    ]
    with pytest.raises(ProfileError, match="unknown action"):
        parse_profile(data)


def test_example_with_out_of_range_param_is_rejected():
    data = json.loads(json.dumps(make_profile().model_dump()))
    data["examples"] = [
        {"version": 1, "mode": "action", "steps": [{"action": "move", "distance_m": 100}]}
    ]
    with pytest.raises(ProfileError, match="above maximum"):
        parse_profile(data)


# -- step checking (used by safety and the planner's retry loop) ---------------------


def test_check_step_fills_defaults():
    profile = make_profile()
    normalized = profile.check_step(step("move", distance_m=1.0))
    assert normalized.params() == {"distance_m": 1.0, "speed_mps": 0.5}


def test_check_step_rejects_unknown_action():
    with pytest.raises(ProfileError, match="unknown action 'teleport'"):
        make_profile().check_step(step("teleport", distance_m=1.0))


def test_check_step_rejects_unknown_param():
    with pytest.raises(ProfileError, match="does not accept"):
        make_profile().check_step(step("move", distance_m=1.0, warp=9))


def test_check_step_rejects_missing_required_param():
    with pytest.raises(ProfileError, match="requires parameter 'distance_m'"):
        make_profile().check_step(step("move"))


def test_check_step_rejects_wrong_type():
    with pytest.raises(ProfileError, match="expected float"):
        make_profile().check_step(step("move", distance_m="far"))


def test_check_step_rejects_bool_for_number():
    # bool is a subclass of int in Python: must not pass as a number
    with pytest.raises(ProfileError, match="expected float"):
        make_profile().check_step(step("move", distance_m=True))


def test_check_step_rejects_out_of_range():
    profile = make_profile()
    with pytest.raises(ProfileError, match="above maximum"):
        profile.check_step(step("move", distance_m=99.0))
    with pytest.raises(ProfileError, match="below minimum"):
        profile.check_step(step("turn", angle_deg=-360))


# -- LLM-facing renderings -----------------------------------------------------------


def test_prompt_section_lists_actions_and_limits():
    text = make_profile().prompt_section()
    assert '"action": "move"' in text
    assert '"distance_m"' in text
    assert "0..10" in text
    assert "default 0.5" in text
    assert "required" in text


def test_examples_section_is_verbatim_json():
    profile = make_profile()
    text = profile.examples_section()
    assert json.dumps(profile.examples[0], indent=2) in text


def test_response_schema_is_strict_and_covers_actions():
    schema = make_profile().response_schema()
    assert schema["additionalProperties"] is False
    assert sorted(schema["properties"]) == sorted(schema["required"])
    assert schema["properties"]["version"] == {"enum": [1]}
    assert schema["properties"]["mode"] == {"enum": ["action", "plan"]}
    items = schema["properties"]["steps"]["items"]
    action_names = [entry["properties"]["action"]["enum"][0] for entry in items["anyOf"]]
    assert action_names == ["move", "turn", "wait"]
    move = items["anyOf"][0]
    assert move["required"] == ["action", "distance_m"]  # speed_mps optional
    assert move["additionalProperties"] is False
    assert json.dumps(schema)  # serializable


# -- ProfileManager persistence -------------------------------------------------------


def test_manager_roundtrip_persists_profile(tmp_path):
    path = tmp_path / "robot_profile.json"
    manager = ProfileManager(str(path))
    assert manager.current() is None
    manager.set(make_profile())
    assert path.is_file()
    reloaded = ProfileManager(str(path))
    assert reloaded.current() is not None
    assert reloaded.current().robot.name == "Test Rover"


def test_manager_survives_corrupt_file(tmp_path):
    path = tmp_path / "robot_profile.json"
    path.write_text("{not json", encoding="utf-8")
    manager = ProfileManager(str(path))
    assert manager.current() is None  # startup continues without a profile


def test_manager_survives_invalid_profile_file(tmp_path):
    path = tmp_path / "robot_profile.json"
    path.write_text(json.dumps({"profile_version": 1}), encoding="utf-8")
    manager = ProfileManager(str(path))
    assert manager.current() is None


def test_manager_without_path_keeps_profile_in_memory():
    manager = ProfileManager("")
    manager.set(make_profile())
    assert manager.current() is not None
    assert manager.path is None


# -- envelope/profile flow --------------------------------------------------------------


def test_profile_steps_flow_through_envelope():
    """End-to-end sanity: an example envelope re-parses and re-validates."""
    profile = make_profile()
    for example in profile.examples:
        plan = PlanResponse.model_validate(example)
        for step_ in plan.steps:
            assert profile.check_step(step_) is not None

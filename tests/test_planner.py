"""Planner tests: strict parsing, profile-driven prompts, retries, fallbacks."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from robotctl.core import MAX_PLAN_STEPS, Pose, ProfileManager, Settings, Telemetry, parse_profile
from robotctl.runtime import LLMPlanner, PlannerError, parse_plan_response

from helpers import make_profile, make_profiles

GOOD = '{"version": 1, "mode": "action", "steps": [{"action": "turn", "angle_deg": -10, "speed_dps": 30}]}'


class FakeLLM:
    """httpx-client double: script items are content strings, a
    (content, reasoning_content) tuple, or an Exception to raise."""

    def __init__(self, script: list[str | tuple[str, str | None] | Exception]) -> None:
        self._script = list(script)
        self.calls: list[dict[str, Any]] = []

    async def post(self, url: str, json: dict[str, Any] | None = None):
        self.calls.append(json)
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        content, reasoning = item if isinstance(item, tuple) else (item, None)
        body = {"choices": [{"message": {"content": content, "reasoning_content": reasoning}}]}
        return SimpleNamespace(status_code=200, json=lambda: body)


class TelemetryStub:
    """Adapter double that only serves a canned telemetry snapshot."""

    def __init__(self, snapshot: Telemetry | None) -> None:
        self.snapshot = snapshot

    async def fetch_telemetry(self) -> Telemetry | None:
        return self.snapshot


def make_planner(
    settings: Settings,
    script: list[str | tuple[str, str | None] | Exception],
    profiles: ProfileManager | None = None,
    adapter: Any | None = None,
) -> tuple[LLMPlanner, FakeLLM]:
    client = FakeLLM(script)
    planner = LLMPlanner(
        settings,
        profiles if profiles is not None else make_profiles(),
        adapter=adapter,
        client=client,
    )
    return planner, client


# -- local parsing ---------------------------------------------------------------


def test_parse_plain_json():
    plan = parse_plan_response(GOOD)
    assert plan.mode == "action"
    assert plan.steps[0].params()["angle_deg"] == -10


def test_parse_fenced_json():
    plan = parse_plan_response(f"```json\n{GOOD}\n```")
    assert plan.steps[0].action == "turn"


def test_parse_json_with_surrounding_prose():
    plan = parse_plan_response(f"Here you go:\n{GOOD}\nDone.")
    assert plan.steps[0].action == "turn"


def test_parse_rejects_prose_without_json():
    with pytest.raises(PlannerError):
        parse_plan_response("I cannot do that.")


def test_parse_rejects_broken_json():
    with pytest.raises(PlannerError, match="invalid JSON"):
        parse_plan_response('{"version": 1, "mode": "action", "steps": [}')


def test_parse_rejects_unknown_top_level_fields():
    """A model trying to sneak in question/options gets corrected, not obeyed."""
    payload = '{"version": 1, "mode": "action", "steps": [], "question": "how far?"}'
    with pytest.raises(PlannerError, match="schema violation"):
        parse_plan_response(payload)


def test_parse_without_profile_accepts_any_action_shape():
    """Without a profile the envelope accepts any step; the profile decides."""
    payload = '{"version": 1, "mode": "action", "steps": [{"action": "grab", "force": 30}]}'
    plan = parse_plan_response(payload)
    assert plan.steps[0].action == "grab"


def test_parse_with_profile_rejects_unknown_action():
    payload = '{"version": 1, "mode": "action", "steps": [{"action": "teleport", "distance_m": 1}]}'
    with pytest.raises(PlannerError, match="unknown action 'teleport'"):
        parse_plan_response(payload, make_profile())


def test_parse_with_profile_rejects_out_of_range():
    payload = '{"version": 1, "mode": "action", "steps": [{"action": "move", "distance_m": 99}]}'
    with pytest.raises(PlannerError, match="above maximum"):
        parse_plan_response(payload, make_profile())


def test_parse_with_profile_rejects_extra_step_fields():
    bad = (
        '{"version": 1, "mode": "action", '
        '"steps": [{"action": "move", "distance_m": 0.5, "speed_mps": 0.2, "cmd": "ls"}]}'
    )
    with pytest.raises(PlannerError, match="does not accept"):
        parse_plan_response(bad, make_profile())


# -- planner behaviour ---------------------------------------------------------------


async def test_planner_returns_validated_plan(settings):
    planner, client = make_planner(settings, [GOOD])
    plan = await planner.plan("turn slightly left")
    assert plan.steps[0].action == "turn"
    call = client.calls[0]
    assert call["model"] == settings.llm_model
    assert "response_format" not in call  # llm_response_format="none" in the fixture


async def test_planner_without_profile_fails_cleanly(settings):
    planner, client = make_planner(settings, [GOOD], profiles=ProfileManager(""))
    with pytest.raises(PlannerError, match="no robot profile"):
        await planner.plan("go forward")
    assert client.calls == []  # the LLM was never consulted


async def test_planner_retries_after_invalid_output():
    settings = Settings(LLM_RESPONSE_FORMAT="json_schema")
    planner, client = make_planner(settings, ["no json here", GOOD])
    plan = await planner.plan("turn slightly left")
    assert plan.mode == "action"
    assert len(client.calls) == 2
    retry_prompt = client.calls[1]["messages"][-1]["content"]
    assert "Invalid response" in retry_prompt


async def test_planner_fails_after_retries():
    settings = Settings(LLM_RESPONSE_FORMAT="json_schema")
    planner, client = make_planner(settings, ["bad", "still bad", "worse"])
    with pytest.raises(PlannerError):
        await planner.plan("go forward")
    assert len(client.calls) == 3  # one attempt per response-format variant


async def test_json_schema_is_generated_from_profile():
    settings = Settings(LLM_RESPONSE_FORMAT="json_schema")
    planner, client = make_planner(settings, [GOOD])
    await planner.plan("turn slightly left")
    response_format = client.calls[0]["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    schema = response_format["json_schema"]["schema"]
    assert "steps" in schema["properties"]
    assert "question" not in schema["properties"]
    any_of = schema["properties"]["steps"]["items"]["anyOf"]
    assert [entry["properties"]["action"]["enum"][0] for entry in any_of] == ["move", "turn", "wait"]


async def test_fallback_from_json_schema_to_json_object():
    settings = Settings(LLM_RESPONSE_FORMAT="json_schema")
    script: list[str | Exception] = [
        RuntimeError("response_format json_schema is not supported"),
        GOOD,
    ]
    planner, client = make_planner(settings, script)
    plan = await planner.plan("turn slightly left")
    assert plan.mode == "action"
    assert client.calls[1]["response_format"] == {"type": "json_object"}


async def test_telemetry_snapshot_is_included_in_prompt(settings):
    snapshot = Telemetry(
        timestamp=1.0,
        monotonic=0.0,
        connected=True,
        pose=Pose(x_m=1.25, y_m=-0.5, heading_deg=45.0),
        linear_speed_mps=0.0,
        angular_speed_dps=0.0,
        battery_pct=80.0,
        obstacle_distance_m=0.9,
        motion_status="idle",
        last_action_id=None,
    )
    planner, client = make_planner(settings, [GOOD], adapter=TelemetryStub(snapshot))
    await planner.plan("turn slightly left")
    system = client.calls[0]["messages"][0]["content"]
    assert "1.25" in system  # pose x
    assert "0.9" in system  # obstacle distance
    assert "45.0" in system  # heading
    assert "never invent" in system.lower()


async def test_optional_telemetry_renders_as_na(settings):
    snapshot = Telemetry(timestamp=1.0, monotonic=0.0, connected=True, motion_status="idle")
    planner, client = make_planner(settings, [GOOD], adapter=TelemetryStub(snapshot))
    await planner.plan("turn slightly left")
    system = client.calls[0]["messages"][0]["content"]
    assert "n/a" in system  # pose/battery/obstacle absent -> rendered, not crashed


async def test_unreachable_telemetry_renders_unavailable(settings):
    planner, client = make_planner(settings, [GOOD], adapter=TelemetryStub(None))
    await planner.plan("turn slightly left")
    system = client.calls[0]["messages"][0]["content"]
    assert "unavailable" in system


async def test_profile_actions_and_examples_are_in_the_prompt(settings):
    client = FakeLLM([GOOD])
    profiles = make_profiles()
    planner = LLMPlanner(settings, profiles, adapter=None, client=client)
    await planner.plan("turn slightly left")
    system = client.calls[0]["messages"][0]["content"]
    assert "Test Rover" in system  # robot name from the profile
    assert '"action": "move"' in system  # profile action vocabulary
    assert '"action": "turn"' in system
    assert "0..10" in system  # move distance limits from the profile
    assert '"angle_deg": 90' in system  # the example answer, verbatim
    assert str(MAX_PLAN_STEPS) in system


async def test_robot_from_profile_appears_in_prompt(settings):
    profile = parse_profile(
        {
            "profile_version": 1,
            "robot": {"name": "Crane-9", "description": "6-axis crane arm"},
            "actions": [
                {"action": "lift", "params": [{"name": "meters", "type": "float", "min": 0, "max": 5, "required": True}]}
            ],
            "examples": [
                {"version": 1, "mode": "action", "steps": [{"action": "lift", "meters": 1.0}]}
            ],
        }
    )
    client = FakeLLM(['{"version": 1, "mode": "action", "steps": [{"action": "lift", "meters": 2.0}]}'])
    planner = LLMPlanner(settings, make_profiles(profile), adapter=None, client=client)
    plan = await planner.plan("lift it up")
    assert plan.steps[0].action == "lift"
    system = client.calls[0]["messages"][0]["content"]
    assert "Crane-9" in system
    assert "6-axis crane arm" in system
    assert '"action": "lift"' in system


# -- guessing instead of asking ---------------------------------------------------------


async def test_prompt_tells_the_model_to_guess_never_ask(settings):
    planner, client = make_planner(settings, [GOOD])
    await planner.plan("turn slightly left")
    system = client.calls[0]["messages"][0]["content"]
    assert "pick conservative values" in system  # the guessing rule is present
    assert "do not ask" in system
    assert "clarification" not in system.lower()
    assert '"question"' not in system


# -- thinking ------------------------------------------------------------------------


THINK_OPEN = "<" + "think>"
THINK_CLOSE = "</" + "think>"


async def test_planner_strips_thinking(settings):
    content = THINK_OPEN + "user wants a small left turn" + THINK_CLOSE + " " + GOOD
    planner, _client = make_planner(settings, [content])
    plan = await planner.plan("turn slightly left")
    assert plan.steps[0].action == "turn"


async def test_planner_still_parses_when_thinking_is_unterminated(settings):
    # a reasoning model left the think-block open: the tail must not break parsing
    content = GOOD + " " + THINK_OPEN + "deliberating forever"
    planner, _client = make_planner(settings, [content])
    plan = await planner.plan("turn slightly left")
    assert plan.mode == "action"


async def test_extra_body_sent_then_dropped_when_server_rejects_it():
    settings = Settings(
        llm_response_format="none",
        llm_extra_body='{"chat_template_kwargs": {"enable_thinking": false}}',
    )
    planner, client = make_planner(settings, [RuntimeError("unknown field: chat_template_kwargs"), GOOD])
    plan = await planner.plan("turn slightly left")
    assert plan.mode == "action"
    calls = client.calls
    assert calls[0]["chat_template_kwargs"] == {"enable_thinking": False}
    assert "chat_template_kwargs" not in calls[1]


def test_invalid_extra_body_raises_clear_error(settings):
    settings.llm_extra_body = "{not json"
    with pytest.raises(ValueError, match="LLM_EXTRA_BODY"):
        LLMPlanner(settings, make_profiles(), client=FakeLLM([GOOD]))


# -- contract checks with auto-retry (mode/step-count mismatches) ----------------------


TWO_STEP_ACTION = (
    '{"version": 1, "mode": "action", "steps": ['
    '{"action": "turn", "angle_deg": 90, "speed_dps": 45}, '
    '{"action": "turn", "angle_deg": 90, "speed_dps": 45}]}'
)
EMPTY_PLAN = '{"version": 1, "mode": "plan", "steps": []}'
DETOUR_PLAN = (
    '{"version": 1, "mode": "plan", "steps": ['
    '{"action": "turn", "angle_deg": 90, "speed_dps": 45}, '
    '{"action": "move", "distance_m": 0.6, "speed_mps": null}]}'
)


def test_parse_rejects_action_mode_with_two_steps():
    with pytest.raises(PlannerError, match="set mode to 'plan'"):
        parse_plan_response(TWO_STEP_ACTION)


def test_parse_rejects_plan_mode_without_steps():
    with pytest.raises(PlannerError, match="requires at least one step"):
        parse_plan_response(EMPTY_PLAN)


async def test_planner_retries_when_action_mode_has_multiple_steps():
    """Regression: a 180-degree turn as two 90-degree steps used to be parsed
    fine and then REJECTED by safety, killing the command."""
    settings = Settings(LLM_RESPONSE_FORMAT="json_schema")
    planner, client = make_planner(settings, [TWO_STEP_ACTION, DETOUR_PLAN])
    plan = await planner.plan("turn around")
    assert plan.mode == "plan"
    feedback = client.calls[1]["messages"][-1]["content"]
    assert "set mode to 'plan'" in feedback


async def test_planner_retries_when_action_is_unknown_to_robot():
    """A robot-specific invalid action is corrected inside the retry loop."""
    settings = Settings(LLM_RESPONSE_FORMAT="json_schema")
    script = [
        '{"version": 1, "mode": "action", "steps": [{"action": "fly", "distance_m": 1}]}',
        GOOD,
    ]
    planner, client = make_planner(settings, script)
    plan = await planner.plan("go forward")
    assert plan.steps[0].action == "turn"
    feedback = client.calls[1]["messages"][-1]["content"]
    assert "unknown action" in feedback

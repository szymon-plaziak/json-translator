"""Planner tests: strict parsing, profile-driven prompts, retries, fallbacks."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from robotctl.config import Settings
from robotctl.planner import LLMPlanner, PlanParseError, PlannerError, parse_plan_response
from robotctl.profile import ProfileManager, parse_profile
from robotctl.schemas import Pose, Telemetry

from helpers import make_profile, make_profiles

GOOD = '{"version": 1, "mode": "action", "steps": [{"action": "turn", "angle_deg": -10, "speed_dps": 30}]}'


class FakeCompletions:
    """Scripted chat.completions.create: script items are str content, a
    (content, reasoning_content) tuple, or an Exception to raise."""

    def __init__(self, script: list[str | tuple[str, str | None] | Exception]) -> None:
        self._script = list(script)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any):
        self.calls.append(kwargs)
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, tuple):
            content, reasoning = item
        else:
            content, reasoning = item, None
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content, reasoning_content=reasoning))]
        )


class FakeClient:
    def __init__(self, script: list[str | tuple[str, str | None] | Exception]) -> None:
        self.chat = SimpleNamespace(completions=FakeCompletions(script))


def make_planner(
    settings: Settings,
    script: list[str | Exception],
    profiles: ProfileManager | None = None,
) -> tuple[LLMPlanner, FakeClient]:
    client = FakeClient(script)
    planner = LLMPlanner(
        settings,
        telemetry_provider=lambda: None,
        profiles=profiles if profiles is not None else make_profiles(),
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
    with pytest.raises(PlanParseError):
        parse_plan_response("I cannot do that.")


def test_parse_rejects_broken_json():
    with pytest.raises(PlanParseError, match="invalid JSON"):
        parse_plan_response('{"version": 1, "mode": "action", "steps": [}')


def test_parse_without_profile_accepts_any_action_shape():
    """Without a profile the envelope accepts any step; the profile decides."""
    payload = '{"version": 1, "mode": "action", "steps": [{"action": "grab", "force": 30}]}'
    plan = parse_plan_response(payload)
    assert plan.steps[0].action == "grab"


def test_parse_with_profile_rejects_unknown_action():
    payload = '{"version": 1, "mode": "action", "steps": [{"action": "teleport", "distance_m": 1}]}'
    with pytest.raises(PlanParseError, match="unknown action 'teleport'"):
        parse_plan_response(payload, make_profile())


def test_parse_with_profile_rejects_out_of_range():
    payload = '{"version": 1, "mode": "action", "steps": [{"action": "move", "distance_m": 99}]}'
    with pytest.raises(PlanParseError, match="above maximum"):
        parse_plan_response(payload, make_profile())


def test_parse_with_profile_rejects_extra_step_fields():
    bad = (
        '{"version": 1, "mode": "action", '
        '"steps": [{"action": "move", "distance_m": 0.5, "speed_mps": 0.2, "cmd": "ls"}]}'
    )
    with pytest.raises(PlanParseError, match="does not accept"):
        parse_plan_response(bad, make_profile())


# -- planner behaviour ---------------------------------------------------------------


async def test_planner_returns_validated_plan(settings):
    planner, client = make_planner(settings, [GOOD])
    plan = await planner.plan("turn slightly left")
    assert plan.steps[0].action == "turn"
    call = client.chat.completions.calls[0]
    assert call["model"] == settings.llm_model
    assert "response_format" not in call  # llm_response_format="none" in the fixture


async def test_planner_without_profile_fails_cleanly(settings):
    planner, _client = make_planner(settings, [GOOD], profiles=ProfileManager(""))
    with pytest.raises(PlannerError, match="no robot profile"):
        await planner.plan("go forward")


async def test_planner_retries_after_invalid_output(settings):
    planner, client = make_planner(settings, ["no json here", GOOD])
    plan = await planner.plan("turn slightly left")
    assert plan.mode == "action"
    assert len(client.chat.completions.calls) == 2
    retry_prompt = client.chat.completions.calls[1]["messages"][-1]["content"]
    assert "Invalid response" in retry_prompt


async def test_planner_fails_after_retries(settings):
    planner, _ = make_planner(settings, ["bad", "still bad"])
    with pytest.raises(PlannerError):
        await planner.plan("go forward")


async def test_json_schema_is_generated_from_profile():
    settings = Settings(_env_file=None, llm_response_format="json_schema")
    planner, client = make_planner(settings, [GOOD])
    await planner.plan("turn slightly left")
    response_format = client.chat.completions.calls[0]["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    schema = response_format["json_schema"]["schema"]
    assert "steps" in schema["properties"]
    any_of = schema["properties"]["steps"]["items"]["anyOf"]
    assert [entry["properties"]["action"]["enum"][0] for entry in any_of] == ["move", "turn", "wait"]


async def test_fallback_from_json_schema_to_json_object():
    settings = Settings(_env_file=None, llm_response_format="json_schema")
    script: list[str | Exception] = [
        RuntimeError("response_format json_schema is not supported"),
        GOOD,
    ]
    planner, client = make_planner(settings, script)
    plan = await planner.plan("turn slightly left")
    assert plan.mode == "action"
    assert client.chat.completions.calls[1]["response_format"] == {"type": "json_object"}


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
    client = FakeClient([GOOD])
    planner = LLMPlanner(
        settings, telemetry_provider=lambda: snapshot, profiles=make_profiles(), client=client
    )
    await planner.plan("turn slightly left")
    system = client.chat.completions.calls[0]["messages"][0]["content"]
    assert "1.25" in system  # pose x
    assert "0.9" in system  # obstacle distance
    assert "45.0" in system  # heading
    assert "never invent" in system.lower()


async def test_optional_telemetry_renders_as_na(settings):
    snapshot = Telemetry(timestamp=1.0, monotonic=0.0, connected=True, motion_status="idle")
    client = FakeClient([GOOD])
    planner = LLMPlanner(settings, telemetry_provider=lambda: snapshot, profiles=make_profiles(), client=client)
    await planner.plan("turn slightly left")
    system = client.chat.completions.calls[0]["messages"][0]["content"]
    assert "n/a" in system  # pose/battery/obstacle absent -> rendered, not crashed


async def test_profile_actions_and_examples_are_in_the_prompt(settings):
    client = FakeClient([GOOD])
    profiles = make_profiles()
    planner = LLMPlanner(settings, telemetry_provider=lambda: None, profiles=profiles, client=client)
    await planner.plan("turn slightly left")
    system = client.chat.completions.calls[0]["messages"][0]["content"]
    assert "Test Rover" in system  # robot name from the profile
    assert '"action": "move"' in system  # profile action vocabulary
    assert '"action": "turn"' in system
    assert "0..10" in system  # move distance limits from the profile
    assert '"angle_deg": 90' in system  # the example answer, verbatim
    assert str(settings.max_plan_steps) in system


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
    client = FakeClient(['{"version": 1, "mode": "action", "steps": [{"action": "lift", "meters": 2.0}]}'])
    planner = LLMPlanner(settings, telemetry_provider=lambda: None, profiles=make_profiles(profile), client=client)
    plan = await planner.plan("lift it up")
    assert plan.steps[0].action == "lift"
    system = client.chat.completions.calls[0]["messages"][0]["content"]
    assert "Crane-9" in system
    assert "6-axis crane arm" in system
    assert '"action": "lift"' in system


# -- thinking + interaction log ---------------------------------------------------------


THINK_OPEN = "<" + "think>"
THINK_CLOSE = "</" + "think>"


async def test_planner_strips_thinking_and_records_it(settings):
    content = THINK_OPEN + "user wants a small left turn" + THINK_CLOSE + " " + GOOD
    planner, _client = make_planner(settings, [content])
    plan = await planner.plan("turn slightly left")
    assert plan.steps[0].action == "turn"
    entry = planner.entries()[0]
    assert entry.thinking == "user wants a small left turn"
    assert entry.raw_answer == GOOD
    assert entry.parsed is not None and entry.parsed.mode == "action"
    assert entry.attempts == 1
    assert entry.error is None
    assert entry.model == settings.llm_model


async def test_planner_records_reasoning_content_field(settings):
    planner, _client = make_planner(settings, [(GOOD, "hidden chain of thought")])
    await planner.plan("turn slightly left")
    entry = planner.entries()[0]
    assert entry.thinking == "hidden chain of thought"
    assert entry.raw_answer == GOOD


async def test_planner_no_thinking_entry_when_reply_is_clean(settings):
    planner, _client = make_planner(settings, [GOOD])
    await planner.plan("turn slightly left")
    assert planner.entries()[0].thinking is None


async def test_planner_still_parses_when_thinking_is_on_despite_config(settings):
    # thinking enabled server-side; the think-tag block must not break parsing
    content = THINK_OPEN + "deliberating" + THINK_CLOSE + GOOD
    planner, _client = make_planner(settings, [content])
    plan = await planner.plan("turn slightly left")
    assert plan.mode == "action"


async def test_extra_body_sent_then_dropped_when_server_rejects_it():
    settings = Settings(
        _env_file=None,
        llm_response_format="none",
        llm_extra_body='{"chat_template_kwargs": {"enable_thinking": false}}',
    )
    planner, client = make_planner(settings, [RuntimeError("unknown field: chat_template_kwargs"), GOOD])
    plan = await planner.plan("turn slightly left")
    assert plan.mode == "action"
    calls = client.chat.completions.calls
    assert calls[0]["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    assert "extra_body" not in calls[1]
    entry = planner.entries()[0]
    assert entry.error is None


def test_invalid_extra_body_raises_clear_error(settings):
    settings.llm_extra_body = "{not json"
    with pytest.raises(ValueError, match="LLM_EXTRA_BODY"):
        LLMPlanner(
            settings,
            telemetry_provider=lambda: None,
            profiles=make_profiles(),
            client=FakeClient([GOOD]),
        )


async def test_planner_appends_interactions_to_log_file(settings, tmp_path):
    log_file = tmp_path / "llm.jsonl"
    settings.llm_log_file = str(log_file)
    planner, _client = make_planner(settings, [GOOD])
    await planner.plan("turn slightly left")
    lines = log_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["command"] == "turn slightly left"
    assert data["raw_answer"] == GOOD
    assert data["parsed"]["mode"] == "action"
    assert data["duration_s"] >= 0.0


async def test_planner_logs_failed_interaction(settings, tmp_path):
    log_file = tmp_path / "llm.jsonl"
    settings.llm_log_file = str(log_file)
    planner, _client = make_planner(settings, ["bad", "still bad"])
    with pytest.raises(PlannerError):
        await planner.plan("go forward")
    data = json.loads(log_file.read_text(encoding="utf-8").strip().splitlines()[0])
    assert data["parsed"] is None
    assert "schema violation" in (data["error"] or "") or "JSON" in (data["error"] or "")
    assert data["attempts"] == 2


# -- clarification carry-over (multi-turn memory) -------------------------------------------


CLARIFY = (
    '{"version": 1, "mode": "clarification", "steps": [], '
    '"question": "Which direction?", "options": ["left", "right"]}'
)
CLARIFY_NO_OPTIONS = (
    '{"version": 1, "mode": "clarification", "steps": [], '
    '"question": "How far?", "options": []}'
)
DETOUR_PLAN = (
    '{"version": 1, "mode": "plan", "steps": ['
    '{"action": "turn", "angle_deg": 90, "speed_dps": 45}, '
    '{"action": "move", "distance_m": 0.6, "speed_mps": null}], '
    '"question": null, "options": []}'
)


def _first_user_message(client: FakeClient, call: int) -> str:
    messages = client.chat.completions.calls[call]["messages"]
    return [m for m in messages if m["role"] == "user"][0]["content"]


async def test_clarification_context_is_carried_into_next_call(settings):
    planner, client = make_planner(settings, [CLARIFY, DETOUR_PLAN])
    first = await planner.plan("go forward and move around the obstacle")
    assert first.mode == "clarification"
    second = await planner.plan("left")
    assert second.mode == "plan"
    user = _first_user_message(client, 1)
    assert user.endswith("User reply: left")
    assert "Which direction?" in user
    assert "options: left, right" in user
    assert "go forward and move around the obstacle" in user
    assert planner.entries()[0].clarification_context is False
    assert planner.entries()[1].clarification_context is True


async def test_clarification_context_cleared_after_resolution(settings):
    planner, client = make_planner(settings, [CLARIFY, DETOUR_PLAN, GOOD])
    await planner.plan("go around the obstacle")
    await planner.plan("left")  # resolves the clarification
    await planner.plan("turn slightly left")  # fresh command: no context
    assert _first_user_message(client, 2) == "turn slightly left"
    assert planner.entries()[2].clarification_context is False


async def test_clarification_chains_keep_original_command_and_history(settings):
    planner, client = make_planner(settings, [CLARIFY, CLARIFY_NO_OPTIONS, DETOUR_PLAN])
    await planner.plan("go around the obstacle")  # asks direction
    await planner.plan("left")  # asks distance; context includes Q1/A1
    third = await planner.plan("0.5 meters")  # resolves; context has both exchanges
    assert third.mode == "plan"
    user = _first_user_message(client, 2)
    assert 'you asked the clarification "How far?"' in user
    assert 'you asked "Which direction?" and the user replied "left"' in user
    assert '"go around the obstacle"' in user  # original command preserved
    assert "User reply: 0.5 meters" in user


async def test_clarification_context_survives_failed_attempt(settings):
    planner, client = make_planner(settings, [CLARIFY, "bad", "worse", DETOUR_PLAN])
    await planner.plan("go around the obstacle")
    with pytest.raises(PlannerError):
        await planner.plan("left")  # both attempts invalid -> PlannerError
    plan = await planner.plan("left")  # retry must still carry the context
    assert plan.mode == "plan"
    assert "Which direction?" in _first_user_message(client, 3)
    assert planner.entries()[2].clarification_context is True


# -- contract checks with auto-retry (mode/step-count mismatches) ----------------------


TWO_STEP_ACTION = (
    '{"version": 1, "mode": "action", "steps": ['
    '{"action": "turn", "angle_deg": 90, "speed_dps": 45}, '
    '{"action": "turn", "angle_deg": 90, "speed_dps": 45}], '
    '"question": null, "options": []}'
)
EMPTY_PLAN = '{"version": 1, "mode": "plan", "steps": [], "question": null, "options": []}'


def test_parse_rejects_action_mode_with_two_steps():
    with pytest.raises(PlanParseError, match="set mode to 'plan'"):
        parse_plan_response(TWO_STEP_ACTION)


def test_parse_rejects_plan_mode_without_steps():
    with pytest.raises(PlanParseError, match="requires at least one step"):
        parse_plan_response(EMPTY_PLAN)


async def test_planner_retries_when_action_mode_has_multiple_steps(settings):
    """Regression: a 180-degree turn as two 90-degree steps used to be parsed
    fine and then REJECTED by safety, killing the command."""
    planner, client = make_planner(settings, [TWO_STEP_ACTION, DETOUR_PLAN])
    plan = await planner.plan("turn around")
    assert plan.mode == "plan"
    assert planner.entries()[0].attempts == 2
    feedback = client.chat.completions.calls[1]["messages"][-1]["content"]
    assert "set mode to 'plan'" in feedback


async def test_planner_retries_when_action_is_unknown_to_robot(settings):
    """A robot-specific invalid action is corrected inside the retry loop."""
    script = [
        '{"version": 1, "mode": "action", "steps": [{"action": "fly", "distance_m": 1}]}',
        GOOD,
    ]
    planner, client = make_planner(settings, script)
    plan = await planner.plan("go forward")
    assert plan.steps[0].action == "turn"
    feedback = client.chat.completions.calls[1]["messages"][-1]["content"]
    assert "unknown action" in feedback
    assert planner.entries()[0].attempts == 2

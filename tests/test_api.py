"""API tests: endpoints, profile upload, risky confirmation, /do, /logs."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx

from robotctl.runtime import LLMPlanner
from robotctl.service import create_app

from helpers import (
    FakeAdapter,
    FakePlanner,
    action_plan,
    make_profiles,
    multi_plan,
    stack,
)

PROFILE_JSON = {
    "profile_version": 1,
    "robot": {"name": "Crane-9", "description": "6-axis crane arm"},
    "actions": [
        {
            "action": "lift",
            "description": "raise the hook",
            "params": [{"name": "meters", "type": "float", "min": 0, "max": 5, "required": True}],
        },
        {"action": "hold", "params": []},
    ],
    "examples": [{"version": 1, "mode": "action", "steps": [{"action": "lift", "meters": 1.0}]}],
}


def app_client(executor, monitor, settings, planner=None, profiles=None):
    app = create_app(executor, monitor, settings, planner, profiles)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_full_command_lifecycle(settings):
    plan = multi_plan(
        {"action": "move", "distance_m": 0.1, "speed_mps": 1.0},
        {"action": "wait", "seconds": 0.05},
    )
    async with stack(settings, FakePlanner(plan)) as (executor, _adapter, monitor):
        profiles = make_profiles()
        async with app_client(executor, monitor, settings, FakePlanner(plan), profiles) as client:
            response = await client.post("/commands", json={"command": "go and pause"})
            assert response.status_code == 202
            command_id = response.json()["id"]

            body = None
            for _ in range(100):
                body = (await client.get(f"/commands/{command_id}")).json()
                if body["status"] in {"completed", "failed", "rejected", "stopped"}:
                    break
                await asyncio.sleep(0.02)
            assert body is not None and body["status"] == "completed"
            assert [step["status"] for step in body["steps"]] == ["done", "done"]


async def test_commands_require_a_profile(settings):
    async with stack(settings, FakePlanner()) as (executor, _adapter, monitor):
        async with app_client(executor, monitor, settings, None, make_profiles()) as client:
            pass
    # separate app with an EMPTY profile manager
    async with stack(settings, FakePlanner()) as (executor, _adapter, monitor):
        from robotctl.core import ProfileManager

        async with app_client(executor, monitor, settings, None, ProfileManager("")) as client:
            response = await client.post("/commands", json={"command": "go"})
            assert response.status_code == 422
            assert "no robot profile" in response.json()["detail"]


async def test_profile_upload_and_roundtrip(settings):
    async with stack(settings, FakePlanner()) as (executor, _adapter, monitor):
        profiles = make_profiles()
        async with app_client(executor, monitor, settings, None, profiles) as client:
            # replace the default test profile with the crane one
            response = await client.post("/profile", json=PROFILE_JSON)
            assert response.status_code == 200
            assert response.json() == {
                "ok": True,
                "robot": "Crane-9",
                "actions": ["lift", "hold"],
                "persisted": False,
            }
            fetched = (await client.get("/profile")).json()
            assert fetched["robot"]["name"] == "Crane-9"
            assert profiles.current().action_names() == ["lift", "hold"]


async def test_profile_upload_rejects_invalid_json(settings):
    async with stack(settings, FakePlanner()) as (executor, _adapter, monitor):
        profiles = make_profiles()
        async with app_client(executor, monitor, settings, None, profiles) as client:
            bad = {**PROFILE_JSON, "actions": []}
            response = await client.post("/profile", json=bad)
            assert response.status_code == 422

            bad_example = {**PROFILE_JSON, "examples": [{"steps": "nope"}]}
            response = await client.post("/profile", json=bad_example)
            assert response.status_code == 422
            assert "example 1" in response.json()["detail"]

            # the previously loaded profile is untouched
            assert (await client.get("/profile")).json()["robot"]["name"] == "Test Rover"


async def test_profile_get_404_when_empty(settings):
    async with stack(settings, FakePlanner()) as (executor, _adapter, monitor):
        from robotctl.core import ProfileManager

        async with app_client(executor, monitor, settings, None, ProfileManager("")) as client:
            response = await client.get("/profile")
            assert response.status_code == 404


async def test_invalid_command_body_is_422(settings):
    async with stack(settings, FakePlanner()) as (executor, _adapter, monitor):
        async with app_client(executor, monitor, settings, None, make_profiles()) as client:
            response = await client.post("/commands", json={})
            assert response.status_code == 422


async def test_api_do_command_skips_the_llm(settings):
    planner = FakePlanner()
    async with stack(settings, planner) as (executor, adapter, monitor):
        profiles = make_profiles()
        async with app_client(executor, monitor, settings, planner, profiles) as client:
            response = await client.post("/commands", json={"command": "/do move 0.1 0.5"})
            assert response.status_code == 202
            command_id = response.json()["id"]

            for _ in range(100):
                body = (await client.get(f"/commands/{command_id}")).json()
                if body["status"] in {"completed", "failed", "rejected"}:
                    break
                await asyncio.sleep(0.02)
            assert body["status"] == "completed"
            assert adapter.executed[0].params() == {"distance_m": 0.1, "speed_mps": 0.5}
            assert planner.calls == []  # the LLM was never consulted


async def test_api_do_command_with_bad_input_is_422(settings):
    async with stack(settings, FakePlanner()) as (executor, _adapter, monitor):
        async with app_client(executor, monitor, settings, None, make_profiles()) as client:
            response = await client.post("/commands", json={"command": "/do warp 9"})
            assert response.status_code == 422
            assert "unknown action" in response.json()["detail"]

            response = await client.post("/commands", json={"command": "/do move"})
            assert response.status_code == 422
            assert "parameter" in response.json()["detail"]


async def test_logs_endpoint_empty_without_planner_history(settings):
    async with stack(settings, FakePlanner()) as (executor, _adapter, monitor):
        async with app_client(executor, monitor, settings, None, make_profiles()) as client:
            response = await client.get("/logs")
            assert response.status_code == 200
            assert response.json()["interactions"] == []


async def test_logs_endpoint_returns_planner_interactions(settings):
    GOOD = '{"version": 1, "mode": "action", "steps": [{"action": "turn", "angle_deg": 15, "speed_dps": 30}]}'

    class StubCompletions:
        def __init__(self, contents):
            self._contents = list(contents)

        async def create(self, **kwargs):
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content=self._contents.pop(0), reasoning_content=None)
                    )
                ]
            )

    class StubClient:
        def __init__(self, contents):
            self.chat = SimpleNamespace(completions=StubCompletions(contents))

    profiles = make_profiles()
    llm_planner = LLMPlanner(
        settings, telemetry_provider=lambda: None, profiles=profiles, client=StubClient([GOOD, GOOD])
    )
    await llm_planner.plan("turn slightly left")
    await llm_planner.plan("turn left a bit")

    async with stack(settings, llm_planner, profiles=profiles) as (executor, _adapter, monitor):
        async with app_client(executor, monitor, settings, llm_planner, profiles) as client:
            response = await client.get("/logs")
            body = response.json()
            assert len(body["interactions"]) == 2
            first = body["interactions"][0]
            assert first["command"] == "turn slightly left"
            assert first["parsed"]["mode"] == "action"
            assert first["attempts"] == 1

            limited = (await client.get("/logs?limit=1")).json()
            assert len(limited["interactions"]) == 1
            assert limited["interactions"][0]["command"] == "turn left a bit"


async def test_risky_plan_rejected_without_confirm(settings):
    settings.risky_actions = ["move"]
    plan = action_plan({"action": "move", "distance_m": 0.1, "speed_mps": 1.0})
    async with stack(settings, FakePlanner(plan)) as (executor, _adapter, monitor):
        async with app_client(executor, monitor, settings, FakePlanner(plan), make_profiles()) as client:
            response = await client.post("/commands", json={"command": "nudge forward"})
            assert response.status_code == 202
            command_id = response.json()["id"]

            body: dict = {}
            for _ in range(100):
                body = (await client.get(f"/commands/{command_id}")).json()
                if body["status"] in {"rejected", "failed"}:
                    break
                await asyncio.sleep(0.02)
            assert body["status"] == "rejected"
            assert "confirm" in body["error"]


async def test_risky_plan_approved_with_confirm(settings):
    settings.risky_actions = ["move"]
    plan = action_plan({"action": "move", "distance_m": 0.1, "speed_mps": 1.0})
    async with stack(settings, FakePlanner(plan)) as (executor, _adapter, monitor):
        async with app_client(executor, monitor, settings, FakePlanner(plan), make_profiles()) as client:
            response = await client.post("/commands", json={"command": "nudge forward", "confirm": True})
            assert response.status_code == 202
            command_id = response.json()["id"]

            status = None
            body = {}
            for _ in range(150):
                body = (await client.get(f"/commands/{command_id}")).json()
                status = body["status"]
                if status in {"completed", "failed"}:
                    break
                await asyncio.sleep(0.02)
            assert status == "completed", f"command error: {body.get('error')}"


async def test_stop_endpoint_and_busy_conflict(settings):
    plan = action_plan({"action": "move", "distance_m": 0.5, "speed_mps": 0.2})
    adapter = FakeAdapter(execute_delay_s=1.0)
    async with stack(settings, FakePlanner(plan), adapter) as (executor, _adapter, monitor):
        async with app_client(executor, monitor, settings, FakePlanner(plan), make_profiles()) as client:
            first = await client.post("/commands", json={"command": "go"})
            assert first.status_code == 202

            await asyncio.sleep(0.1)
            second = await client.post("/commands", json={"command": "again"})
            assert second.status_code == 409

            stop = await client.post("/stop")
            assert stop.status_code == 200
            assert stop.json() == {"stopped": True}

            body = {}
            for _ in range(100):
                body = (await client.get(f"/commands/{first.json()['id']}")).json()
                if body["status"] in {"stopped", "completed"}:
                    break
                await asyncio.sleep(0.02)
            assert body["status"] == "stopped"


async def test_state_returns_503_without_telemetry(settings):
    async with stack(settings, FakePlanner()) as (executor, adapter, monitor):
        await adapter.disconnect()  # telemetry() now returns None
        async with app_client(executor, monitor, settings, None, make_profiles()) as client:
            response = await client.get("/state")
            assert response.status_code == 503


async def test_health_reports_profile_and_robot(settings):
    async with stack(settings, FakePlanner()) as (executor, _adapter, monitor):
        async with app_client(executor, monitor, settings, None, make_profiles()) as client:
            body = (await client.get("/health")).json()
            assert body["ok"] is True
            assert body["adapter"] == "http"
            assert body["profile_loaded"] is True
            assert body["robot_configured"] is False  # no ROBOT_BASE_URL in test settings

"""API tests: endpoints, profile upload, command lifecycle, stop, health."""

from __future__ import annotations

import asyncio

import httpx

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


def app_client(executor, adapter, settings, profiles):
    app = create_app(executor, adapter, settings, profiles)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_full_command_lifecycle(settings):
    plan = multi_plan(
        {"action": "move", "distance_m": 0.1, "speed_mps": 1.0},
        {"action": "wait", "seconds": 0.05},
    )
    async with stack(settings, FakePlanner(plan)) as (executor, adapter):
        async with app_client(executor, adapter, settings, make_profiles()) as client:
            response = await client.post("/commands", json={"command": "go and pause"})
            assert response.status_code == 202
            assert response.json()["status"] == "planning"
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
    from robotctl.core import ProfileManager

    async with stack(settings, FakePlanner()) as (executor, adapter):
        async with app_client(executor, adapter, settings, ProfileManager("")) as client:
            response = await client.post("/commands", json={"command": "go"})
            assert response.status_code == 422
            assert "no robot profile" in response.json()["detail"]


async def test_profile_upload_and_roundtrip(settings):
    async with stack(settings, FakePlanner()) as (executor, adapter):
        profiles = make_profiles()
        async with app_client(executor, adapter, settings, profiles) as client:
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
    async with stack(settings, FakePlanner()) as (executor, adapter):
        profiles = make_profiles()
        async with app_client(executor, adapter, settings, profiles) as client:
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
    from robotctl.core import ProfileManager

    async with stack(settings, FakePlanner()) as (executor, adapter):
        async with app_client(executor, adapter, settings, ProfileManager("")) as client:
            response = await client.get("/profile")
            assert response.status_code == 404


async def test_invalid_command_body_is_422(settings):
    async with stack(settings, FakePlanner()) as (executor, adapter):
        async with app_client(executor, adapter, settings, make_profiles()) as client:
            response = await client.post("/commands", json={})
            assert response.status_code == 422


async def test_stop_endpoint_and_busy_conflict(settings):
    plan = action_plan({"action": "move", "distance_m": 0.5, "speed_mps": 0.2})
    adapter = FakeAdapter(execute_delay_s=1.0)
    async with stack(settings, FakePlanner(plan), adapter) as (executor, adapter_):
        async with app_client(executor, adapter_, settings, make_profiles()) as client:
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


async def test_unknown_command_id_is_404(settings):
    async with stack(settings, FakePlanner()) as (executor, adapter):
        async with app_client(executor, adapter, settings, make_profiles()) as client:
            response = await client.get("/commands/nope")
            assert response.status_code == 404


async def test_state_returns_503_without_telemetry(settings):
    async with stack(settings, FakePlanner()) as (executor, adapter):
        adapter.drop_link()  # telemetry fetches now return None
        async with app_client(executor, adapter, settings, make_profiles()) as client:
            response = await client.get("/state")
            assert response.status_code == 503


async def test_state_returns_telemetry(settings):
    async with stack(settings, FakePlanner()) as (executor, adapter):
        async with app_client(executor, adapter, settings, make_profiles()) as client:
            response = await client.get("/state")
            assert response.status_code == 200
            body = response.json()
            assert body["connected"] is True
            assert body["motion_status"] == "idle"


async def test_health_reports_profile_and_robot(settings):
    async with stack(settings, FakePlanner()) as (executor, adapter):
        async with app_client(executor, adapter, settings, make_profiles()) as client:
            body = (await client.get("/health")).json()
            assert body["ok"] is True
            assert body["adapter"] == "http"
            assert body["profile_loaded"] is True
            assert body["robot_configured"] is False  # no ROBOT_BASE_URL in test settings


async def test_health_reports_dead_robot(settings):
    async with stack(settings, FakePlanner()) as (executor, adapter):
        adapter.drop_link()
        async with app_client(executor, adapter, settings, make_profiles()) as client:
            body = (await client.get("/health")).json()
            assert body["ok"] is False

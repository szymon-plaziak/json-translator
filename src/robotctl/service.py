"""Headless REST API service — the app's only interface.

Natural language in via `POST /commands`, an LLM plan in the robot's own
command language, deterministic profile validation, and step-by-step
execution forwarded to the real robot over HTTP. The robot-specific control
language comes from a profile uploaded via `POST /profile`; the core itself
is robot-agnostic. See API_README.md.
"""

from __future__ import annotations

import asyncio

import uvicorn
from fastapi import Body, FastAPI, HTTPException
from pydantic import BaseModel, Field

from robotctl import __version__
from robotctl.core import (
    ProfileError,
    ProfileManager,
    Settings,
    get_settings,
    parse_profile,
)
from robotctl.runtime import (
    Command,
    Executor,
    ExecutorBusy,
    HttpRobotAdapter,
    LLMPlanner,
)


class CommandIn(BaseModel):
    command: str = Field(min_length=1, max_length=500)


def create_app(
    executor: Executor,
    adapter: HttpRobotAdapter,
    settings: Settings,
    profiles: ProfileManager,
) -> FastAPI:
    """Build the API app."""
    app = FastAPI(
        title="robotctl",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.post("/profile")
    async def upload_profile(payload: dict = Body(...)) -> dict:
        """Upload/replace the robot profile (validated, persisted, hot-applied)."""
        try:
            profile = parse_profile(payload)
        except ProfileError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        profiles.set(profile)
        return {
            "ok": True,
            "robot": profile.robot.name,
            "actions": profile.action_names(),
            "persisted": profiles.path is not None,
        }

    @app.get("/profile")
    async def get_profile() -> dict:
        profile = profiles.current()
        if profile is None:
            raise HTTPException(status_code=404, detail="no robot profile loaded")
        return profile.model_dump()

    @app.post("/commands", status_code=202)
    async def submit_command(body: CommandIn) -> dict:
        if profiles.current() is None:
            raise HTTPException(
                status_code=422,
                detail="no robot profile loaded; upload one via POST /profile first",
            )
        try:
            command = await executor.submit(body.command.strip())
        except ExecutorBusy as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"id": command.id, "status": command.status}

    @app.get("/commands/{command_id}")
    async def get_command(command_id: str) -> Command:
        command = executor.get(command_id)
        if command is None:
            raise HTTPException(status_code=404, detail="unknown command id")
        return command

    @app.get("/state")
    async def state() -> dict:
        snapshot = await adapter.fetch_telemetry()
        if snapshot is None:
            raise HTTPException(status_code=503, detail="no telemetry available")
        return snapshot.model_dump()

    @app.post("/stop")
    async def stop() -> dict:
        executor.stop()
        return {"stopped": True}

    @app.get("/health")
    async def health() -> dict:
        snapshot = await adapter.fetch_telemetry()
        return {
            "ok": snapshot is not None and snapshot.connected,
            "adapter": "http",
            "robot_configured": bool(settings.robot_base_url.strip()),
            "profile_loaded": profiles.current() is not None,
        }

    return app


async def serve() -> int:
    settings = get_settings()

    adapter = HttpRobotAdapter(settings)
    profiles = ProfileManager(settings.profile_path)
    if profiles.current() is None and profiles.path is not None and profiles.path.is_file():
        print(f"warning: {profiles.path} exists but holds no valid profile; ignoring it")

    try:
        planner = LLMPlanner(settings, profiles, adapter=adapter)
    except ValueError as exc:  # e.g. invalid LLM_EXTRA_BODY
        print(f"configuration error: {exc}")
        await adapter.disconnect()
        return 2

    executor = Executor(settings, adapter, planner, profiles)
    app = create_app(executor, adapter, settings, profiles)

    host, port = settings.api_host, settings.api_port
    config = uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)

    profile = profiles.current()
    profile_line = (
        f"{profile.robot.name} ({len(profile.actions)} actions)"
        if profile is not None
        else "(none — upload via POST /profile)"
    )
    robot = settings.robot_base_url.strip() or "(not configured)"
    print(f"robotctl {__version__} — robot controller REST API")
    print(f"  robot         : {robot}")
    print(f"  profile       : {profile_line}")
    print(f"  llm           : {settings.llm_model} @ {settings.llm_base_url}")
    print(f"  api           : http://{host}:{port} (loopback only — do not expose)")
    print("  shutdown      : Ctrl+C or SIGTERM")

    try:
        # Uvicorn owns the current loop; SIGINT/SIGTERM end serve() cleanly.
        await server.serve()
    finally:
        executor.stop()
        await planner.aclose()
        await adapter.disconnect()
        print("robot stopped and disconnected")
    return 0


def main() -> int:
    try:
        return asyncio.run(serve())
    except KeyboardInterrupt:
        print("\ninterrupted; robot stopped safely")
        return 130

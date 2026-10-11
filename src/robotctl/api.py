"""Local REST API.

This is the app's only interface: a loopback-only API service for another
application (no interactive UI). It exposes exactly the same validated
pipeline the executor enforces everywhere: LLM plans are parsed and
re-validated against the uploaded robot profile, risky actions need explicit
confirmation, and the e-stop bypasses the LLM.
"""

from __future__ import annotations

from fastapi import Body, FastAPI, HTTPException
from pydantic import BaseModel, Field

from robotctl.config import Settings
from robotctl.executor import Command, Executor, ExecutorBusy
from robotctl.monitor import StateMonitor
from robotctl.profile import ProfileError, ProfileManager, parse_do_action, parse_profile
from robotctl.schemas import PlanResponse, SCHEMA_VERSION

_MAX_LOG_LIMIT = 50


class CommandIn(BaseModel):
    command: str = Field(min_length=1, max_length=500)
    confirm: bool = False


async def _approve(command: Command) -> bool:
    """Confirmation callback used when a request sets "confirm": true."""
    return True


def create_app(
    executor: Executor,
    monitor: StateMonitor,
    settings: Settings,
    planner: object | None = None,
    profiles: ProfileManager | None = None,
) -> FastAPI:
    """Build the API app.

    `planner` is only used by GET /logs; `profiles` is required for
    /commands and /profile.
    """
    app = FastAPI(
        title="robotctl",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    def _require_profiles() -> ProfileManager:
        if profiles is None:
            raise HTTPException(status_code=503, detail="profile manager not configured")
        return profiles

    @app.post("/profile")
    async def upload_profile(payload: dict = Body(...)) -> dict:
        """Upload/replace the robot profile (validated, persisted, hot-applied)."""
        manager = _require_profiles()
        try:
            profile = parse_profile(payload)
        except ProfileError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        manager.set(profile)
        return {
            "ok": True,
            "robot": profile.robot.name,
            "actions": profile.action_names(),
            "persisted": manager.path is not None,
        }

    @app.get("/profile")
    async def get_profile() -> dict:
        manager = _require_profiles()
        profile = manager.current()
        if profile is None:
            raise HTTPException(status_code=404, detail="no robot profile loaded")
        return profile.model_dump()

    @app.post("/commands", status_code=202)
    async def submit_command(body: CommandIn) -> dict:
        manager = _require_profiles()
        if manager.current() is None:
            raise HTTPException(
                status_code=422,
                detail="no robot profile loaded; upload one via POST /profile first",
            )
        try:
            text = body.command.strip()
            if text.startswith("/do"):
                # Manual action: parsed against the profile, no LLM call.
                action = parse_do_action(text.split()[1:], manager.current())
                command = await executor.submit_plan(
                    PlanResponse(version=SCHEMA_VERSION, mode="action", steps=[action]),
                    raw_command=text,
                    source="api",
                    confirm=_approve if body.confirm else None,
                )
            else:
                command = await executor.submit(
                    body.command,
                    source="api",
                    confirm=_approve if body.confirm else None,
                )
        except ExecutorBusy as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:  # invalid /do action
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"id": command.id, "status": command.status.value}

    @app.get("/commands/{command_id}")
    async def get_command(command_id: str) -> Command:
        command = executor.get(command_id)
        if command is None:
            raise HTTPException(status_code=404, detail="unknown command id")
        return command

    @app.get("/state")
    async def state() -> dict:
        snapshot = monitor.snapshot()
        if snapshot is None:
            raise HTTPException(status_code=503, detail="no telemetry available")
        return snapshot.model_dump()

    @app.post("/stop")
    async def stop() -> dict:
        executor.stop()
        return {"stopped": True}

    @app.get("/health")
    async def health() -> dict:
        snapshot = monitor.snapshot()
        return {
            "ok": snapshot is not None and snapshot.connected,
            "adapter": "http",
            "robot_configured": bool(settings.robot_base_url.strip()),
            "profile_loaded": profiles is not None and profiles.current() is not None,
        }

    @app.get("/logs")
    async def logs(limit: int = 20) -> dict:
        """Recent LLM planner interactions (command, thinking, raw answer,
        parsed JSON, error). Empty when the planner provides no history."""
        provider = getattr(planner, "entries", None)
        entries = list(provider()) if callable(provider) else []
        limit = max(1, min(limit, _MAX_LOG_LIMIT))
        return {
            "interactions": [
                entry.model_dump(mode="json") for entry in entries[-limit:]
            ]
        }

    return app

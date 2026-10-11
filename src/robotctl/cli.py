"""robotctl service entry point — headless REST API.

The app is an API service for another application: natural language in via
`POST /commands`, validated JSON plans executed through the safety layer and
the robot's HTTP endpoints. The robot-specific control language comes from a
profile uploaded via `POST /profile`; the core itself is robot-agnostic.
There is no interactive terminal UI and no simulation; see API_README.md.
"""

from __future__ import annotations

import argparse
import asyncio

import uvicorn

from robotctl import __version__
from robotctl.adapters import build_adapter
from robotctl.api import create_app
from robotctl.config import get_settings
from robotctl.executor import Executor
from robotctl.monitor import StateMonitor
from robotctl.planner import LLMPlanner
from robotctl.profile import ProfileManager


async def serve(args: argparse.Namespace) -> int:
    settings = get_settings()
    host = args.host if args.host is not None else settings.api_host
    port = args.port if args.port is not None else settings.api_port

    adapter = build_adapter(settings)
    await adapter.connect()
    monitor = StateMonitor(adapter, settings.telemetry_max_age_s)
    monitor.start()
    try:
        await monitor.wait_for_telemetry(timeout_s=2.0)
    except TimeoutError:
        if settings.robot_base_url.strip():
            print(
                "warning: robot not reachable yet — /health shows ok:false and "
                "commands fail safely until telemetry arrives"
            )
        else:
            print(
                "warning: ROBOT_BASE_URL is not set — no robot configured; "
                "profile/LLM setup still works, execution will fail safely"
            )

    profiles = ProfileManager(settings.profile_path)
    if profiles.current() is None and profiles.path is not None and profiles.path.is_file():
        print(f"warning: {profiles.path} exists but holds no valid profile; ignoring it")

    try:
        planner = LLMPlanner(settings, monitor.snapshot, profiles)
    except ValueError as exc:  # e.g. invalid LLM_EXTRA_BODY
        print(f"configuration error: {exc}")
        await monitor.stop()
        await adapter.disconnect()
        return 2

    executor = Executor(settings, adapter, monitor, planner, profiles)
    app = create_app(executor, monitor, settings, planner, profiles)

    config = uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)

    profile = profiles.current()
    profile_line = (
        f"{profile.robot.name} ({len(profile.actions)} actions)"
        if profile is not None
        else "(none — upload via POST /profile)"
    )
    risky = ", ".join(settings.risky_actions) if settings.risky_actions else "(none)"
    robot = settings.robot_base_url.strip() or "(not configured)"
    print(f"robotctl {__version__} — robot controller REST API")
    print(f"  robot         : {robot}")
    print(f"  profile       : {profile_line}")
    print(f"  llm           : {settings.llm_model} @ {settings.llm_base_url}")
    print(f"  risky actions : {risky}")
    print(f"  api           : http://{host}:{port} (loopback only — do not expose)")
    print("  shutdown      : Ctrl+C or SIGTERM")

    try:
        # Uvicorn owns the current loop; SIGINT/SIGTERM end serve() cleanly.
        await server.serve()
    finally:
        executor.stop()
        await monitor.stop()
        await adapter.disconnect()
        print("robot stopped and disconnected")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="robotctl",
        description="Universal robot controller REST API (LLM planner, safety layer, HTTP bridge).",
    )
    parser.add_argument("--host", default=None, help="bind address (default: API_HOST, 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None, help="bind port (default: API_PORT, 8081)")
    parser.add_argument("--version", action="version", version=f"robotctl {__version__}")
    args = parser.parse_args(argv)
    try:
        return asyncio.run(serve(args))
    except KeyboardInterrupt:
        print("\ninterrupted; robot stopped safely")
        return 130

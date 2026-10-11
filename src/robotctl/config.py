"""Application configuration, loaded from environment variables / a local .env file.

Secrets and deployment-specific values never live in source control; copy
.env.example to .env and edit it.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- LLM endpoint (any OpenAI-compatible server) ------------------------
    llm_base_url: str = "http://localhost"
    llm_api_key: str = "sk-key"
    llm_model: str = "Model"
    # json_schema = structured outputs, json_object = JSON mode, none = prompt only.
    llm_response_format: str = "json_schema"
    llm_timeout_s: float = 10.0
    llm_max_tokens: int = 300
    llm_temperature: float = 0.0
    # JSON object merged into each chat-completions request (e.g. disabling
    # Qwen3-style thinking: {"chat_template_kwargs": {"enable_thinking": false}}).
    # If the server rejects it, the planner retries without it automatically.
    llm_extra_body: str = ""
    # Planner interactions are appended here as JSON lines (empty disables).
    # Empty by default so scripts/tests never write logs implicitly; .env sets it.
    llm_log_file: str = ""

    # --- Robot (real hardware via the HTTP bridge) ----------------------------
    # The robot's companion app/firmware exposes three endpoints; see
    # adapters/http.py. An empty ROBOT_BASE_URL means "no robot configured":
    # the service still starts, /health reports ok:false, execution fails safe.
    robot_base_url: str = ""
    robot_command_path: str = "/command"
    robot_telemetry_path: str = "/telemetry"
    # Empty disables the best-effort emergency-halt POST.
    robot_stop_path: str = "/stop"
    robot_timeout_s: float = 30.0  # per command/telemetry HTTP request
    robot_poll_hz: float = 5.0  # telemetry polling rate

    # --- Robot profile --------------------------------------------------------
    # Uploaded via POST /profile; persisted here and reloaded at startup.
    # Empty keeps profiles in memory only.
    profile_path: str = ""

    # --- Universal safety limits ----------------------------------------------
    max_plan_steps: int = 8
    step_timeout_s: float = 15.0
    telemetry_max_age_s: float = 1.0
    # Action names (from the robot profile) that always require "confirm": true.
    # NoDecode: the dotenv/env source must not attempt json.loads on the raw
    # value (e.g. an empty RISKY_ACTIONS= would crash); _parse_csv handles it.
    risky_actions: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # --- Local REST API -----------------------------------------------------
    api_host: str = "127.0.0.1"
    api_port: int = 8080

    @field_validator("risky_actions", mode="before")
    @classmethod
    def _parse_csv(cls, value: object) -> object:
        """Accept RISKY_ACTIONS=move,wait as well as a JSON list."""
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()

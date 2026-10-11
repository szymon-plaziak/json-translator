"""Pytest configuration: deterministic, .env-free settings."""

from __future__ import annotations

import pytest

from robotctl.core import Settings


@pytest.fixture
def settings() -> Settings:
    """Fast, deterministic settings: no .env, no persistence, prompt-only LLM."""
    return Settings(
        _env_file=None,
        llm_response_format="none",
        llm_log_file="",  # keep tests from writing log files
        llm_extra_body="",
        # generous wall-clock margins: this NTFS-backed WSL setup can stall
        # for seconds under load (individual tests override when they test
        # timeouts / staleness explicitly)
        step_timeout_s=5.0,
        telemetry_max_age_s=2.0,
        profile_path="",  # profiles stay in memory unless a test opts in
    )

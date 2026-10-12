"""Pytest configuration: deterministic settings built from the process
environment only (no .env pickup, no persistence)."""

from __future__ import annotations

import pytest

from robotctl.core import Settings


@pytest.fixture
def settings() -> Settings:
    """Fast, deterministic settings: prompt-only LLM, in-memory profiles.

    Generous wall-clock margins because this NTFS-backed WSL setup can stall
    for seconds under load (individual tests override when they test
    timeouts explicitly).
    """
    return Settings(
        llm_response_format="none",
        llm_extra_body="",
        step_timeout_s=5.0,
        profile_path="",
    )

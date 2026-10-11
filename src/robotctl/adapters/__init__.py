"""Adapter layer: the generic HTTP bridge to a real robot."""

from __future__ import annotations

from robotctl.adapters.base import AdapterError, RobotAdapter
from robotctl.adapters.http import HttpRobotAdapter
from robotctl.config import Settings

__all__ = ["AdapterConfigError", "AdapterError", "HttpRobotAdapter", "RobotAdapter", "build_adapter"]


class AdapterConfigError(Exception):
    """Raised when the configured adapter cannot be constructed."""


def build_adapter(settings: Settings) -> RobotAdapter:
    if not settings.robot_base_url.strip():
        # Allowed: the service starts anyway so the profile/LLM can be set up
        # first; /health reports ok:false and execution fails safely.
        pass
    return HttpRobotAdapter(settings)

"""Robot adapter interface: all hardware specifics live behind this contract."""

from __future__ import annotations

from abc import ABC, abstractmethod

from robotctl.schemas import ActionStep, Telemetry


class AdapterError(Exception):
    """Raised when the adapter cannot complete an action."""


class RobotAdapter(ABC):
    """Validated-command interface to a robot (real hardware via HTTP)."""

    @abstractmethod
    async def connect(self) -> None:
        """Open the connection and start producing telemetry."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Stop the robot and close the connection."""

    @abstractmethod
    async def execute(self, action: ActionStep) -> None:
        """Run one validated action to completion.

        Raises AdapterError on failure. Must honour cancellation (emergency
        stop / step timeout) by halting all motion before propagating it.
        """

    @abstractmethod
    async def stop(self) -> None:
        """Immediate stop. Safe to call repeatedly and must never raise."""

    @abstractmethod
    def telemetry(self) -> Telemetry | None:
        """Latest telemetry snapshot, or None before the first sample."""

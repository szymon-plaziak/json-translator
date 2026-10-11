"""State monitor: caches the latest telemetry, keeps a bounded history and
answers freshness questions for the executor and the dashboard."""

from __future__ import annotations

import asyncio
import time
from collections import deque

from robotctl.adapters.base import RobotAdapter
from robotctl.schemas import Telemetry


class StateMonitor:
    def __init__(self, adapter: RobotAdapter, max_age_s: float, history: int = 300) -> None:
        self._adapter = adapter
        self._max_age_s = max_age_s
        self._history: deque[Telemetry] = deque(maxlen=history)
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._poll())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _poll(self) -> None:
        try:
            while True:
                snapshot = self.snapshot()
                if snapshot is not None:
                    self._history.append(snapshot)
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            raise

    # -- queries -------------------------------------------------------------

    def snapshot(self) -> Telemetry | None:
        return self._adapter.telemetry()

    def age_s(self) -> float | None:
        snapshot = self.snapshot()
        if snapshot is None:
            return None
        return max(0.0, time.monotonic() - snapshot.monotonic)

    def is_fresh(self) -> bool:
        snapshot = self.snapshot()
        if snapshot is None or not snapshot.connected:
            return False
        return (time.monotonic() - snapshot.monotonic) <= self._max_age_s

    def history(self) -> list[Telemetry]:
        return list(self._history)

    async def wait_for_telemetry(self, timeout_s: float = 5.0) -> Telemetry:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            snapshot = self.snapshot()
            if snapshot is not None:
                return snapshot
            await asyncio.sleep(0.05)
        raise TimeoutError("no telemetry received from the robot adapter")

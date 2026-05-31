"""
Abstract base class for all tracking agents.
Provides a consistent asyncio lifecycle: start → _run_loop → process → stop.
"""
import asyncio
import logging
from abc import ABC, abstractmethod

log = logging.getLogger(__name__)


class BaseAgent(ABC):
    """
    Subclass and implement `process()` and `on_ais_message()`.
    Call `start()` to launch the background asyncio task.
    """

    name: str = "BaseAgent"
    interval_seconds: int = 60

    def __init__(self) -> None:
        self._running = False
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Launch the agent's background loop as an asyncio Task."""
        self._running = True
        self._task = asyncio.create_task(self._run_loop(), name=self.name)
        log.info("[%s] started (interval=%ds)", self.name, self.interval_seconds)

    async def stop(self) -> None:
        """Gracefully cancel the background loop."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("[%s] stopped", self.name)

    @property
    def is_running(self) -> bool:
        return self._running and (self._task is not None) and (not self._task.done())

    # ------------------------------------------------------------------
    # Internal loop — catches all exceptions so one bad cycle never kills the agent
    # ------------------------------------------------------------------

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await self.process()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.exception("[%s] process() error: %s", self.name, exc)
            try:
                await asyncio.sleep(self.interval_seconds)
            except asyncio.CancelledError:
                break

    # ------------------------------------------------------------------
    # Abstract interface
    # ------------------------------------------------------------------

    @abstractmethod
    async def process(self) -> None:
        """
        Called every `interval_seconds`. Implement periodic classification,
        DB updates, and cleanup here.
        """

    @abstractmethod
    async def on_ais_message(self, message: dict) -> None:
        """
        Called by AISBus for every incoming AIS message.
        Implement real-time vessel classification here.
        """

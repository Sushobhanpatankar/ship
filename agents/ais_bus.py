"""
AISBus — single WebSocket connection to AISstream.io that fans out every
incoming AIS message to all registered subscriber callbacks.

Only ONE WebSocket connection is ever open, regardless of how many agents
are subscribed. This avoids exceeding the AISstream per-key connection limit.
"""
import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

import websockets
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

from config import (
    AIS_ENERGY_SHIP_TYPES,
    AIS_RECONNECT_DELAY_SECONDS,
    AIS_RECONNECT_MAX_SECONDS,
    AISSTREAM_WS_URL,
    INDIA_BOUNDING_BOX,
)

log = logging.getLogger(__name__)

MessageCallback = Callable[[dict], Awaitable[None]]


class AISBus:
    """
    Manages the connection to AISstream and distributes messages.

    Usage:
        bus = AISBus()
        bus.subscribe(my_agent.on_ais_message)
        await bus.start()   # runs forever until cancelled
    """

    def __init__(self) -> None:
        self._subscribers: list[MessageCallback] = []
        self._connected = False
        self._last_message_at: datetime | None = None
        self._message_count = 0
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def subscribe(self, callback: MessageCallback) -> None:
        self._subscribers.append(callback)

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def message_count(self) -> int:
        return self._message_count

    @property
    def last_message_at(self) -> datetime | None:
        return self._last_message_at

    async def start(self) -> None:
        """Start the WebSocket stream loop. Runs until cancelled."""
        self._task = asyncio.current_task()
        await self._stream_loop()

    async def stop(self) -> None:
        self._connected = False
        if self._task and not self._task.done():
            self._task.cancel()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _build_subscription(self) -> str:
        api_key = os.getenv("AISSTREAM_API_KEY", "")
        payload = {
            "APIKey": api_key,
            "BoundingBoxes": INDIA_BOUNDING_BOX,
            "FilterMessageTypes": [
                "PositionReport",
                "ShipStaticData",
                "StandardClassBPositionReport",
                "ExtendedClassBPositionReport",
            ],
            "FilterShipTypes": AIS_ENERGY_SHIP_TYPES,
        }
        return json.dumps(payload)

    async def _fan_out(self, message: dict) -> None:
        if not self._subscribers:
            return
        results = await asyncio.gather(
            *[cb(message) for cb in self._subscribers],
            return_exceptions=True,
        )
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                log.warning("[AISBus] subscriber[%d] raised: %s", i, result)

    async def _stream_loop(self) -> None:
        delay = AIS_RECONNECT_DELAY_SECONDS
        while True:
            try:
                log.info("[AISBus] connecting to %s", AISSTREAM_WS_URL)
                async with websockets.connect(
                    AISSTREAM_WS_URL,
                    ping_interval=20,
                    ping_timeout=30,
                    close_timeout=10,
                ) as ws:
                    await ws.send(self._build_subscription())
                    self._connected = True
                    delay = AIS_RECONNECT_DELAY_SECONDS  # reset on successful connect
                    log.info("[AISBus] connected — streaming AIS data")

                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        self._message_count += 1
                        self._last_message_at = datetime.now(timezone.utc)
                        await self._fan_out(msg)

            except asyncio.CancelledError:
                log.info("[AISBus] cancelled")
                self._connected = False
                break
            except (ConnectionClosedOK, ConnectionClosedError) as e:
                log.warning("[AISBus] connection closed: %s", e)
            except Exception as e:
                log.exception("[AISBus] unexpected error: %s", e)
            finally:
                self._connected = False

            log.info("[AISBus] reconnecting in %ds", delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, AIS_RECONNECT_MAX_SECONDS)

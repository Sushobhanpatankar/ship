"""
WebSocket manager — maintains all connected dashboard clients and
broadcasts updates from the AggregatorAgent's notification queue.
"""
import asyncio
import json
import logging
from datetime import datetime, timezone

from fastapi import WebSocket, WebSocketDisconnect

log = logging.getLogger(__name__)


class ConnectionManager:
    def __init__(self) -> None:
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.active.append(ws)
        log.info("[WS] client connected (total: %d)", len(self.active))

    def disconnect(self, ws: WebSocket) -> None:
        self.active = [w for w in self.active if w is not ws]
        log.info("[WS] client disconnected (total: %d)", len(self.active))

    async def broadcast(self, payload: dict) -> None:
        if not self.active:
            return
        text = json.dumps(payload, default=str)
        dead: list[WebSocket] = []
        for ws in list(self.active):
            try:
                await ws.send_text(text)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    @property
    def client_count(self) -> int:
        return len(self.active)


manager = ConnectionManager()


async def ws_endpoint(websocket: WebSocket, queue: asyncio.Queue) -> None:
    """
    Called by api/server.py router. Handles one client connection:
    - Sends an immediate welcome with current stats
    - Relays queue events to client
    - Handles ping/pong keepalive
    """
    await manager.connect(websocket)
    try:
        # Send initial ping back so client knows it's live
        await websocket.send_text(json.dumps({
            "type": "connected",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }))

        # Just keep alive — broadcasts come from the background task
        while True:
            try:
                data = await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
                if data == "ping":
                    await websocket.send_text(json.dumps({"type": "pong"}))
            except asyncio.TimeoutError:
                # Send keepalive
                await websocket.send_text(json.dumps({"type": "keepalive"}))
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.debug("[WS] client error: %s", e)
    finally:
        manager.disconnect(websocket)


async def broadcast_loop(queue: asyncio.Queue) -> None:
    """
    Background task that reads from the notification queue and broadcasts to
    all connected WebSocket clients. Run this as an asyncio task in server.py.
    """
    while True:
        try:
            payload = await asyncio.wait_for(queue.get(), timeout=30.0)
            await manager.broadcast(payload)
        except asyncio.TimeoutError:
            # Keepalive broadcast every 30s even without new data
            await manager.broadcast({
                "type": "heartbeat",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "clients": manager.client_count,
            })
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.exception("[WS] broadcast_loop error: %s", e)

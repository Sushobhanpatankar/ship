"""
FastAPI application factory.
The lifespan context manager starts all agents and the AIS bus on startup
and shuts them down cleanly on exit.
"""
import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from agents.aggregator_agent import AggregatorAgent, set_notification_queue
from agents.ais_bus import AISBus
from agents.inbound_agent import InboundAgent
from agents.outbound_agent import OutboundAgent
from agents.port_activity_agent import PortActivityAgent
from api.routes.aggregator import router as agg_router
from api.routes.inbound import router as inbound_router
from api.routes.outbound import router as outbound_router
from api.routes.port_activity import router as port_router
from api.routes.ships import router as ships_router
from api.websocket import broadcast_loop, manager, ws_endpoint
from database.db import init_db

log = logging.getLogger(__name__)

DASHBOARD_DIR = Path(__file__).parent.parent / "dashboard"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ------------------------------------------------------------------ startup
    log.info("Initialising database…")
    await init_db()

    log.info("Starting agents…")
    notification_queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    set_notification_queue(notification_queue)

    ais_bus = AISBus()
    inbound_agent = InboundAgent(ais_bus)
    outbound_agent = OutboundAgent(ais_bus)
    port_agent = PortActivityAgent(ais_bus)
    aggregator = AggregatorAgent()

    # Store references on app.state for routes/health endpoint
    app.state.ais_bus = ais_bus
    app.state.agents = {
        "inbound": inbound_agent,
        "outbound": outbound_agent,
        "port_activity": port_agent,
        "aggregator": aggregator,
    }
    app.state.aggregator = aggregator
    app.state.notification_queue = notification_queue

    # Start agent background loops
    await inbound_agent.start()
    await outbound_agent.start()
    await port_agent.start()
    await aggregator.start()

    # Start AIS WebSocket stream as a background task
    ais_task = asyncio.create_task(ais_bus.start(), name="AISBus")
    # Start WebSocket broadcast loop
    broadcast_task = asyncio.create_task(
        broadcast_loop(notification_queue), name="BroadcastLoop"
    )

    log.info("All agents running. Dashboard at http://%s:%s/",
             os.getenv("HOST", "0.0.0.0"), os.getenv("PORT", "8000"))

    yield  # ← application serves requests

    # ----------------------------------------------------------------- shutdown
    log.info("Shutting down agents…")
    for agent in app.state.agents.values():
        await agent.stop()
    ais_task.cancel()
    broadcast_task.cancel()
    try:
        await asyncio.gather(ais_task, broadcast_task, return_exceptions=True)
    except Exception:
        pass
    log.info("Shutdown complete.")


def create_app() -> FastAPI:
    app = FastAPI(
        title="India Energy Vessel Tracker",
        description="Real-time tracking of crude, LNG, CNG & petroleum ships around India",
        version="1.0.0",
        lifespan=lifespan,
    )

    # ---- REST routes
    app.include_router(inbound_router, prefix="/api", tags=["Inbound"])
    app.include_router(outbound_router, prefix="/api", tags=["Outbound"])
    app.include_router(port_router, prefix="/api", tags=["Port Activity"])
    app.include_router(agg_router, prefix="/api", tags=["Summary"])
    app.include_router(ships_router, prefix="/api", tags=["Vessels"])

    # ---- Health
    @app.get("/api/health", tags=["System"])
    async def health():
        bus = app.state.ais_bus
        agents = app.state.agents
        return {
            "status": "ok",
            "ais_connected": bus.connected,
            "ais_messages": bus.message_count,
            "ais_last_message": bus.last_message_at,
            "ws_clients": manager.client_count,
            "agents": {
                name: {"running": a.is_running}
                for name, a in agents.items()
            },
        }

    # ---- WebSocket
    @app.websocket("/ws/updates")
    async def websocket_route(websocket: WebSocket):
        await ws_endpoint(websocket, app.state.notification_queue)

    # ---- Static dashboard files
    if DASHBOARD_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(DASHBOARD_DIR)), name="static")

        @app.get("/")
        async def serve_dashboard():
            return FileResponse(str(DASHBOARD_DIR / "index.html"))

        @app.get("/{path:path}")
        async def serve_static(path: str):
            file = DASHBOARD_DIR / path
            if file.exists() and file.is_file():
                return FileResponse(str(file))
            return FileResponse(str(DASHBOARD_DIR / "index.html"))

    return app


app = create_app()

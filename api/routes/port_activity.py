from datetime import datetime, timezone
from fastapi import APIRouter, Query
from database import db

router = APIRouter()


@router.get("/port-activity")
async def get_port_activity(
    port: str = Query(default=None, description="Port name, e.g. Paradip"),
    cargo_type: str = Query(default=None, description="CRUDE|LNG|CNG|PETROLEUM"),
    limit: int = Query(default=200, le=500),
):
    ships = await db.get_port_activity(port=port, cargo_type=cargo_type, limit=limit)
    return {
        "count": len(ships),
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "ships": ships,
    }

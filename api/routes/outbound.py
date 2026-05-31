from datetime import datetime, timezone
from fastapi import APIRouter, Query
from database import db

router = APIRouter()


@router.get("/outbound")
async def get_outbound(
    cargo_type: str = Query(default=None, description="CRUDE|LNG|CNG|PETROLEUM"),
    limit: int = Query(default=200, le=500),
):
    ships = await db.get_outbound_ships(cargo_type=cargo_type, limit=limit)
    return {
        "count": len(ships),
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "ships": ships,
    }

from fastapi import APIRouter, HTTPException
from database import db

router = APIRouter()


@router.get("/ships/{mmsi}")
async def get_ship(mmsi: str):
    vessel = await db.get_vessel(mmsi)
    if not vessel:
        raise HTTPException(status_code=404, detail="Vessel not found")
    positions = await db.get_vessel_positions(mmsi, limit=50)
    return {**vessel, "recent_positions": positions}


@router.get("/ships/{mmsi}/track")
async def get_ship_track(mmsi: str):
    """Return vessel track as GeoJSON LineString for Leaflet."""
    positions = await db.get_vessel_positions(mmsi, limit=100)
    if not positions:
        raise HTTPException(status_code=404, detail="No track data found")

    # GeoJSON: [lon, lat] order
    coords = [[p["longitude"], p["latitude"]] for p in reversed(positions)
              if p.get("latitude") and p.get("longitude")]
    return {
        "type": "Feature",
        "properties": {"mmsi": mmsi},
        "geometry": {
            "type": "LineString",
            "coordinates": coords,
        },
    }

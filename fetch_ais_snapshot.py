"""
AIS snapshot fetcher — connects to AISstream.io for STREAM_SECONDS, collects
energy tanker positions around India, classifies vessels as INBOUND or OUTBOUND,
and writes docs/ais_snapshot.json.

Run by GitHub Actions every 6 hours. Requires AISSTREAM_API_KEY secret.
No persistent server needed — one connection, fixed window, disconnect.
"""
import asyncio
import json
import os
import sys
from datetime import datetime, timezone

import websockets

from config import (
    AIS_ENERGY_SHIP_TYPES,
    INDIA_BOUNDING_BOX,
    INBOUND_NEAR_RADIUS_NM,
    AISSTREAM_WS_URL,
    classify_cargo,
    destination_is_india,
    is_heading_toward_india,
    is_in_port_zone,
    nearest_indian_port,
)

STREAM_SECONDS   = int(os.environ.get("AIS_STREAM_SECONDS", "300"))  # 5 min default
OUTPUT_FILE      = "docs/ais_snapshot.json"
OUTBOUND_RADIUS_NM = 150.0   # within this of a port, moving away → outbound
MIN_OUTBOUND_SPD = 3.0       # knots — ignore drifting vessels

API_KEY = os.environ.get("AISSTREAM_API_KEY", "").strip()


# ─────────────────────────────────────────────────────────────
# In-memory vessel state
# ─────────────────────────────────────────────────────────────

vessel_meta: dict[str, dict] = {}      # mmsi → static data
vessel_pos:  dict[str, dict] = {}      # mmsi → latest position


def _handle_static(msg: dict) -> None:
    data = msg.get("Message", {}).get("ShipStaticData", {})
    mmsi = str(data.get("UserId") or data.get("Mmsi") or "")
    if not mmsi:
        return
    vessel_meta[mmsi] = {
        "ship_name":   (data.get("Name") or "").strip(),
        "ship_type":   int(data.get("Type") or 0),
        "destination": (data.get("Destination") or "").strip(),
        "cargo_category": classify_cargo(int(data.get("Type") or 0)),
    }


def _handle_position(msg: dict, msg_type: str) -> None:
    key_map = {
        "PositionReport":                    "PositionReport",
        "StandardClassBPositionReport":      "StandardClassBPositionReport",
        "ExtendedClassBPositionReport":      "ExtendedClassBPositionReport",
    }
    pos = msg.get("Message", {}).get(key_map.get(msg_type, msg_type), {})
    mmsi = str(pos.get("UserId") or pos.get("Mmsi") or "")
    if not mmsi:
        return
    lat = float(pos.get("Latitude") or 0)
    lon = float(pos.get("Longitude") or 0)
    if lat == 0.0 and lon == 0.0:
        return
    vessel_pos[mmsi] = {
        "lat":    lat,
        "lon":    lon,
        "speed":  float(pos.get("Sog") or pos.get("SpeedOverGround") or 0),
        "course": float(pos.get("Cog") or pos.get("CourseOverGround") or 0),
    }


# ─────────────────────────────────────────────────────────────
# Classification
# ─────────────────────────────────────────────────────────────

def _classify(mmsi: str) -> str | None:
    """Return 'INBOUND', 'OUTBOUND', or None (not relevant / in port)."""
    pos  = vessel_pos.get(mmsi)
    meta = vessel_meta.get(mmsi, {})
    if not pos:
        return None

    lat, lon   = pos["lat"], pos["lon"]
    speed      = pos["speed"]
    course     = pos["course"]

    # Skip ships already inside a port zone (tracked by scraper)
    in_port, _ = is_in_port_zone(lat, lon)
    if in_port:
        return None

    port_name, dist = nearest_indian_port(lat, lon)
    heading_india, _ = is_heading_toward_india(lat, lon, course, speed)
    dest_india = destination_is_india(meta.get("destination", ""))
    near_india = dist <= INBOUND_NEAR_RADIUS_NM

    # Inbound: ≥2 signals (proximity, heading, destination)
    if sum([near_india, heading_india, dest_india]) >= 2:
        return "INBOUND"

    # Outbound: close to a port, moving, not heading toward India
    if dist <= OUTBOUND_RADIUS_NM and speed >= MIN_OUTBOUND_SPD and not heading_india:
        return "OUTBOUND"

    return None


def _build_record(mmsi: str, role: str) -> dict:
    pos  = vessel_pos[mmsi]
    meta = vessel_meta.get(mmsi, {})
    port, dist = nearest_indian_port(pos["lat"], pos["lon"])
    return {
        "mmsi":           mmsi,
        "ship_name":      meta.get("ship_name", "Unknown"),
        "cargo_category": meta.get("cargo_category", "OTHER"),
        "destination":    meta.get("destination", ""),
        "nearest_port":   port,
        "distance_nm":    round(dist, 1),
        "speed":          pos["speed"],
        "course":         pos["course"],
        "lat":            round(pos["lat"], 4),
        "lon":            round(pos["lon"], 4),
        "role":           role,
    }


# ─────────────────────────────────────────────────────────────
# WebSocket stream
# ─────────────────────────────────────────────────────────────

async def stream(api_key: str) -> None:
    sub = json.dumps({
        "APIKey":             api_key,
        "BoundingBoxes":      INDIA_BOUNDING_BOX,
        "FilterMessageTypes": [
            "PositionReport",
            "ShipStaticData",
            "StandardClassBPositionReport",
            "ExtendedClassBPositionReport",
        ],
        "FilterShipTypes": AIS_ENERGY_SHIP_TYPES,
    })

    print(f"Connecting to AISstream (streaming {STREAM_SECONDS}s)…")
    async with websockets.connect(
        AISSTREAM_WS_URL,
        ping_interval=20,
        ping_timeout=30,
        close_timeout=10,
    ) as ws:
        await ws.send(sub)
        print("Connected — collecting vessel data…")

        deadline = asyncio.get_event_loop().time() + STREAM_SECONDS
        msg_count = 0

        while asyncio.get_event_loop().time() < deadline:
            remaining = deadline - asyncio.get_event_loop().time()
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=min(remaining, 10))
            except asyncio.TimeoutError:
                continue

            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("MessageType", "")
            if msg_type == "ShipStaticData":
                _handle_static(msg)
            elif msg_type in ("PositionReport", "StandardClassBPositionReport",
                              "ExtendedClassBPositionReport"):
                _handle_position(msg, msg_type)

            msg_count += 1
            if msg_count % 500 == 0:
                elapsed = STREAM_SECONDS - remaining
                print(f"  {msg_count} messages, {len(vessel_pos)} positions, "
                      f"{elapsed:.0f}s elapsed")

    print(f"Done — {msg_count} total messages, {len(vessel_pos)} unique vessel positions")


# ─────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────

async def _main() -> None:
    if not API_KEY:
        print("AISSTREAM_API_KEY not set — writing empty snapshot.")
        _save_snapshot([], [])
        return

    await stream(API_KEY)

    inbound, outbound = [], []
    for mmsi in vessel_pos:
        role = _classify(mmsi)
        if role == "INBOUND":
            inbound.append(_build_record(mmsi, "INBOUND"))
        elif role == "OUTBOUND":
            outbound.append(_build_record(mmsi, "OUTBOUND"))

    # Sort by proximity
    inbound.sort(key=lambda r: r["distance_nm"])
    outbound.sort(key=lambda r: r["distance_nm"])

    print(f"Classified: {len(inbound)} inbound, {len(outbound)} outbound")
    _save_snapshot(inbound, outbound)


def _save_snapshot(inbound: list, outbound: list) -> None:
    os.makedirs("docs", exist_ok=True)
    snapshot = {
        "fetched_at":      datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "stream_seconds":  STREAM_SECONDS,
        "total_inbound":   len(inbound),
        "total_outbound":  len(outbound),
        "inbound":         inbound,
        "outbound":        outbound,
    }
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2)
    print(f"Saved {OUTPUT_FILE}  (inbound={len(inbound)}, outbound={len(outbound)})")


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()

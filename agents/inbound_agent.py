"""
InboundAgent — classifies energy tankers that are heading TO India with cargo.

Classification rule (≥2 of 3 signals must be true):
  1. Within INBOUND_NEAR_RADIUS_NM of any Indian port (and not yet IN port zone)
  2. Course vector projects near India within INBOUND_FAR_HORIZON_HOURS
  3. AIS destination field contains Indian port name / LOCODE / country
"""
import asyncio
import logging
from datetime import datetime, timezone

from agents.ais_bus import AISBus
from agents.base_agent import BaseAgent
from config import (
    AGENT_LOOP_SECONDS,
    INBOUND_NEAR_RADIUS_NM,
    classify_cargo,
    destination_is_india,
    is_heading_toward_india,
    is_in_port_zone,
    nearest_indian_port,
)
from database import db

log = logging.getLogger(__name__)

# In-memory cache: mmsi → {ship_type, cargo_category, ship_name, destination, eta}
_vessel_meta: dict[str, dict] = {}


class InboundAgent(BaseAgent):
    name = "InboundAgent"
    interval_seconds = AGENT_LOOP_SECONDS

    def __init__(self, bus: AISBus) -> None:
        super().__init__()
        bus.subscribe(self.on_ais_message)

    # ------------------------------------------------------------------
    # AIS message handler (called for every message by AISBus)
    # ------------------------------------------------------------------

    async def on_ais_message(self, message: dict) -> None:
        msg_type = message.get("MessageType", "")

        if msg_type == "ShipStaticData":
            await self._handle_static(message)
        elif msg_type in ("PositionReport", "StandardClassBPositionReport",
                          "ExtendedClassBPositionReport"):
            await self._handle_position(message)

    async def _handle_static(self, message: dict) -> None:
        meta = message.get("Message", {}).get("ShipStaticData", {})
        mmsi = str(meta.get("UserId", "") or meta.get("Mmsi", ""))
        if not mmsi:
            return

        ship_type = int(meta.get("Type", 0) or 0)
        cargo = classify_cargo(ship_type)
        name = (meta.get("Name") or "").strip()
        destination = (meta.get("Destination") or "").strip()
        eta_raw = meta.get("Eta") or {}
        eta_str = ""
        if isinstance(eta_raw, dict):
            m = eta_raw.get("Month", 0)
            d = eta_raw.get("Day", 0)
            h = eta_raw.get("Hour", 0)
            mn = eta_raw.get("Minute", 0)
            if m and d:
                year = datetime.now(timezone.utc).year
                eta_str = f"{year}-{m:02d}-{d:02d}T{h:02d}:{mn:02d}:00Z"

        _vessel_meta[mmsi] = {
            "ship_type": ship_type,
            "cargo_category": cargo,
            "ship_name": name,
            "destination": destination,
            "eta": eta_str,
            "draft": float(meta.get("MaximumStaticDraught") or 0),
        }

        await db.upsert_vessel({
            "mmsi": mmsi,
            "imo": str(meta.get("ImoNumber") or ""),
            "ship_name": name,
            "ship_type": ship_type,
            "cargo_category": cargo,
            "flag": meta.get("CallSign", ""),
            "length": float(meta.get("Dimension", {}).get("A", 0) or 0)
                      + float(meta.get("Dimension", {}).get("B", 0) or 0),
            "width": float(meta.get("Dimension", {}).get("C", 0) or 0)
                     + float(meta.get("Dimension", {}).get("D", 0) or 0),
            "draft": float(meta.get("MaximumStaticDraught") or 0),
            "deadweight": 0.0,
        })

    async def _handle_position(self, message: dict) -> None:
        msg_type = message.get("MessageType", "")
        if msg_type == "PositionReport":
            pos = message.get("Message", {}).get("PositionReport", {})
        elif msg_type == "StandardClassBPositionReport":
            pos = message.get("Message", {}).get("StandardClassBPositionReport", {})
        else:
            pos = message.get("Message", {}).get("ExtendedClassBPositionReport", {})

        mmsi = str(pos.get("UserId", "") or pos.get("Mmsi", ""))
        if not mmsi:
            return

        lat = float(pos.get("Latitude") or 0)
        lon = float(pos.get("Longitude") or 0)
        speed = float(pos.get("Sog") or pos.get("SpeedOverGround") or 0)
        course = float(pos.get("Cog") or pos.get("CourseOverGround") or 0)
        nav_status = int(pos.get("NavigationalStatus") or 15)

        # Skip implausible positions
        if lat == 0.0 and lon == 0.0:
            return

        # Skip ships already in a port zone (handled by PortActivityAgent)
        in_port, _ = is_in_port_zone(lat, lon)
        if in_port:
            await db.remove_inbound(mmsi)
            return

        # Retrieve cached metadata
        meta = _vessel_meta.get(mmsi, {})
        cargo = meta.get("cargo_category", "OTHER")
        ship_name = meta.get("ship_name", "")
        destination = meta.get("destination", "")
        eta = meta.get("eta", "")

        # --- Classification signals ---
        port_name, dist = nearest_indian_port(lat, lon)
        signal_proximity = dist <= INBOUND_NEAR_RADIUS_NM

        heading_ok, heading_port = is_heading_toward_india(lat, lon, course, speed)
        signal_heading = heading_ok

        signal_dest = destination_is_india(destination)

        score = int(signal_proximity) + int(signal_heading) + int(signal_dest)
        if score < 2:
            return  # not enough evidence this ship is inbound to India

        from config import nav_status_str
        status = nav_status_str(nav_status)

        await db.upsert_position({
            "mmsi": mmsi, "latitude": lat, "longitude": lon,
            "speed": speed, "course": course, "heading": course,
            "nav_status": nav_status,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": "AIS",
        })

        await db.upsert_inbound({
            "mmsi": mmsi,
            "destination": destination,
            "eta": eta,
            "origin_port": "",
            "origin_country": "",
            "cargo_category": cargo,
            "ship_name": ship_name,
            "current_lat": lat,
            "current_lon": lon,
            "speed": speed,
            "course": course,
            "distance_to_port": round(dist, 1),
            "nearest_port": port_name,
            "status": status,
        })

    # ------------------------------------------------------------------
    # Periodic process — cleanup stale records
    # ------------------------------------------------------------------

    async def process(self) -> None:
        # Ships that entered port zones are cleaned up in on_ais_message.
        # Ships with no update in 6h are removed.
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()

        def _remove_stale():
            from database.db import _get_conn
            conn = _get_conn()
            rows = conn.execute(
                "SELECT mmsi FROM inbound_ships WHERE last_updated < ?", (cutoff,)
            ).fetchall()
            for row in rows:
                conn.execute("DELETE FROM inbound_ships WHERE mmsi=?", (row[0],))
            conn.commit()
            return len(rows)

        removed = await asyncio.to_thread(_remove_stale)
        if removed:
            log.debug("[InboundAgent] removed %d stale inbound records", removed)

        await db.purge_old_positions()

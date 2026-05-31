"""
OutboundAgent — classifies energy tankers that recently left an Indian port
and are heading away (ballast run to pick up cargo).

Classification: ship was in a port zone within OUTBOUND_TRACK_HOURS and is
now moving away (speed > 3kn, increasing distance from that port).
Ships are retired after OUTBOUND_MAX_AGE_HOURS.
"""
import asyncio
import logging
from datetime import datetime, timezone, timedelta

from agents.ais_bus import AISBus
from agents.base_agent import BaseAgent
from config import (
    AGENT_LOOP_SECONDS,
    OUTBOUND_TRACK_HOURS,
    OUTBOUND_MAX_AGE_HOURS,
    classify_cargo,
    haversine,
    is_in_port_zone,
    nearest_indian_port,
)
from database import db

log = logging.getLogger(__name__)

# In-memory: mmsi → {port_name, departure_time, cargo_category, ship_name,
#                     last_lat, last_lon, last_dist}
_recently_departed: dict[str, dict] = {}
# In-memory vessel meta from ShipStaticData
_vessel_meta: dict[str, dict] = {}


class OutboundAgent(BaseAgent):
    name = "OutboundAgent"
    interval_seconds = AGENT_LOOP_SECONDS

    def __init__(self, bus: AISBus) -> None:
        super().__init__()
        bus.subscribe(self.on_ais_message)

    # ------------------------------------------------------------------
    # AIS message handler
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
        _vessel_meta[mmsi] = {
            "cargo_category": classify_cargo(ship_type),
            "ship_name": (meta.get("Name") or "").strip(),
            "destination": (meta.get("Destination") or "").strip(),
            "draft": float(meta.get("MaximumStaticDraught") or 0),
        }

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

        if lat == 0.0 and lon == 0.0:
            return

        in_port, port_name = is_in_port_zone(lat, lon)
        meta = _vessel_meta.get(mmsi, {})
        cargo = meta.get("cargo_category", "OTHER")
        ship_name = meta.get("ship_name", "")
        destination = meta.get("destination", "")

        if in_port:
            # Ship is (still) in port — record for departure detection
            _recently_departed[mmsi] = {
                "port_name": port_name,
                "last_time": datetime.now(timezone.utc),
                "cargo_category": cargo,
                "ship_name": ship_name,
                "last_lat": lat,
                "last_lon": lon,
                "last_dist": 0.0,
            }
            # Remove from outbound if it returned to port
            await db.remove_outbound(mmsi)
            return

        # Not in port — check if it recently departed
        if mmsi not in _recently_departed:
            return

        dep = _recently_departed[mmsi]
        time_since = (datetime.now(timezone.utc) - dep["last_time"]).total_seconds() / 3600

        if time_since > OUTBOUND_TRACK_HOURS:
            # Too long since we last saw it in port to consider it "just departed"
            _recently_departed.pop(mmsi, None)
            return

        if speed < 3.0:
            return  # Not actually moving away yet

        # Check it's moving AWAY (distance increasing)
        port_info = next((p for p in __import__("config").INDIAN_PORTS
                          if p["name"] == dep["port_name"]), None)
        if port_info is None:
            return
        dist_now = haversine(lat, lon, port_info["lat"], port_info["lon"])
        if dist_now <= dep.get("last_dist", 0.0):
            return  # distance not increasing

        dep["last_dist"] = dist_now
        dep["last_lat"] = lat
        dep["last_lon"] = lon

        # Ballast check: if current draft << typical loaded draft, flag it
        ballast_confirmed = 0
        draft = meta.get("draft", 0.0)
        if 0 < draft < 10.0:  # typical loaded VLCC draft ~20m, ballast ~8-12m
            ballast_confirmed = 1

        await db.upsert_outbound({
            "mmsi": mmsi,
            "departure_port": dep["port_name"],
            "departure_time": dep["last_time"].isoformat(),
            "destination": destination,
            "cargo_category": cargo,
            "ship_name": ship_name,
            "current_lat": lat,
            "current_lon": lon,
            "speed": speed,
            "course": course,
            "distance_from_port": round(dist_now, 1),
            "nav_status": nav_status,
            "ballast_confirmed": ballast_confirmed,
        })

    # ------------------------------------------------------------------
    # Periodic: retire old outbound records
    # ------------------------------------------------------------------

    async def process(self) -> None:
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(hours=OUTBOUND_MAX_AGE_HOURS)).isoformat()
        stale = await db.get_stale_outbound_ships(cutoff)
        for row in stale:
            await db.remove_outbound(row["mmsi"])
            _recently_departed.pop(row["mmsi"], None)
        if stale:
            log.debug("[OutboundAgent] retired %d old outbound records", len(stale))

        # Prune in-memory recently_departed cache
        horizon = datetime.now(timezone.utc) - timedelta(hours=OUTBOUND_TRACK_HOURS)
        to_drop = [m for m, v in _recently_departed.items()
                   if v["last_time"] < horizon]
        for m in to_drop:
            _recently_departed.pop(m, None)

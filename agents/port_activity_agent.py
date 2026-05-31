"""
PortActivityAgent — tracks energy tankers physically within Indian port zones.

Two data sources:
  1. Real-time AIS: any energy tanker within PORT_ZONE_RADIUS_NM of a port.
  2. Port website scrapers: run every SCRAPER_INTERVAL_SECONDS, fuse by name.

Activity inference from nav_status:
  1 (anchored) → ANCHORED
  5 (moored)   → BERTHED
  Draft change → LOADING / UNLOADING (if ShipStaticData shows changing draft)
"""
import asyncio
import logging
from datetime import datetime, timezone, timedelta

from agents.ais_bus import AISBus
from agents.base_agent import BaseAgent
from config import (
    AGENT_LOOP_SECONDS,
    SCRAPER_INTERVAL_SECONDS,
    STALE_PORT_HOURS,
    classify_cargo,
    is_in_port_zone,
)
from database import db

log = logging.getLogger(__name__)

# In-memory: mmsi → {port_name, arrival_time, cargo_category, ship_name,
#                     prev_draft, activity}
_in_port_cache: dict[str, dict] = {}
_vessel_meta: dict[str, dict] = {}
_last_scrape_time: float = 0.0


def _infer_activity(nav_status: int, speed: float,
                    prev_draft: float, curr_draft: float) -> str:
    if nav_status == 1:
        return "ANCHORED"
    if nav_status == 5:
        # Try to distinguish loading vs unloading via draft trend
        if curr_draft > 0 and prev_draft > 0:
            if curr_draft > prev_draft + 0.3:
                return "LOADING"
            if curr_draft < prev_draft - 0.3:
                return "UNLOADING"
        return "BERTHED"
    if speed < 0.5:
        return "BERTHED"
    if speed < 2.0:
        return "MANEUVERING"
    return "UNKNOWN"


class PortActivityAgent(BaseAgent):
    name = "PortActivityAgent"
    interval_seconds = AGENT_LOOP_SECONDS

    def __init__(self, bus: AISBus) -> None:
        super().__init__()
        bus.subscribe(self.on_ais_message)
        self._scraper_task: asyncio.Task | None = None

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
        draft = float(meta.get("MaximumStaticDraught") or 0)
        _vessel_meta[mmsi] = {
            "cargo_category": classify_cargo(ship_type),
            "ship_name": (meta.get("Name") or "").strip(),
            "draft": draft,
        }
        # Update draft in port cache for loading/unloading inference
        if mmsi in _in_port_cache:
            _in_port_cache[mmsi]["curr_draft"] = draft

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
        nav_status = int(pos.get("NavigationalStatus") or 15)

        if lat == 0.0 and lon == 0.0:
            return

        in_port, port_name = is_in_port_zone(lat, lon)

        if not in_port:
            if mmsi in _in_port_cache:
                # Ship has left — remove from port activity
                del _in_port_cache[mmsi]
                await db.remove_port_activity(mmsi)
            return

        meta = _vessel_meta.get(mmsi, {})
        cargo = meta.get("cargo_category", "OTHER")
        ship_name = meta.get("ship_name", "")
        curr_draft = meta.get("draft", 0.0)

        cache = _in_port_cache.get(mmsi, {})
        prev_draft = cache.get("curr_draft", 0.0)
        arrival_time = cache.get("arrival_time",
                                  datetime.now(timezone.utc).isoformat())

        activity = _infer_activity(nav_status, speed, prev_draft, curr_draft)

        _in_port_cache[mmsi] = {
            "port_name": port_name,
            "arrival_time": arrival_time,
            "cargo_category": cargo,
            "ship_name": ship_name,
            "curr_draft": curr_draft,
            "activity": activity,
        }

        await db.upsert_port_activity({
            "mmsi": mmsi,
            "port_name": port_name,
            "berth": "",
            "activity": activity,
            "cargo_category": cargo,
            "ship_name": ship_name,
            "arrival_time": arrival_time,
            "expected_departure": "",
            "current_lat": lat,
            "current_lon": lon,
            "source": "AIS",
        })

    # ------------------------------------------------------------------
    # Periodic: scrape ports + remove stale AIS records
    # ------------------------------------------------------------------

    async def process(self) -> None:
        # Remove ships not seen on AIS for STALE_PORT_HOURS
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(hours=STALE_PORT_HOURS)).isoformat()
        stale = await db.get_stale_port_ships(cutoff)
        for row in stale:
            await db.remove_port_activity(row["mmsi"])
            _in_port_cache.pop(row["mmsi"], None)

        # Run scrapers on their own interval
        import time
        global _last_scrape_time
        now = time.monotonic()
        if now - _last_scrape_time >= SCRAPER_INTERVAL_SECONDS:
            _last_scrape_time = now
            await self._run_scrapers()

    async def _run_scrapers(self) -> None:
        from scrapers.jnpt_scraper import JNPTScraper
        from scrapers.paradip_scraper import ParadipScraper
        from scrapers.mundra_scraper import MundraScraper
        from scrapers.vizag_scraper import VizagScraper

        scrapers = [JNPTScraper(), ParadipScraper(), MundraScraper(), VizagScraper()]
        results = await asyncio.gather(
            *[s.run() for s in scrapers],
            return_exceptions=True,
        )
        for scraper, result in zip(scrapers, results):
            if isinstance(result, Exception):
                log.warning("[PortActivityAgent] scraper %s failed: %s",
                            scraper.name, result)
                continue
            await self._fuse_scraped(result, scraper.name)

    async def _fuse_scraped(self, records: list[dict], source: str) -> None:
        """Merge scraper records into port_activity by fuzzy ship name match."""
        for rec in records:
            scrape_name = rec.get("ship_name", "").strip().lower()
            if not scrape_name:
                continue

            # Try to find an existing AIS record by name
            existing = await db.get_port_activity()
            matched_mmsi = None
            for row in existing:
                if row.get("ship_name", "").strip().lower() == scrape_name:
                    matched_mmsi = row["mmsi"]
                    break

            if matched_mmsi:
                # Enrich existing record with scraper data
                await db.upsert_port_activity({
                    "mmsi": matched_mmsi,
                    "port_name": rec.get("port_name", ""),
                    "berth": rec.get("berth", ""),
                    "activity": rec.get("activity", "UNKNOWN"),
                    "cargo_category": rec.get("cargo_category", "OTHER"),
                    "ship_name": rec.get("ship_name", ""),
                    "arrival_time": rec.get("arrival_time", ""),
                    "expected_departure": rec.get("expected_departure", ""),
                    "current_lat": 0.0,
                    "current_lon": 0.0,
                    "source": source,
                })
            else:
                # Scraper-only record — use ship name as synthetic MMSI key
                synthetic_mmsi = f"SCRAPE_{scrape_name[:20].replace(' ', '_').upper()}"
                await db.upsert_port_activity({
                    "mmsi": synthetic_mmsi,
                    "port_name": rec.get("port_name", ""),
                    "berth": rec.get("berth", ""),
                    "activity": rec.get("activity", "UNKNOWN"),
                    "cargo_category": rec.get("cargo_category", "OTHER"),
                    "ship_name": rec.get("ship_name", ""),
                    "arrival_time": rec.get("arrival_time", ""),
                    "expected_departure": rec.get("expected_departure", ""),
                    "current_lat": 0.0,
                    "current_lon": 0.0,
                    "source": source,
                })

"""
AggregatorAgent — reads all three tracking tables every AGGREGATOR_INTERVAL_SECONDS,
computes summary statistics, persists to aggregated_stats, and broadcasts an
update event to the WebSocket notification queue.
"""
import asyncio
import json
import logging
from collections import Counter
from datetime import datetime, timezone

from agents.base_agent import BaseAgent
from config import AGGREGATOR_INTERVAL_SECONDS
from database import db

log = logging.getLogger(__name__)

# Set by api/server.py after startup; AggregatorAgent drops events into it
_notification_queue: asyncio.Queue | None = None


def set_notification_queue(q: asyncio.Queue) -> None:
    global _notification_queue
    _notification_queue = q


class AggregatorAgent(BaseAgent):
    name = "AggregatorAgent"
    interval_seconds = AGGREGATOR_INTERVAL_SECONDS

    def __init__(self) -> None:
        super().__init__()

    async def on_ais_message(self, message: dict) -> None:
        pass  # AggregatorAgent does not process raw AIS

    async def process(self) -> None:
        try:
            stats = await self._compute_stats()
            await db.save_aggregated_stats(stats)
            if _notification_queue is not None:
                try:
                    _notification_queue.put_nowait({"type": "stats", "data": stats})
                except asyncio.QueueFull:
                    pass
            log.debug("[AggregatorAgent] stats computed: in=%d out=%d port=%d",
                      stats["total_inbound"], stats["total_outbound"],
                      stats["total_in_port"])
        except Exception as e:
            log.exception("[AggregatorAgent] compute error: %s", e)

    async def _compute_stats(self) -> dict:
        inbound  = await db.get_inbound_ships()
        outbound = await db.get_outbound_ships()
        in_port  = await db.get_port_activity()

        all_ships = inbound + outbound + in_port
        cargo_counter = Counter(s.get("cargo_category", "OTHER") for s in all_ships)

        # Busiest port
        port_counter: Counter = Counter()
        for s in in_port:
            pn = s.get("port_name", "")
            if pn:
                port_counter[pn] += 1
        busiest = port_counter.most_common(1)[0][0] if port_counter else ""

        # Ships arriving in next 24h
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
        arriving_soon = [
            s for s in inbound
            if s.get("eta") and s["eta"] <= cutoff and s["eta"] >= datetime.now(timezone.utc).isoformat()
        ]

        extended = {
            "inbound_by_cargo": dict(Counter(s.get("cargo_category") for s in inbound)),
            "outbound_by_cargo": dict(Counter(s.get("cargo_category") for s in outbound)),
            "port_by_cargo": dict(Counter(s.get("cargo_category") for s in in_port)),
            "port_counts": dict(port_counter),
            "arriving_next_24h": len(arriving_soon),
            "avg_inbound_speed": (
                round(sum(s.get("speed", 0) for s in inbound) / len(inbound), 1)
                if inbound else 0.0
            ),
        }

        return {
            "computed_at": datetime.now(timezone.utc).isoformat(),
            "total_inbound": len(inbound),
            "total_outbound": len(outbound),
            "total_in_port": len(in_port),
            "crude_count": cargo_counter.get("CRUDE", 0),
            "lng_count": cargo_counter.get("LNG", 0),
            "cng_count": cargo_counter.get("CNG", 0),
            "petroleum_count": cargo_counter.get("PETROLEUM", 0),
            "busiest_port": busiest,
            "stats_json": json.dumps(extended),
        }

    async def get_dashboard_summary(self) -> dict:
        """Return the most recent stats dict, refreshing if needed."""
        stats = await db.get_latest_stats()
        if stats:
            # Parse stats_json back to dict for the API response
            try:
                stats["extended"] = json.loads(stats.get("stats_json", "{}"))
            except Exception:
                stats["extended"] = {}
            return stats
        # No stats yet — compute on demand
        return await self._compute_stats()

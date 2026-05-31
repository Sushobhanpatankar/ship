"""
Abstract base scraper with retry, rate-limit handling, and logging.
All scrapers use httpx.AsyncClient with a realistic User-Agent.
"""
import asyncio
import logging
import random
from abc import ABC, abstractmethod

import httpx

from database import db

log = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
]


def _random_ua() -> str:
    return random.choice(USER_AGENTS)


class BaseScraper(ABC):
    name: str = "BaseScraper"
    url: str = ""
    port_name: str = ""
    timeout_seconds: int = 30
    retry_attempts: int = 3
    backoff_seconds: float = 5.0

    async def fetch(self) -> str:
        headers = {
            "User-Agent": _random_ua(),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }
        last_err: Exception | None = None
        for attempt in range(self.retry_attempts):
            try:
                async with httpx.AsyncClient(
                    timeout=self.timeout_seconds,
                    follow_redirects=True,
                    headers=headers,
                ) as client:
                    resp = await client.get(self.url)
                    if resp.status_code == 429:
                        wait = self.backoff_seconds * (attempt + 1)
                        log.warning("[%s] rate limited, waiting %ds", self.name, wait)
                        await db.log_scraper_run(self.name, "RATE_LIMITED", 0)
                        await asyncio.sleep(wait)
                        continue
                    resp.raise_for_status()
                    return resp.text
            except httpx.HTTPError as e:
                last_err = e
                log.warning("[%s] HTTP error (attempt %d): %s", self.name, attempt + 1, e)
                await asyncio.sleep(self.backoff_seconds * (attempt + 1))
        raise RuntimeError(f"{self.name} failed after {self.retry_attempts} attempts: {last_err}")

    @abstractmethod
    async def parse(self, html: str) -> list[dict]:
        """
        Parse the HTML page and return a list of ship dicts with keys:
          ship_name, berth, cargo_category, activity,
          arrival_time, expected_departure, port_name, source
        """

    async def run(self) -> list[dict]:
        try:
            html = await self.fetch()
            records = await self.parse(html)
            await db.log_scraper_run(self.name, "OK", len(records))
            log.info("[%s] scraped %d records", self.name, len(records))
            return records
        except Exception as e:
            await db.log_scraper_run(self.name, "ERROR", 0, str(e))
            log.warning("[%s] scrape failed: %s", self.name, e)
            return []

    # ------------------------------------------------------------------
    # Cargo keyword detection
    # ------------------------------------------------------------------

    CARGO_KEYWORDS: dict[str, list[str]] = {
        "CRUDE": ["crude", "vlcc", "suezmax", "aframax", "crude oil"],
        "LNG":   ["lng", "liquefied natural", "gas carrier", "lngc"],
        "CNG":   ["cng", "compressed natural", "cng carrier"],
        "PETROLEUM": [
            "petroleum", "hsd", "high speed diesel", "ms ", "motor spirit",
            "atf", "aviation turbine", "naphtha", "fuel oil", "fob", "lub oil",
            "product tanker", "petro", "refined",
        ],
    }

    def detect_cargo(self, text: str) -> str:
        t = text.lower()
        for cat, keywords in self.CARGO_KEYWORDS.items():
            if any(kw in t for kw in keywords):
                return cat
        return "OTHER"

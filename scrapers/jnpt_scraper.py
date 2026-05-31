"""
JNPT (Jawaharlal Nehru Port) — Daily Berthing Status scraper.

The berthing-report URL contains a session token that changes, so we first
fetch the main page to discover the current URL, then scrape it.
"""
import logging
import re

import httpx
from bs4 import BeautifulSoup

from scrapers.base_scraper import BaseScraper

log = logging.getLogger(__name__)

JNPT_MAIN = "https://www.jnport.gov.in/"


class JNPTScraper(BaseScraper):
    name = "JNPT"
    port_name = "JNPT"
    # fallback if discovery fails
    url = "https://www.jnport.gov.in/page/berthing-report/M2VlS0pwUXZ3akhSV0E0RDFUVlhxQT09"

    async def fetch(self) -> str:
        """Override to discover current berthing-report URL from the main page."""
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }
        async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=headers) as client:
            # Discover current URL from the main page
            try:
                main_resp = await client.get(JNPT_MAIN)
                soup = BeautifulSoup(main_resp.text, "html.parser")
                report_url = None
                for a in soup.find_all("a", href=True):
                    href = a["href"]
                    if "berthing-report" in href or "berthing-status" in href:
                        if href.startswith("http"):
                            report_url = href
                        else:
                            report_url = "https://www.jnport.gov.in/" + href.lstrip("/")
                        break
                if report_url:
                    log.info("[JNPT] discovered URL: %s", report_url)
                    self.url = report_url
            except Exception as e:
                log.warning("[JNPT] URL discovery failed, using fallback: %s", e)

            resp = await client.get(self.url)
            resp.raise_for_status()
            return resp.text

    async def parse(self, html: str) -> list[dict]:
        soup = BeautifulSoup(html, "html.parser")
        records: list[dict] = []

        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            if len(rows) < 2:
                continue

            # Detect header row
            header_cells = rows[0].find_all(["th", "td"])
            headers = [th.get_text(strip=True).lower() for th in header_cells]

            col_ship    = _find_col(headers, ["vessel", "ship name", "name"])
            col_berth   = _find_col(headers, ["berth", "jetty", "terminal"])
            col_cargo   = _find_col(headers, ["cargo", "commodity"])
            col_arrival = _find_col(headers, ["berthed on", "arrival", "arrived"])
            col_etd     = _find_col(headers, ["expected", "departure", "completion"])

            if col_ship is None:
                continue

            for row in rows[1:]:
                cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
                if len(cells) < 2:
                    continue

                ship_name = _safe_get(cells, col_ship)
                if not ship_name or len(ship_name) < 3:
                    continue

                berth      = _safe_get(cells, col_berth) or ""
                cargo_text = _safe_get(cells, col_cargo) or ""
                arrival    = _safe_get(cells, col_arrival) or ""
                etd        = _safe_get(cells, col_etd) or ""

                cargo_cat = self.detect_cargo(cargo_text + " " + ship_name)
                if cargo_cat == "OTHER":
                    continue  # JNPT is primarily containers — skip unknown

                records.append({
                    "ship_name": ship_name,
                    "berth": berth,
                    "cargo_category": cargo_cat,
                    "activity": "BERTHED" if berth else "ANCHORED",
                    "arrival_time": arrival,
                    "expected_departure": etd,
                    "port_name": self.port_name,
                    "source": self.name,
                })

        log.debug("[JNPT] parsed %d energy vessel records", len(records))
        return records


def _find_col(headers: list[str], keywords: list[str]) -> int | None:
    for i, h in enumerate(headers):
        if any(kw in h for kw in keywords):
            return i
    return None


def _safe_get(cells: list[str], idx: int | None) -> str:
    if idx is None or idx >= len(cells):
        return ""
    return cells[idx].strip()

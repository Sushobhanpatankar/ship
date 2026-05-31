"""
Visakhapatnam (Vizag) Port — vessel schedule scraper.

Note: www.vizagport.com has an SSL hostname mismatch on the /vessel-position/
path. We use verify=False and fall back to Vizag's port info site if needed.
"""
import logging

import httpx
from bs4 import BeautifulSoup

from scrapers.base_scraper import BaseScraper

log = logging.getLogger(__name__)

VIZAG_URLS = [
    "https://www.vizagport.com/vessel-position/",
    "https://www.vizagport.com/vessel-schedule/",
    "https://www.vizagport.com/port-services/",
]


class VizagScraper(BaseScraper):
    name = "Vizag"
    url = VIZAG_URLS[0]
    port_name = "Vizag"

    async def fetch(self) -> str:
        """Override to try multiple URLs and bypass SSL hostname mismatch."""
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        # verify=False because Vizag uses a cert that doesn't cover www.vizagport.com
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds,
            follow_redirects=True,
            verify=False,
            headers=headers,
        ) as client:
            for url in VIZAG_URLS:
                try:
                    resp = await client.get(url)
                    if resp.status_code == 200 and len(resp.text) > 500:
                        soup = BeautifulSoup(resp.text, "html.parser")
                        # Only accept if there's actual vessel data
                        if soup.find("table") or any(
                            kw in resp.text.lower()
                            for kw in ["vessel", "berth", "ship", "cargo"]
                        ):
                            log.info("[Vizag] got data from %s", url)
                            return resp.text
                except Exception as e:
                    log.debug("[Vizag] %s failed: %s", url, e)

        raise RuntimeError("Vizag: no working URL found — port site may be down")

    async def parse(self, html: str) -> list[dict]:
        soup = BeautifulSoup(html, "html.parser")
        records: list[dict] = []

        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            if len(rows) < 2:
                continue

            headers = [th.get_text(strip=True).lower()
                       for th in rows[0].find_all(["th", "td"])]

            col_ship    = _find_col(headers, ["vessel", "ship", "name"])
            col_berth   = _find_col(headers, ["berth", "terminal", "jetty", "wharf"])
            col_cargo   = _find_col(headers, ["cargo", "commodity", "type", "nature"])
            col_arrival = _find_col(headers, ["arrival", "eta", "arrived", "date"])
            col_etd     = _find_col(headers, ["departure", "etd"])

            if col_ship is None:
                continue

            for row in rows[1:]:
                cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
                if len(cells) < 2:
                    continue

                ship_name = _safe_get(cells, col_ship)
                if not ship_name or len(ship_name) < 3:
                    continue

                berth      = _safe_get(cells, col_berth)   or ""
                cargo_text = _safe_get(cells, col_cargo)   or ""
                arrival    = _safe_get(cells, col_arrival) or ""
                etd        = _safe_get(cells, col_etd)     or ""

                cargo_cat = self.detect_cargo(cargo_text + " " + ship_name)
                if cargo_cat == "OTHER":
                    cargo_cat = "PETROLEUM"  # Vizag default

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

        log.debug("[Vizag] parsed %d energy vessel records", len(records))
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

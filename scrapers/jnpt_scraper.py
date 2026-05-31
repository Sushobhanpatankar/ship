"""
JNPT (Jawaharlal Nehru Port) — Daily Berthing Report scraper.
Source: https://www.jnport.gov.in/page/daily-berthing-report/
"""
import logging
import re

from bs4 import BeautifulSoup

from scrapers.base_scraper import BaseScraper

log = logging.getLogger(__name__)


class JNPTScraper(BaseScraper):
    name = "JNPT"
    url = "https://www.jnport.gov.in/page/daily-berthing-report/"
    port_name = "JNPT"

    async def parse(self, html: str) -> list[dict]:
        soup = BeautifulSoup(html, "html.parser")
        records: list[dict] = []

        # Find all tables on the page
        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            if len(rows) < 2:
                continue

            # Detect header row to find column indices
            header_row = rows[0]
            headers = [th.get_text(strip=True).lower() for th in
                       header_row.find_all(["th", "td"])]

            col_ship = _find_col(headers, ["vessel", "ship", "name"])
            col_berth = _find_col(headers, ["berth", "jetty", "terminal"])
            col_cargo = _find_col(headers, ["cargo", "commodity", "type"])
            col_arrival = _find_col(headers, ["arrival", "arrived", "date"])
            col_departure = _find_col(headers, ["departure", "etd", "expected"])

            if col_ship is None:
                continue

            for row in rows[1:]:
                cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
                if len(cells) < 2:
                    continue

                ship_name = _safe_get(cells, col_ship)
                if not ship_name or len(ship_name) < 3:
                    continue

                berth = _safe_get(cells, col_berth) or ""
                cargo_text = _safe_get(cells, col_cargo) or ""
                arrival = _safe_get(cells, col_arrival) or ""
                departure = _safe_get(cells, col_departure) or ""

                cargo_cat = self.detect_cargo(cargo_text + " " + ship_name)
                if cargo_cat == "OTHER":
                    continue  # Skip non-energy cargo

                records.append({
                    "ship_name": ship_name,
                    "berth": berth,
                    "cargo_category": cargo_cat,
                    "activity": _infer_activity(berth, cargo_text),
                    "arrival_time": arrival,
                    "expected_departure": departure,
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


def _infer_activity(berth: str, cargo: str) -> str:
    if not berth:
        return "ANCHORED"
    return "BERTHED"

"""
Paradip Port — vessel line-up scraper.
Paradip is primarily a crude oil port (handles ~60MT crude/year).
"""
import logging

from bs4 import BeautifulSoup

from scrapers.base_scraper import BaseScraper

log = logging.getLogger(__name__)


class ParadipScraper(BaseScraper):
    name = "Paradip"
    url = "https://www.paradipport.gov.in/vessel-schedule.aspx"
    port_name = "Paradip"

    async def parse(self, html: str) -> list[dict]:
        soup = BeautifulSoup(html, "html.parser")
        records: list[dict] = []

        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            if len(rows) < 2:
                continue

            header_row = rows[0]
            headers = [th.get_text(strip=True).lower()
                       for th in header_row.find_all(["th", "td"])]

            col_ship    = _find_col(headers, ["vessel", "ship", "name"])
            col_berth   = _find_col(headers, ["berth", "jetty", "terminal", "wharf"])
            col_cargo   = _find_col(headers, ["cargo", "commodity", "type", "nature"])
            col_arrival = _find_col(headers, ["arrival", "arrived", "eta", "date"])
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

                # Default Paradip cargo to CRUDE unless specified otherwise
                cargo_cat = self.detect_cargo(cargo_text + " " + ship_name)
                if cargo_cat == "OTHER":
                    cargo_cat = "CRUDE"  # Paradip default

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

        # Fallback: try unordered list or div-based layouts
        if not records:
            records = self._parse_fallback(soup)

        log.debug("[Paradip] parsed %d energy vessel records", len(records))
        return records

    def _parse_fallback(self, soup: BeautifulSoup) -> list[dict]:
        records = []
        # Look for any text blocks containing vessel names
        for tag in soup.find_all(["p", "li", "div"]):
            text = tag.get_text(strip=True)
            if len(text) < 5 or len(text) > 200:
                continue
            # Very basic: if line contains a known pattern like "MV " or "MT "
            if any(text.upper().startswith(prefix) for prefix in
                   ["MV ", "MT ", "M.T ", "M.V ", "SS ", "FSO ", "VLCC "]):
                cargo_cat = self.detect_cargo(text)
                if cargo_cat == "OTHER":
                    cargo_cat = "CRUDE"
                records.append({
                    "ship_name": text[:50],
                    "berth": "",
                    "cargo_category": cargo_cat,
                    "activity": "ANCHORED",
                    "arrival_time": "",
                    "expected_departure": "",
                    "port_name": self.port_name,
                    "source": self.name,
                })
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

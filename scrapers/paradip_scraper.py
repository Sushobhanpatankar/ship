"""
Paradip Port Authority — vessel berth allotment scraper.
Source: PPT_VIEW public dashboard (https://www.paradipport.gov.in/ppt_berth/PPT_VIEW/Dashboard_public.aspx)

Paradip is India's largest crude oil port (~60 MT/year).
SPM (Single Point Mooring) berth = crude tanker (VLCC/Suezmax).
"""
import logging

from bs4 import BeautifulSoup

from scrapers.base_scraper import BaseScraper

log = logging.getLogger(__name__)

# Berth → cargo hints
BERTH_CARGO_HINTS = {
    "SPM": "CRUDE",   # Single Point Mooring — crude VLCCs
    "EQ":  "CRUDE",   # Equip quay
    "OQ":  "CRUDE",   # Oil quay
}


class ParadipScraper(BaseScraper):
    name = "Paradip"
    url = "https://www.paradipport.gov.in/ppt_berth/PPT_VIEW/Dashboard_public.aspx"
    port_name = "Paradip"

    async def parse(self, html: str) -> list[dict]:
        soup = BeautifulSoup(html, "html.parser")
        records: list[dict] = []

        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            if len(rows) < 2:
                continue

            headers = [th.get_text(strip=True).lower()
                       for th in rows[0].find_all(["th", "td"])]

            col_ship    = _find_col(headers, ["vessel", "ship", "name of"])
            col_berth   = _find_col(headers, ["berth allotted", "berth", "jetty"])
            col_cargo   = _find_col(headers, ["cargo", "commodity", "nature", "remarks"])
            col_arrival = _find_col(headers, ["arrival", "arrived", "readiness"])
            col_etd     = _find_col(headers, ["sailing", "departure", "exp."])

            if col_ship is None:
                continue

            for row in rows[1:]:
                cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
                if len(cells) < 3:
                    continue

                ship_name = _safe_get(cells, col_ship)
                if not ship_name or len(ship_name) < 3 or ship_name.isdigit():
                    continue

                berth      = _safe_get(cells, col_berth) or ""
                cargo_text = _safe_get(cells, col_cargo) or ""
                arrival    = _safe_get(cells, col_arrival) or ""
                etd        = _safe_get(cells, col_etd) or ""

                # Determine cargo: berth name hints > text detection > default (CRUDE)
                berth_upper = berth.upper().strip()
                cargo_cat = None
                for hint_berth, hint_cargo in BERTH_CARGO_HINTS.items():
                    if berth_upper.startswith(hint_berth):
                        cargo_cat = hint_cargo
                        break
                if cargo_cat is None:
                    cargo_cat = self.detect_cargo(cargo_text + " " + ship_name)
                if cargo_cat == "OTHER":
                    cargo_cat = "CRUDE"  # Paradip default

                records.append({
                    "ship_name": ship_name.lstrip("0123456789. "),
                    "berth": berth,
                    "cargo_category": cargo_cat,
                    "activity": "BERTHED" if berth else "ANCHORED",
                    "arrival_time": arrival,
                    "expected_departure": etd,
                    "port_name": self.port_name,
                    "source": self.name,
                })

        log.debug("[Paradip] parsed %d vessel records", len(records))
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

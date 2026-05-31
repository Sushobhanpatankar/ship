"""
Mundra Port (Adani Ports) — vessel schedule scraper.

The Mundra schedule page has four tables:
  table[0]  Berthed vessels   – headers: Berth no. | Vessels Name | Imp or Exp | Cargo | ETC
  table[1]  Anchored vessels  – headers: SBU Name. | Vessels Name | Imp or Exp | ATA
  table[2]  Expected arrivals – headers: SBU Name. | Vessels Name | Imp or Exp | ETA  (not yet at port – skip)
  table[3]  Departed vessels  – headers: SBU Name  | Vessels Name | POB | PD | ATUB   (already left – skip)

The SBU Name column (index 0 in tables 1-3) is a terminal/commodity label like
"FOR LIQUID CARGO VSL OPERATION" — useful for cargo detection when no Cargo
column exists, but must NOT be confused with the vessel name.
"""
import logging

from bs4 import BeautifulSoup

from scrapers.base_scraper import BaseScraper

log = logging.getLogger(__name__)

# Keywords that identify columns that mean the table should be skipped
_SKIP_TABLE_KEYWORDS = [
    "unberthing",   # table[3]: already departed
    "atub",         # table[3]: Actual Time of Unberthing
]

# SBU Name prefixes → cargo category overrides
_SBU_CARGO_MAP = {
    "LIQUEFIED NATURAL GAS": "LNG",
    "LNG":                   "LNG",
    "LIQUEFIED PETROLEUM":   "PETROLEUM",
    "LPG":                   "PETROLEUM",
    "SPM":                   "CRUDE",       # Single Point Mooring = crude tanker berth
    "HMEL SPM":              "CRUDE",       # HPCL-Mittal SPM crude terminal
    "LIQUID CARGO":          "PETROLEUM",   # generic liquid / petroleum products
    "CRUDE":                 "CRUDE",
}


class MundraScraper(BaseScraper):
    name = "Mundra"
    url = "https://www.adaniports.com/ports-and-terminals/mundra-port/vesselschedule"
    port_name = "Mundra"

    async def parse(self, html: str) -> list[dict]:
        soup = BeautifulSoup(html, "html.parser")
        records: list[dict] = []

        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            if len(rows) < 2:
                continue

            headers = [th.get_text(strip=True).lower()
                       for th in rows[0].find_all(["th", "td"])]

            # Skip departed-vessel table
            if any(kw in " ".join(headers) for kw in _SKIP_TABLE_KEYWORDS):
                continue

            # Skip future-arrivals table (has ETA but no ATA/berth)
            # Identified by having "eta" but no "berth" or "ata" column
            has_eta   = any("eta" in h and "ata" not in h for h in headers)
            has_berth = any("berth" in h for h in headers)
            has_ata   = any(h == "ata" or "ata" in h for h in headers)
            if has_eta and not has_berth and not has_ata:
                continue   # table[2]: expected arrivals — ships not yet at port

            # NOTE: use "vessel" / "ship" only — "name" also appears in "sbu name."
            col_ship    = _find_col(headers, ["vessel", "ship"])
            col_berth   = _find_col(headers, ["berth", "terminal", "jetty"])
            col_cargo   = _find_col(headers, ["cargo", "commodity"])
            col_arrival = _find_col(headers, ["arrival", "eta", "ata", "arrived"])

            if col_ship is None:
                continue

            for row in rows[1:]:
                cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
                if len(cells) < 2:
                    continue

                ship_name = _safe_get(cells, col_ship)
                if not ship_name or len(ship_name) < 3:
                    continue
                # Skip placeholder entries
                if ship_name.upper() in ("VACANT", "N/A", "-", "TBA"):
                    continue

                berth      = _safe_get(cells, col_berth)   or ""
                arrival    = _safe_get(cells, col_arrival) or ""
                cargo_text = _safe_get(cells, col_cargo)   or ""

                # When there is no dedicated Cargo column, use the SBU Name (index 0)
                # as a cargo hint — it contains labels like "FOR LIQUID CARGO VSL OPERATION"
                sbu_name = cells[0] if cells else ""
                if not cargo_text:
                    cargo_text = sbu_name

                cargo_cat = self.detect_cargo(cargo_text + " " + ship_name)

                # Strengthen cargo from SBU name when detect_cargo falls back to OTHER
                if cargo_cat == "OTHER":
                    cargo_cat = _sbu_to_cargo(sbu_name)

                if cargo_cat == "OTHER":
                    continue  # Mundra handles many cargo types; skip non-energy

                records.append({
                    "ship_name": ship_name,
                    "berth": berth,
                    "cargo_category": cargo_cat,
                    "activity": "BERTHED" if berth else "ANCHORED",
                    "arrival_time": arrival,
                    "port_name": self.port_name,
                    "source": self.name,
                })

        log.debug("[Mundra] parsed %d energy vessel records", len(records))
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


def _sbu_to_cargo(sbu_name: str) -> str:
    """Map an SBU Name label to a cargo category."""
    upper = sbu_name.upper()
    for fragment, category in _SBU_CARGO_MAP.items():
        if fragment in upper:
            return category
    return "OTHER"

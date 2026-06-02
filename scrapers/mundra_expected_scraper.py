"""
Mundra Port (Adani Ports) — expected vessel arrivals scraper.

The vessel schedule page has four tables:
  table[0]  Berthed vessels   — Berth no. | Vessel Name | Imp/Exp | Cargo | ETC
  table[1]  Anchored vessels  — SBU Name  | Vessel Name | Imp/Exp | ATA
  table[2]  Expected arrivals — SBU Name  | Vessel Name | Imp/Exp | ETA   ← this file
  table[3]  Departed vessels  — already left, skipped

The existing MundraScraper deliberately skips table[2].
This scraper targets table[2] only, returning EXPECTED records.

Cargo type is derived from the SBU Name column (same mapping as MundraScraper).
"""
import logging
import re
from datetime import datetime, timezone

from bs4 import BeautifulSoup

from scrapers.base_scraper import BaseScraper

log = logging.getLogger(__name__)

SCHEDULE_URL = "https://www.adaniports.com/ports-and-terminals/mundra-port/vesselschedule"

# SBU Name keyword → cargo category
_SBU_MAP: list[tuple[str, str]] = [
    ("LIQUEFIED NATURAL GAS", "LNG"),
    ("LNG",                   "LNG"),
    ("LIQUEFIED PETROLEUM",   "PETROLEUM"),
    ("LPG",                   "PETROLEUM"),
    ("SPM",                   "CRUDE"),
    ("HMEL SPM",              "CRUDE"),
    ("CRUDE",                 "CRUDE"),
    ("LIQUID CARGO",          "PETROLEUM"),
    ("PETROLEUM",             "PETROLEUM"),
]


def _sbu_to_cargo(sbu: str) -> str:
    upper = sbu.upper()
    for fragment, cat in _SBU_MAP:
        if fragment in upper:
            return cat
    return "OTHER"


def _parse_eta(raw: str) -> str:
    """Normalise ETA string to 'DD-MM-YYYY HH:MM', or return raw if unparseable."""
    raw = raw.strip()
    # Try DD-MM-YYYY HH:MM (already correct format)
    if re.match(r'\d{2}-\d{2}-\d{4}\s+\d{2}:\d{2}', raw):
        return raw
    # Try DD-MM-YYYY HH:MM without seconds
    m = re.match(r'(\d{2}-\d{2}-\d{4})\s+(\d{2}:\d{2})', raw)
    if m:
        return f"{m.group(1)} {m.group(2)}"
    return raw


def _is_future(eta_str: str) -> bool:
    """Return True if the ETA is today or in the future (UTC)."""
    try:
        # Accept DD-MM-YYYY HH:MM
        dt = datetime.strptime(eta_str[:16], "%d-%m-%Y %H:%M").replace(tzinfo=timezone.utc)
        return dt.date() >= datetime.now(timezone.utc).date()
    except ValueError:
        return True   # can't parse → keep it


class MundraExpectedScraper(BaseScraper):
    """Scrapes the 'Vessels Expected' table from Mundra's vessel schedule page."""

    name      = "MundraExpected"
    url       = SCHEDULE_URL
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
            header_str = " ".join(headers)

            # Target: has "eta" but NOT "ata" / "berth" / "unberthing" / "atub"
            has_eta    = "eta" in header_str and "ata" not in header_str
            has_berth  = "berth" in header_str
            has_depart = any(kw in header_str for kw in ("atub", "unberthing", "pob"))

            if not has_eta or has_berth or has_depart:
                continue

            # Column indices
            col_sbu    = _find_col(headers, ["sbu"])
            col_vessel = _find_col(headers, ["vessel", "ship"])
            col_ie     = _find_col(headers, ["imp", "exp", "import", "export"])
            col_eta    = _find_col(headers, ["eta"])

            if col_vessel is None or col_eta is None:
                continue

            for row in rows[1:]:
                cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
                if len(cells) < 2:
                    continue

                vessel = _safe_get(cells, col_vessel)
                if not vessel or len(vessel) < 3:
                    continue
                if vessel.upper() in ("VACANT", "N/A", "-", "TBA", "VESSEL NAME"):
                    continue

                eta_raw = _safe_get(cells, col_eta)
                eta     = _parse_eta(eta_raw)

                # Skip stale records (old dates sometimes in the table)
                if not _is_future(eta):
                    continue

                sbu      = _safe_get(cells, col_sbu) if col_sbu is not None else ""
                imp_exp  = _safe_get(cells, col_ie)  if col_ie  is not None else ""

                # Cargo from SBU name, then keyword detect on vessel name
                cargo_cat = _sbu_to_cargo(sbu)
                if cargo_cat == "OTHER":
                    cargo_cat = self.detect_cargo(sbu + " " + vessel)
                if cargo_cat == "OTHER":
                    continue    # Mundra handles many non-energy cargo types; skip

                direction = "OUTBOUND" if imp_exp.upper().startswith("E") else "INBOUND"

                records.append({
                    "ship_name":          vessel,
                    "cargo_category":     cargo_cat,
                    "activity":           "EXPECTED",
                    "eta":                eta,
                    "quantity_mt":        0,
                    "direction":          direction,
                    "port_name":          self.port_name,
                    "source":             self.name,
                    "arrival_time":       eta,
                    "expected_departure": "",
                    "berth":              "",
                })

        log.debug("[MundraExpected] parsed %d expected vessel records", len(records))
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

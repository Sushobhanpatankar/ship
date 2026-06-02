"""
Paradip Port Authority — expected vessel arrivals scraper.

Parses Section C ("Expected Vessels") of the Daily Traffic Report (DTR) PDF,
published every day at:
  https://www.paradipport.gov.in/Writereaddata/Daily_Traffic/dtrDDMM.pdf
  e.g. dtr0206.pdf for 2 June.

Each row in Section C covers a vessel arriving within the next ~7 days and
includes: vessel name, LOA, cargo type, D/L flag, quantity (MT), agent, ETA.
"""
import io
import logging
import re
from datetime import datetime, timezone

import httpx
import pdfplumber

from scrapers.base_scraper import BaseScraper

log = logging.getLogger(__name__)

DTR_BASE = "https://www.paradipport.gov.in/Writereaddata/Daily_Traffic"

# Cargo keyword → category (checked against each row's text)
_CARGO_MAP: list[tuple[str, str]] = [
    ("CRUDE OIL", "CRUDE"),
    ("CRUDE",     "CRUDE"),
    ("HSD",       "PETROLEUM"),
    ("HIGH SPEED", "PETROLEUM"),
    ("MOTOR SPIRIT", "PETROLEUM"),
    ("NAPHTHA",   "PETROLEUM"),
    ("FUEL OIL",  "PETROLEUM"),
    ("LPG",       "PETROLEUM"),
    ("POL",       "PETROLEUM"),
    ("LNG",       "LNG"),
    ("CNG",       "CNG"),
]

# Regex: ETA embedded in a row looks like "05-06 1200" or "08-06 2000"
_ETA_RE   = re.compile(r'(\d{2}-\d{2})\s+(\d{3,4})')
# LOA is a 3–4 digit float right after the vessel name
_LOA_RE   = re.compile(r'\d{2,4}\.\d{1,2}')
# D/L flag (word boundary so we don't match middle of words)
_DL_RE    = re.compile(r'\b([DL])\b')
# Cargo quantity: "15,000" / "124,659" or plain "15000" / "124659"
_QTY_RE   = re.compile(r'\b(\d{1,3},\d{3}|\d{5,7})\b')
# Date-group header inside Section C: "02-Jun-2026" or "2 June 2026" etc.
_DATE_HDR = re.compile(r'\b\d{1,2}[-\s][A-Za-z]+[-\s]\d{4}\b')


def _dtr_url() -> str:
    now = datetime.now(timezone.utc)
    return f"{DTR_BASE}/dtr{now.strftime('%d%m')}.pdf"


def _detect_cargo(text: str) -> str:
    upper = text.upper()
    for keyword, cat in _CARGO_MAP:
        if keyword in upper:
            return cat
    return "OTHER"


def _parse_row(line: str) -> dict | None:
    """
    Parse one vessel row from Section C.
    Returns a dict or None if the line doesn't look like a vessel row.
    """
    eta_m = _ETA_RE.search(line)
    if not eta_m:
        return None

    loa_m = _LOA_RE.search(line)
    if not loa_m:
        return None

    # Vessel name: everything before the LOA number, stripped of prefixes
    raw_name = line[:loa_m.start()].strip()
    # Remove serial numbers like "1 " or "12 " at the start
    raw_name = re.sub(r'^\d+\s+', '', raw_name).strip()
    # Normalise MT./MV. prefix
    vessel = re.sub(r'^(MT\.|MV\.|SS\.|MSC\s)', '', raw_name, flags=re.I).strip()
    # Re-add the MT/MV prefix without dot for display
    if raw_name.upper().startswith("MT"):
        vessel = "MT " + vessel
    elif raw_name.upper().startswith("MV"):
        vessel = "MV " + vessel
    if len(vessel) < 3:
        return None

    # Cargo text: chunk between LOA and the ETA
    between = line[loa_m.end():eta_m.start()]
    cargo_text = between.strip()
    cargo_cat = _detect_cargo(cargo_text + " " + line)
    if cargo_cat == "OTHER":
        return None   # skip non-energy cargo (coal, iron ore, steel, etc.)

    # D/L flag
    dl_m = _DL_RE.search(between)
    direction = "INBOUND" if (not dl_m or dl_m.group(1) == "D") else "OUTBOUND"

    # Cargo quantity
    qty_m = _QTY_RE.search(between)
    qty = int(qty_m.group(1).replace(",", "")) if qty_m else 0

    # ETA string: "DD-MM HH:MM"
    eta_time = eta_m.group(2).zfill(4)
    eta_str  = f"{eta_m.group(1)} {eta_time[:2]}:{eta_time[2:]}"

    return {
        "ship_name":          vessel,
        "cargo_category":     cargo_cat,
        "activity":           "EXPECTED",
        "eta":                eta_str,
        "quantity_mt":        qty,
        "direction":          direction,
        "port_name":          "Paradip",
        "source":             "ParadipDTR",
        "arrival_time":       eta_str,
        "expected_departure": "",
        "berth":              "",
    }


class ParadipExpectedScraper(BaseScraper):
    """Scrapes Section C of today's Paradip Daily Traffic Report PDF."""

    name      = "ParadipExpected"
    url       = DTR_BASE          # used only by BaseScraper.fetch(); we override run()
    port_name = "Paradip"

    # Override run() — we need PDF bytes, not HTML text
    async def run(self) -> list[dict]:
        url = _dtr_url()
        log.info("[ParadipExpected] fetching %s", url)
        try:
            async with httpx.AsyncClient(
                timeout=30,
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0"},
            ) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                if resp.content[:4] != b"%PDF":
                    raise RuntimeError(f"not a PDF — got {resp.content[:8]!r}")
            records = self._parse_pdf(resp.content)
            log.info("[ParadipExpected] %d expected vessel records", len(records))
            return records
        except Exception as e:
            log.warning("[ParadipExpected] failed: %s", e)
            return []

    async def parse(self, html: str) -> list[dict]:   # required by ABC; not used
        return []

    # ------------------------------------------------------------------

    def _parse_pdf(self, pdf_bytes: bytes) -> list[dict]:
        records: list[dict] = []
        try:
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                in_section = False
                for page in pdf.pages:
                    text = page.extract_text() or ""
                    upper = text.upper()

                    # Detect Section C header — PDF sometimes has typo "VESSSEL"
                    if "EXPECTED VES" in upper:
                        in_section = True

                    if not in_section:
                        continue

                    for line in text.splitlines():
                        stripped = line.strip()
                        if not stripped:
                            continue
                        # Skip date-group headers and section titles
                        if _DATE_HDR.search(stripped):
                            continue
                        if re.match(r'^[A-Z\s]+$', stripped) and len(stripped) < 40:
                            continue   # all-caps heading line
                        rec = _parse_row(stripped)
                        if rec:
                            records.append(rec)

                    # Stop after section D starts
                    if in_section and "BERTHING MOVEMENT" in upper:
                        break

        except Exception as e:
            log.warning("[ParadipExpected] PDF parse error: %s", e)

        # Deduplicate by ship name
        seen: set[str] = set()
        unique = []
        for r in records:
            key = r["ship_name"].upper()
            if key not in seen:
                seen.add(key)
                unique.append(r)

        return unique

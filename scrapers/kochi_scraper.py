"""
Cochin Port Authority (Kochi) — vessel position scraper.
Source: https://cochinport.gov.in/shipsinport

Data is published as a daily PDF ("Vessel Position as on DD.MM.YYYY 07:00").
We scrape /shipsinport to find the current PDF link (a tabcontents upload),
download it, and extract vessel rows using pdfplumber.

Kochi is India's primary LNG import terminal.
Key berth codes: LNG = LNG carrier, SPM = crude, COT = crude oil terminal,
NCB/BTP/STB/NTB = product tankers.
"""
import io
import logging
import re

import httpx
import pdfplumber
from bs4 import BeautifulSoup

from database import db
from scrapers.base_scraper import BaseScraper

log = logging.getLogger(__name__)

SHIPS_IN_PORT_URL = "https://cochinport.gov.in/shipsinport"

# Berth prefix → cargo category (used only when cargo text is absent/unknown)
# COT excluded — it handles both crude and petroleum products
BERTH_CARGO_MAP = {
    "LNG":  "LNG",
    "SPM":  "CRUDE",
    "NCB":  "PETROLEUM",
    "BTP":  "PETROLEUM",
    "SCB":  "PETROLEUM",
    "NTB":  "PETROLEUM",
    "STB":  "PETROLEUM",
}

# Cargo column text → category
CARGO_TEXT_MAP = {
    "CRUDE OIL":    "CRUDE",
    "CRUDE":        "CRUDE",
    "LNG":          "LNG",
    "LIQUEFIED":    "LNG",
    "NAPHTHA":      "PETROLEUM",
    "MOTOR SPIRIT": "PETROLEUM",
    "M S":          "PETROLEUM",   # abbreviated motor spirit
    "HSD":          "PETROLEUM",
    "HIGH SPEED":   "PETROLEUM",
    "FUEL OIL":     "PETROLEUM",
    "ATF":          "PETROLEUM",
    "PETROLEUM":    "PETROLEUM",
    "CNG":          "CNG",
    "COMPRESSED":   "CNG",
}

# Cargo types to discard entirely (non-energy)
_DISCARD_CARGO = {
    "container", "barge", "passenger", "tug", "research",
    "dredger", "offshore supply", "roro", "general cargo",
    "dry bulk", "coal", "fertilizer", "cement", "salt", "grain", "iron ore",
}


class KochiScraper(BaseScraper):
    name = "Kochi"
    url = SHIPS_IN_PORT_URL
    port_name = "Kochi"

    # Override run() to keep PDF as bytes throughout — avoids str encode/decode
    async def run(self) -> list[dict]:
        try:
            pdf_bytes = await self._fetch_pdf()
            records = self._parse_pdf(pdf_bytes)
            await db.log_scraper_run(self.name, "OK", len(records))
            log.info("[Kochi] scraped %d energy vessel records", len(records))
            return records
        except Exception as e:
            await db.log_scraper_run(self.name, "ERROR", 0, str(e))
            log.warning("[Kochi] scrape failed: %s", e)
            return []

    # parse() required by ABC — not used (run() calls _parse_pdf directly)
    async def parse(self, html: str) -> list[dict]:  # pragma: no cover
        return []

    # ------------------------------------------------------------------

    async def _fetch_pdf(self) -> bytes:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        }
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds,
            follow_redirects=True,
            headers=headers,
        ) as client:
            resp = await client.get(SHIPS_IN_PORT_URL)
            resp.raise_for_status()
            pdf_url = _find_vessel_position_pdf(resp.text)
            if not pdf_url:
                raise RuntimeError("could not find Vessel Position PDF link")
            log.info("[Kochi] downloading PDF: %s", pdf_url)
            pdf_resp = await client.get(pdf_url)
            pdf_resp.raise_for_status()
            if not pdf_resp.content[:4] == b"%PDF":
                raise RuntimeError(f"response is not a PDF (got {pdf_resp.content[:8]!r})")
            return pdf_resp.content

    def _parse_pdf(self, pdf_bytes: bytes) -> list[dict]:
        records: list[dict] = []
        try:
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                for page in pdf.pages:
                    for table in page.extract_tables():
                        records.extend(_parse_table(table, self))
        except Exception as e:
            log.warning("[Kochi] PDF parse error: %s", e)
        return records


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def _find_vessel_position_pdf(html: str) -> str | None:
    """
    The /shipsinport page has three tabcontents PDF links (empty anchor text).
    The first tabcontents PDF is always the Vessel Position report.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Prefer tabcontents PDFs — first one is Vessel Position
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "tabcontents" in href and href.lower().endswith(".pdf"):
            return _absolute(href)

    # Fallback: any PDF link
    for a in soup.find_all("a", href=True):
        if a["href"].lower().endswith(".pdf"):
            return _absolute(a["href"])

    return None


def _absolute(href: str) -> str:
    if href.startswith("http"):
        return href
    return "https://cochinport.gov.in" + ("" if href.startswith("/") else "/") + href


def _berth_to_cargo(berth: str) -> str | None:
    b = berth.upper().strip()
    for prefix, cargo in BERTH_CARGO_MAP.items():
        if b.startswith(prefix):
            return cargo
    return None


def _cargo_text_to_cat(text: str) -> str | None:
    t = text.upper().strip()
    for keyword, cat in CARGO_TEXT_MAP.items():
        if keyword in t:
            return cat
    return None


def _is_discard_cargo(cargo_text: str) -> bool:
    t = cargo_text.lower()
    return any(kw in t for kw in _DISCARD_CARGO)


def _parse_table(table: list[list], scraper: BaseScraper) -> list[dict]:
    if not table or len(table) < 2:
        return []

    # Find the header row
    header_row = None
    data_start = 0
    for i, row in enumerate(table):
        row_text = " ".join(str(c or "") for c in row).lower()
        if "berth" in row_text and "vessel" in row_text:
            header_row = [str(c or "").lower().strip() for c in row]
            data_start = i + 1
            break

    if header_row is None:
        return []

    col_berth   = _find_col(header_row, ["berth"])
    col_vessel  = _find_col(header_row, ["vessel", "ship"])
    col_cargo   = _find_col(header_row, ["cargo"])
    col_arrival = _find_col(header_row, ["date of berthing", "berthing", "arrival"])

    if col_vessel is None:
        return []

    records = []
    for row in table[data_start:]:
        if not row:
            continue
        cells = [str(c or "").strip() for c in row]
        if len(cells) < 2:
            continue

        raw_vessel = _safe_get(cells, col_vessel)
        if not raw_vessel or len(raw_vessel) < 3:
            continue

        # Skip empty berth slots
        if raw_vessel.upper() in ("NO VESSEL", "NIL", "-", "—"):
            continue

        # "(SAILED)" — vessel already left, skip
        if "(SAILED)" in raw_vessel.upper():
            continue

        # "(DUE)" — vessel expected but not yet arrived; treat as ANCHORED
        is_due = "(DUE)" in raw_vessel.upper()
        vessel = re.sub(r"\s*\((?:SAILED|DUE)\)\s*", "", raw_vessel, flags=re.I).strip()

        berth      = _safe_get(cells, col_berth)
        cargo_text = _safe_get(cells, col_cargo)
        arrival    = _safe_get(cells, col_arrival)

        # Discard obvious non-energy cargo
        if _is_discard_cargo(cargo_text):
            continue

        # Determine cargo category: cargo text > berth code > keyword detect
        cargo_cat = (
            _cargo_text_to_cat(cargo_text)
            or _berth_to_cargo(berth)
            or scraper.detect_cargo(cargo_text + " " + vessel)
        )
        if not cargo_cat or cargo_cat == "OTHER":
            continue  # skip non-energy vessels

        activity = "ANCHORED" if is_due else ("BERTHED" if berth and berth not in ("-", "—") else "ANCHORED")

        records.append({
            "ship_name":          vessel,
            "berth":              berth,
            "cargo_category":     cargo_cat,
            "activity":           activity,
            "arrival_time":       arrival,
            "expected_departure": "",
            "port_name":          scraper.port_name,
            "source":             scraper.name,
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

"""
Ministry of Ports, Shipping and Waterways (MoPSW) — monthly cargo statistics PDF parser.

Parses the "Cargo handled at Major Ports" monthly PDF published by the Indian government,
extracting POL & Crude Products tonnage from Table-2 (commodity-wise aggregate).

Usage:
    python scrapers/mopsw_scraper.py "Cargo handled at Major Ports April 2026.pdf"
    python scrapers/mopsw_scraper.py /path/to/cargo_report.pdf

Output: JSON to stdout with pol_crude_tonnes, weekly_avg_mt, report_month, etc.

PDF structure (verified against April 2026 edition):
  Table-2 (last 1-2 pages): commodity-wise cargo across all 12 major ports
  Row of interest: "POL and Crude Products" — contains monthly totals in Tonnes
  Column order in cleaned (non-None) cells:
    [0] Commodity name
    [1] April N-1 (previous year, provisional)
    [2] April N   (current year, advanced estimate)  ← target
    [3] % share current year
    [4] % change vs prev year
    [5] Apr-Mar N-1 cumulative
    [6] Apr-Mar N cumulative
    [7] % share cumulative
    [8] % change cumulative
"""
import json
import re
import sys

import pdfplumber

# Approximate weeks per calendar month
_WEEKS_PER_MONTH = 4.3

# Patterns for the commodity row we want
_POL_PATTERNS = ("POL AND CRUDE", "POL AND PRODUCT", "POL AND CRUDE PRODUCT", "POL PRODUCTS", "POL&CRUDE")

# Regex to find "DURING MONTH YEAR" or "DURING MONTHNAME YEAR" on the title page
_MONTH_RE = re.compile(r"DURING\s+([A-Z]+(?:\s+[A-Z]+)?)\s+(\d{4})", re.IGNORECASE)


def parse_mopsw_pdf(pdf_path: str) -> dict:
    """
    Parse a MoPSW monthly cargo PDF and extract POL & Crude Products tonnage.

    Returns a dict with:
        report_month         (str)  e.g. "April 2026"
        pol_crude_tonnes     (int)  monthly total in metric tonnes
        weekly_avg_mt        (int)  pol_crude_tonnes / 4.3
        pol_crude_share_pct  (float) share of total cargo
        total_cargo_tonnes   (int)  total cargo across all commodities
        notes                (str)
    """
    with pdfplumber.open(pdf_path) as pdf:
        report_month = _detect_month(pdf)
        pol_tonnes, pol_share, total_tonnes = _extract_commodity_table(pdf)

    weekly_avg = int(pol_tonnes / _WEEKS_PER_MONTH) if pol_tonnes else 0

    return {
        "report_month": report_month,
        "pol_crude_tonnes": pol_tonnes,
        "weekly_avg_mt": weekly_avg,
        "pol_crude_share_pct": pol_share,
        "total_cargo_tonnes": total_tonnes,
        "notes": (
            "POL & Crude Products aggregate across all 12 major Indian ports. "
            "Per-port crude breakdown is not available in this PDF. "
            f"Weekly average = {pol_tonnes:,} MT ÷ {_WEEKS_PER_MONTH} weeks."
        ),
    }


def _detect_month(pdf: pdfplumber.PDF) -> str:
    """Extract report month/year from page 1 text."""
    try:
        text = pdf.pages[0].extract_text() or ""
        m = _MONTH_RE.search(text.upper())
        if m:
            month_str = m.group(1).strip().title()
            year = m.group(2)
            return f"{month_str} {year}"
    except Exception:
        pass
    return "Unknown"


def _extract_commodity_table(pdf: pdfplumber.PDF) -> tuple[int, float, int]:
    """
    Scan all pages for Table-2 (commodity-wise) and extract:
      - POL & Crude Products current-month tonnes
      - POL share %
      - Total cargo current-month tonnes

    Returns (pol_tonnes, pol_share_pct, total_tonnes).
    """
    pol_tonnes = 0
    pol_share = 0.0
    total_tonnes = 0

    for page in pdf.pages:
        text = (page.extract_text() or "").upper()
        # Look for pages that contain commodity breakdown keywords
        if not any(kw in text for kw in ("TABLE-2", "COMMODITY", "POL AND CRUDE", "POL &")):
            continue

        tables = page.extract_tables()
        for table in tables:
            for row in table:
                if not row:
                    continue
                # Clean the row: strip None cells and whitespace
                cleaned = [str(c).strip() for c in row if c is not None and str(c).strip()]
                if len(cleaned) < 3:
                    continue

                first = cleaned[0].upper()

                # POL / Crude row
                if any(pat in first for pat in _POL_PATTERNS) or (
                    first.startswith("POL") and "CRUDE" in first
                ):
                    pol_tonnes = _parse_int(cleaned[2]) if len(cleaned) > 2 else 0
                    pol_share = _parse_float(cleaned[3]) if len(cleaned) > 3 else 0.0

                # Total row
                if first in ("TOTAL", "GRAND TOTAL", "ALL COMMODITIES"):
                    total_tonnes = _parse_int(cleaned[2]) if len(cleaned) > 2 else 0

        # If we found data, stop scanning pages
        if pol_tonnes > 0:
            break

    return pol_tonnes, pol_share, total_tonnes


def _parse_int(val: str) -> int:
    try:
        return int(str(val).replace(",", "").replace(" ", "").strip())
    except (ValueError, TypeError):
        return 0


def _parse_float(val: str) -> float:
    try:
        return float(str(val).replace(",", "").strip())
    except (ValueError, TypeError):
        return 0.0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scrapers/mopsw_scraper.py <pdf_path>", file=sys.stderr)
        sys.exit(1)

    result = parse_mopsw_pdf(sys.argv[1])
    print(json.dumps(result, indent=2))

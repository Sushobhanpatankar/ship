"""
Crude Weekly Average Pipeline
==============================
Determines the average amount of crude oil landed on Indian shores per week.

Two data paths:
  official — parses the MoPSW "Cargo handled at Major Ports" monthly PDF
             (government aggregate: POL & Crude Products ÷ 4.3 weeks)
  live     — runs Paradip and Deendayal scrapers, accumulates vessel-level
             quantity_mt into a 7-day rolling log (docs/crude_history.json)
  both     — runs both paths and picks the best available figure

Output: docs/crude_weekly_avg.json
        Optionally printed to stdout with -p flag.

Usage:
    python crude_weekly_pipeline.py
    python crude_weekly_pipeline.py --mode official -p
    python crude_weekly_pipeline.py --mode live -p
    python crude_weekly_pipeline.py --mode both --pdf "Cargo handled at Major Ports April 2026.pdf"
"""
import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── Silence noisy loggers unless DEBUG is set ───────────────────────────────
logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("crude_pipeline")

# ── Paths ────────────────────────────────────────────────────────────────────
_ROOT = Path(__file__).parent
_DOCS = _ROOT / "docs"
_DEFAULT_PDF = _ROOT / "Cargo handled at Major Ports April 2026.pdf"
_HISTORY_FILE = _DOCS / "crude_history.json"
_OUTPUT_FILE = _DOCS / "crude_weekly_avg.json"

# Minimum unique crude vessels for live data to be considered non-sparse
_SPARSE_THRESHOLD = 3

# ── DB no-op patch (scrapers call db.log_scraper_run; we don't need DB here) ──
import database.db as _db  # noqa: E402

async def _noop(*args, **kwargs):
    pass

_db.log_scraper_run = _noop


# ─────────────────────────────────────────────────────────────────────────────
# Official path: MoPSW PDF
# ─────────────────────────────────────────────────────────────────────────────

def _run_official(pdf_path: Path) -> dict:
    from scrapers.mopsw_scraper import parse_mopsw_pdf

    if not pdf_path.exists():
        return {
            "source": "official",
            "error": f"PDF not found: {pdf_path}",
            "weekly_avg_mt": None,
        }

    try:
        data = parse_mopsw_pdf(str(pdf_path))
        return {
            "source": "official",
            "report_month": data["report_month"],
            "pol_crude_tonnes_monthly": data["pol_crude_tonnes"],
            "weekly_avg_mt": data["weekly_avg_mt"],
            "pol_crude_share_pct": data["pol_crude_share_pct"],
            "total_cargo_tonnes": data["total_cargo_tonnes"],
            "date_range": f"{data['report_month']} ÷ 4.3 weeks",
            "per_port": {},
            "notes": data["notes"],
        }
    except Exception as exc:
        log.warning("MoPSW PDF parse failed: %s", exc)
        return {
            "source": "official",
            "error": str(exc),
            "weekly_avg_mt": None,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Live path: port scrapers
# ─────────────────────────────────────────────────────────────────────────────

async def _scrape_crude_vessels() -> list[dict]:
    """Run Paradip and Deendayal scrapers, return crude vessels with quantity_mt."""
    from scrapers.paradip_expected_scraper import ParadipExpectedScraper
    from scrapers.deendayal_scraper import DeendayalScraper

    results = await asyncio.gather(
        ParadipExpectedScraper().run(),
        DeendayalScraper().run(),
        return_exceptions=True,
    )

    vessels = []
    for scraper_result in results:
        if isinstance(scraper_result, Exception):
            log.warning("Scraper error: %s", scraper_result)
            continue
        for rec in scraper_result:
            if rec.get("cargo_category") == "CRUDE" and rec.get("quantity_mt", 0) > 0:
                vessels.append({
                    "ship_name": rec["ship_name"],
                    "port": rec.get("port_name", "Unknown"),
                    "quantity_mt": rec["quantity_mt"],
                    "activity": rec.get("activity", ""),
                })
    return vessels


def _load_history() -> list[dict]:
    if _HISTORY_FILE.exists():
        try:
            return json.loads(_HISTORY_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def _save_history(history: list[dict]) -> None:
    _DOCS.mkdir(parents=True, exist_ok=True)
    _HISTORY_FILE.write_text(json.dumps(history, indent=2), encoding="utf-8")


def _append_snapshot(history: list[dict], vessels: list[dict]) -> list[dict]:
    """Append a new snapshot and prune entries older than 14 days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=14)
    history = [
        e for e in history
        if datetime.fromisoformat(e["ts_utc"].replace("Z", "+00:00")) >= cutoff
    ]
    history.append({
        "ts_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "vessels": vessels,
    })
    return history


def _compute_7day_window(history: list[dict]) -> dict:
    """
    Deduplicate vessels within the last 7 days (keep latest per ship_name+port)
    then sum their quantity_mt.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    window_start = cutoff.strftime("%Y-%m-%d")
    window_end = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Collect all vessel records in window, newest first
    latest: dict[tuple, dict] = {}
    for entry in sorted(history, key=lambda e: e["ts_utc"]):
        ts = datetime.fromisoformat(entry["ts_utc"].replace("Z", "+00:00"))
        if ts < cutoff:
            continue
        for v in entry.get("vessels", []):
            key = (v["ship_name"].upper(), v["port"])
            latest[key] = v  # overwrite → keeps most recent snapshot

    unique_vessels = list(latest.values())
    total_mt = sum(v["quantity_mt"] for v in unique_vessels)

    per_port: dict[str, int] = {}
    for v in unique_vessels:
        per_port[v["port"]] = per_port.get(v["port"], 0) + v["quantity_mt"]

    return {
        "source": "live",
        "date_range": f"{window_start} to {window_end}",
        "weekly_avg_mt": total_mt,
        "vessel_count": len(unique_vessels),
        "per_port": per_port,
        "data_sparse": len(unique_vessels) < _SPARSE_THRESHOLD,
    }


def _run_live() -> dict:
    vessels = asyncio.run(_scrape_crude_vessels())
    history = _load_history()
    history = _append_snapshot(history, vessels)
    _save_history(history)
    return _compute_7day_window(history)


# ─────────────────────────────────────────────────────────────────────────────
# Recommendation logic
# ─────────────────────────────────────────────────────────────────────────────

def _pick_recommended(official: dict | None, live: dict | None) -> tuple[int | None, str]:
    """Return (recommended_weekly_avg_mt, note)."""
    has_official = official and official.get("weekly_avg_mt") and not official.get("error")
    has_live = live and live.get("weekly_avg_mt") and not live.get("data_sparse")

    if has_live:
        return live["weekly_avg_mt"], "Using live scraper data (7-day vessel-level tonnage)."
    if has_official:
        return official["weekly_avg_mt"], (
            f"Using official MoPSW data ({official.get('report_month', '')}); "
            "live data insufficient (<3 crude vessels with quantity)."
        )
    if live and live.get("weekly_avg_mt"):
        return live["weekly_avg_mt"], "Live data sparse but used as best available estimate."
    return None, "Insufficient data from all sources."


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compute average crude oil landed on Indian shores per week."
    )
    parser.add_argument(
        "--mode",
        choices=["official", "live", "both"],
        default="both",
        help="Data source: official (MoPSW PDF), live (port scrapers), or both (default).",
    )
    parser.add_argument(
        "-p", "--print",
        dest="print_output",
        action="store_true",
        help="Print JSON result to stdout (pipe-friendly).",
    )
    parser.add_argument(
        "--pdf",
        default=str(_DEFAULT_PDF),
        help="Path to the MoPSW monthly cargo PDF.",
    )
    args = parser.parse_args()

    official_result: dict | None = None
    live_result: dict | None = None

    if args.mode in ("official", "both"):
        log.info("Running official (MoPSW PDF) path…")
        official_result = _run_official(Path(args.pdf))

    if args.mode in ("live", "both"):
        log.info("Running live (scraper) path…")
        live_result = _run_live()

    recommended, note = _pick_recommended(official_result, live_result)

    output = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "recommended_weekly_avg_mt": recommended,
        "recommendation_note": note,
    }
    if official_result:
        output["official"] = official_result
    if live_result:
        output["live"] = live_result

    # Write output file
    _DOCS.mkdir(parents=True, exist_ok=True)
    _OUTPUT_FILE.write_text(json.dumps(output, indent=2), encoding="utf-8")
    log.info("Wrote %s", _OUTPUT_FILE)

    if args.print_output:
        print(json.dumps(output, indent=2))
    else:
        print(f"Done. Output written to {_OUTPUT_FILE}")
        if recommended:
            print(f"Recommended weekly crude avg: {recommended:,} MT")
        print(f"Note: {note}")


if __name__ == "__main__":
    main()

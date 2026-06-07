"""
Vessel Watch Agent — Gemini-powered monitoring of crude/LNG/CNG vessels
=======================================================================
Loads:
  - docs/ais_snapshot.json     (inbound/outbound AIS from Indian waters)
  - docs/hormuz_snapshot.json  (Persian Gulf / Hormuz region, from hormuz_tracker.py)
  - live port scraper results

Then asks Gemini to analyse origin routes, sea zones, Hormuz pipeline, and
notable patterns for energy tankers heading to India.

Run twice daily by GitHub Actions (06:00 and 18:00 UTC).

Usage:
    python vessel_watch_agent.py              # banner output
    python vessel_watch_agent.py -p           # stdout only (pipe-friendly)
    python vessel_watch_agent.py --no-gemini  # skip Gemini, just refresh JSON

Outputs:
    docs/vessel_watch.json          — structured snapshot
    docs/vessel_watch_analysis.txt  — Gemini plain-text briefing

Environment:
    GEMINI_API_KEY   — Google AI Studio key (required unless --no-gemini)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── stub db so scrapers run without a live database ──────────────────────────
_db_stub = types.ModuleType("database.db")
sys.modules.setdefault("database.db", _db_stub)
sys.modules.setdefault("database", types.ModuleType("database"))

import database.db as _db  # noqa: E402  (must come after stub)

async def _noop(*a, **kw):
    pass

_db.log_scraper_run = _noop

from scrapers.jnpt_scraper import JNPTScraper                          # noqa: E402
from scrapers.kochi_scraper import KochiScraper                        # noqa: E402
from scrapers.mundra_scraper import MundraScraper                      # noqa: E402
from scrapers.paradip_scraper import ParadipScraper                    # noqa: E402
from scrapers.vizag_scraper import VizagScraper                        # noqa: E402

AIS_SNAPSHOT      = Path("docs/ais_snapshot.json")
HORMUZ_SNAPSHOT   = Path("docs/hormuz_snapshot.json")
WATCH_JSON        = Path("docs/vessel_watch.json")
WATCH_ANALYSIS    = Path("docs/vessel_watch_analysis.txt")
AIS_MAX_AGE_HOURS     = 6    # still useful for watch even if slightly stale
HORMUZ_MAX_AGE_HOURS  = 14   # hormuz runs twice daily

ENERGY_CARGO      = {"CRUDE", "LNG", "CNG"}

# ── Sea-zone classifier ───────────────────────────────────────────────────────

def _sea_zone(lat: float, lon: float) -> str:
    """Return a human-readable sea zone from lat/lon."""
    if 23 <= lat <= 30 and 48 <= lon <= 57:
        return "Persian Gulf"
    if 11 <= lat <= 30 and 32 <= lon <= 48:
        return "Red Sea / Gulf of Aden"
    if 0 <= lat <= 12 and 43 <= lon <= 58:
        return "Gulf of Aden / Arabian Sea entrance"
    if 5 <= lat <= 25 and 55 <= lon <= 78:
        return "Arabian Sea"
    if -5 <= lat <= 10 and 50 <= lon <= 75:
        return "Indian Ocean (west)"
    if -20 <= lat <= 5 and 30 <= lon <= 80:
        return "Indian Ocean (south)"
    if 5 <= lat <= 22 and 78 <= lon <= 100:
        return "Bay of Bengal"
    if 1 <= lat <= 6 and 99 <= lon <= 105:
        return "Strait of Malacca"
    if -5 <= lat <= 25 and 100 <= lon <= 120:
        return "South China Sea"
    if lat <= -20 or lon <= 20:
        return "Atlantic / South Africa"
    return "Open Ocean"


# ── AIS snapshot loader ───────────────────────────────────────────────────────

def load_ais_snapshot() -> tuple[list[dict], list[dict], str, bool]:
    """
    Returns (inbound_vessels, outbound_vessels, fetched_at, is_stale).
    Filters to CRUDE/LNG/CNG only and attaches sea_zone to each vessel.
    """
    if not AIS_SNAPSHOT.exists():
        return [], [], "", True

    data = json.loads(AIS_SNAPSHOT.read_text(encoding="utf-8"))
    fetched_at = data.get("fetched_at", "")
    stale = True
    if fetched_at:
        ts = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
        age = datetime.now(timezone.utc) - ts
        stale = age > timedelta(hours=AIS_MAX_AGE_HOURS)

    def _enrich(vessels: list[dict]) -> list[dict]:
        out = []
        for v in vessels:
            if v.get("cargo_category") not in ENERGY_CARGO:
                continue
            lat = v.get("lat", 0)
            lon = v.get("lon", 0)
            v = dict(v)
            v["sea_zone"] = _sea_zone(lat, lon)
            out.append(v)
        return out

    inbound  = _enrich(data.get("inbound", []))
    outbound = _enrich(data.get("outbound", []))
    return inbound, outbound, fetched_at, stale


# ── Hormuz snapshot loader ───────────────────────────────────────────────────

def load_hormuz_snapshot() -> tuple[dict, bool]:
    """
    Load docs/hormuz_snapshot.json (written by hormuz_tracker.py).
    Returns (data, is_stale). Empty dict if file missing or too old.
    """
    if not HORMUZ_SNAPSHOT.exists():
        return {}, True
    data = json.loads(HORMUZ_SNAPSHOT.read_text(encoding="utf-8"))
    gen_at = data.get("generated_at", "")
    stale = True
    if gen_at:
        ts = datetime.fromisoformat(gen_at.replace("Z", "+00:00"))
        stale = datetime.now(timezone.utc) - ts > timedelta(hours=HORMUZ_MAX_AGE_HOURS)
    return data, stale


# ── Port scraper runner ───────────────────────────────────────────────────────

async def _run_port_scrapers() -> list[dict]:
    scrapers = [
        ("JNPT",    JNPTScraper()),
        ("Kochi",   KochiScraper()),
        ("Paradip", ParadipScraper()),
        ("Mundra",  MundraScraper()),
        ("Vizag",   VizagScraper()),
    ]
    results = await asyncio.gather(
        *[s.run() for _, s in scrapers],
        return_exceptions=True,
    )
    vessels = []
    for (name, _), res in zip(scrapers, results):
        if isinstance(res, Exception):
            print(f"  [{name}] scraper error: {res}", file=sys.stderr)
        else:
            vessels.extend(res)
    return [v for v in vessels if v.get("cargo_category") in ENERGY_CARGO]


# ── Snapshot builder ──────────────────────────────────────────────────────────

def _build_snapshot(
    inbound: list[dict],
    outbound: list[dict],
    at_port: list[dict],
    ais_fetched_at: str,
    ais_stale: bool,
    hormuz: dict | None = None,
    hormuz_stale: bool = True,
) -> dict:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Count by cargo type
    def _counts(vessels: list[dict]) -> dict:
        c: dict[str, int] = {"CRUDE": 0, "LNG": 0, "CNG": 0}
        for v in vessels:
            cat = v.get("cargo_category", "")
            if cat in c:
                c[cat] += 1
        return c

    # Sea-zone distribution from inbound
    zone_counts: dict[str, int] = {}
    for v in inbound:
        z = v.get("sea_zone", "Unknown")
        zone_counts[z] = zone_counts.get(z, 0) + 1

    hormuz_summary = {}
    if hormuz and not hormuz_stale:
        t = hormuz.get("totals", {})
        hormuz_summary = {
            "india_bound":   t.get("india_bound", 0),
            "transiting":    t.get("transiting", 0),
            "in_gulf":       t.get("in_gulf", 0),
            "india_bound_counts": t.get("india_bound_counts", {}),
            "destination_ports": hormuz.get("destination_ports", {}),
            "flag_origins":  hormuz.get("flag_origins", {}),
            "generated_at":  hormuz.get("generated_at", ""),
            "stale":         False,
        }
    elif hormuz_stale:
        hormuz_summary = {"stale": True}

    return {
        "generated_at":     now,
        "ais_snapshot_at":  ais_fetched_at,
        "ais_stale":        ais_stale,
        "totals": {
            "at_port":  len(at_port),
            "inbound":  len(inbound),
            "outbound": len(outbound),
            "at_port_counts":  _counts(at_port),
            "inbound_counts":  _counts(inbound),
            "outbound_counts": _counts(outbound),
        },
        "sea_zones":    zone_counts,
        "hormuz":       hormuz_summary,
        "inbound":      inbound,
        "outbound":     outbound,
        "at_port":      at_port,
    }


# ── Gemini prompt builder ─────────────────────────────────────────────────────

def _build_prompt(snap: dict) -> str:
    lines = [
        "You are a shipping analyst specialising in Indian energy imports.",
        "Write a concise plain-text briefing (4-5 paragraphs, no markdown) based on the",
        "real-time vessel data below. Focus on origin routes, sea zones, cargo breakdown,",
        "and any noteworthy patterns or risks.",
        "",
        f"Data snapshot: {snap['generated_at']}",
        f"AIS data age: {'STALE (>6h)' if snap['ais_stale'] else 'FRESH'} (captured {snap['ais_snapshot_at']})",
        "",
    ]

    # Totals
    t = snap["totals"]
    lines += [
        "VESSEL SUMMARY:",
        f"  At Indian ports:   {t['at_port']}  (Crude {t['at_port_counts']['CRUDE']},"
        f" LNG {t['at_port_counts']['LNG']}, CNG {t['at_port_counts']['CNG']})",
        f"  Inbound to India:  {t['inbound']}  (Crude {t['inbound_counts']['CRUDE']},"
        f" LNG {t['inbound_counts']['LNG']}, CNG {t['inbound_counts']['CNG']})",
        f"  Outbound:          {t['outbound']}  (Crude {t['outbound_counts']['CRUDE']},"
        f" LNG {t['outbound_counts']['LNG']}, CNG {t['outbound_counts']['CNG']})",
        "",
    ]

    # Sea zones
    if snap["sea_zones"]:
        lines.append("SEA ZONES (inbound vessels):")
        for zone, count in sorted(snap["sea_zones"].items(), key=lambda x: -x[1]):
            lines.append(f"  {zone}: {count} vessel(s)")
        lines.append("")

    # Inbound detail
    if snap["inbound"]:
        lines.append("INBOUND VESSELS (en-route to India):")
        for v in snap["inbound"][:15]:  # cap at 15
            name  = v.get("ship_name", "Unknown")
            cargo = v.get("cargo_category", "?")
            port  = v.get("nearest_port", "?")
            dist  = v.get("distance_nm", "?")
            spd   = v.get("speed", 0)
            dest  = v.get("destination", "") or ""
            zone  = v.get("sea_zone", "")
            speed_str = f"{float(spd):.1f} kn" if spd else "—"
            lines.append(
                f"  {name} | {cargo} | {dist} nm from {port}"
                f" | {speed_str} | dest: {dest or '—'} | zone: {zone}"
            )
        lines.append("")

    # At-port detail
    if snap["at_port"]:
        lines.append("VESSELS AT INDIAN PORTS:")
        for v in snap["at_port"][:12]:
            name  = v.get("ship_name", "Unknown")
            cargo = v.get("cargo_category", "?")
            port  = v.get("port_name", v.get("nearest_port", "?"))
            act   = v.get("activity", "BERTHED")
            lines.append(f"  {name} | {cargo} | {port} | {act}")
        lines.append("")

    # Outbound
    if snap["outbound"]:
        lines.append("OUTBOUND VESSELS:")
        for v in snap["outbound"][:8]:
            name  = v.get("ship_name", "Unknown")
            cargo = v.get("cargo_category", "?")
            port  = v.get("nearest_port", "?")
            dest  = v.get("destination", "") or "—"
            spd   = float(v.get("speed", 0) or 0)
            lines.append(f"  {name} | {cargo} | from {port} | dest: {dest} | {spd:.1f} kn")
        lines.append("")

    # Hormuz pipeline section
    hormuz = snap.get("hormuz", {})
    if hormuz and not hormuz.get("stale"):
        lines += [
            f"HORMUZ PIPELINE (as of {hormuz.get('generated_at','')[:16].replace('T',' ')} UTC):",
            f"  Vessels with confirmed India destination in/near Gulf: {hormuz.get('india_bound', 0)}",
            f"  Transiting Hormuz/Gulf of Oman (no India dest yet):    {hormuz.get('transiting', 0)}",
            f"  Remaining in Persian Gulf:                              {hormuz.get('in_gulf', 0)}",
        ]
        ibc = hormuz.get("india_bound_counts", {})
        if ibc:
            lines.append(
                f"  India-bound cargo mix: Crude {ibc.get('CRUDE',0)}  "
                f"LNG {ibc.get('LNG',0)}  CNG {ibc.get('CNG',0)}  "
                f"Petroleum {ibc.get('PETROLEUM',0)}"
            )
        dest_ports = hormuz.get("destination_ports", {})
        if dest_ports:
            lines.append("  India port destinations (from AIS dest field):")
            for port, cnt in sorted(dest_ports.items(), key=lambda x: -x[1]):
                lines.append(f"    {port}: {cnt} vessel(s)")
        flag_origins = hormuz.get("flag_origins", {})
        if flag_origins:
            top5 = sorted(flag_origins.items(), key=lambda x: -x[1])[:6]
            lines.append("  Vessel flags (top origins): "
                         + ", ".join(f"{c}×{f}" for f, c in top5))
        lines.append("")
    elif hormuz.get("stale"):
        lines += ["HORMUZ PIPELINE: Data stale — run hormuz_tracker.py to refresh.", ""]

    lines += [
        "Please analyse:",
        "1. Hormuz pipeline: how many vessels are in the gulf pipeline heading to India,",
        "   what cargo types dominate, and what their flag states suggest about supply origins",
        "   (Middle East crude, Qatar LNG, Russian crude via Gulf, etc.)",
        "2. Which Indian ports are the primary destinations and what this implies",
        "   (Paradip/Mundra → crude; Kochi/Hazira/Dahej → LNG; Hazira → CNG)",
        "3. Sea-lane distribution for currently inbound vessels (Arabian Sea vs Bay of Bengal)",
        "4. LNG vs crude vs CNG balance — any shift from typical Indian import patterns?",
        "5. Any operational caveats (stale AIS, sparse data, coverage gaps)",
        "6. Short outlook: estimated arrivals in next 24-48h based on distances and speeds",
    ]
    return "\n".join(lines)


# ── Gemini call ───────────────────────────────────────────────────────────────

def _call_gemini(prompt: str, api_key: str) -> str:
    try:
        from google import genai
    except ImportError:
        print("Error: google-genai not installed. Run: pip install google-genai>=1.0.0",
              file=sys.stderr)
        sys.exit(1)

    client = genai.Client(api_key=api_key)
    import time
    for model in ("gemini-2.5-flash", "gemini-2.0-flash", "gemini-2.5-flash-lite"):
        for attempt in range(2):
            try:
                resp = client.models.generate_content(model=model, contents=prompt)
                return resp.text
            except Exception as exc:
                msg = str(exc)
                if "503" in msg or "UNAVAILABLE" in msg:
                    wait = 8 * (attempt + 1)
                    print(f"  [{model}] 503 overloaded, retrying in {wait}s…", file=sys.stderr)
                    time.sleep(wait)
                    continue
                print(f"Error: Gemini call failed ({model}): {exc}", file=sys.stderr)
                break  # non-503 error, try next model
    print("Error: all Gemini models failed.", file=sys.stderr)
    sys.exit(1)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Gemini vessel watch agent for India crude/LNG/CNG imports."
    )
    parser.add_argument("-p", "--print", dest="print_only", action="store_true",
                        help="Stdout only (pipe-friendly, no banner).")
    parser.add_argument("--no-gemini", action="store_true",
                        help="Skip Gemini call; just refresh vessel_watch.json.")
    args = parser.parse_args()

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key and not args.no_gemini:
        print("Error: GEMINI_API_KEY not set.", file=sys.stderr)
        sys.exit(1)

    # 1. Load AIS snapshot (Indian waters)
    inbound, outbound, ais_at, ais_stale = load_ais_snapshot()
    print(f"AIS (India): {len(inbound)} inbound, {len(outbound)} outbound"
          f"{' (STALE)' if ais_stale else ''}", file=sys.stderr)

    # 2. Load Hormuz snapshot
    hormuz, hormuz_stale = load_hormuz_snapshot()
    if hormuz:
        t = hormuz.get("totals", {})
        print(f"Hormuz: {t.get('india_bound',0)} India-bound, "
              f"{t.get('transiting',0)} transiting"
              f"{' (STALE)' if hormuz_stale else ''}", file=sys.stderr)
    else:
        print("Hormuz: no snapshot found (run hormuz_tracker.py)", file=sys.stderr)

    # 3. Run port scrapers
    at_port = asyncio.run(_run_port_scrapers())
    print(f"At-port: {len(at_port)} energy vessels", file=sys.stderr)

    # 4. Build snapshot JSON
    snap = _build_snapshot(inbound, outbound, at_port, ais_at, ais_stale,
                           hormuz, hormuz_stale)
    WATCH_JSON.parent.mkdir(parents=True, exist_ok=True)
    WATCH_JSON.write_text(json.dumps(snap, indent=2), encoding="utf-8")
    print(f"Written {WATCH_JSON}", file=sys.stderr)

    if args.no_gemini:
        return

    # 4. Call Gemini
    prompt   = _build_prompt(snap)
    analysis = _call_gemini(prompt, api_key)

    # 5. Save analysis
    WATCH_ANALYSIS.write_text(analysis, encoding="utf-8")

    # 6. Output
    if args.print_only:
        print(analysis)
    else:
        bar = "=" * 70
        t   = snap["totals"]
        print(bar)
        print(f"  VESSEL WATCH — {snap['generated_at']}")
        print(f"  At port: {t['at_port']}  Inbound: {t['inbound']}  "
              f"Outbound: {t['outbound']}")
        print(bar)
        print()
        print(analysis)
        print()
        print(bar)


if __name__ == "__main__":
    main()

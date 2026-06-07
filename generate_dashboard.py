"""
Static ship movement dashboard generator.

Runs the four port scrapers (JNPT, Paradip, Mundra, Vizag) without needing
an AIS key, appends a snapshot to docs/ships_data.json, then writes a
self-contained docs/index.html.

Run by GitHub Actions every 30 minutes, or locally any time.
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone

# ── Patch db.log_scraper_run to a no-op so scrapers run without a real DB ──
import database.db as _db

async def _noop(*args, **kwargs):
    pass

_db.log_scraper_run = _noop

from scrapers.jnpt_scraper import JNPTScraper                          # noqa: E402
from scrapers.kochi_scraper import KochiScraper                         # noqa: E402
from scrapers.mundra_scraper import MundraScraper                       # noqa: E402
from scrapers.mundra_expected_scraper import MundraExpectedScraper      # noqa: E402
from scrapers.paradip_scraper import ParadipScraper                     # noqa: E402
from scrapers.paradip_expected_scraper import ParadipExpectedScraper    # noqa: E402
from scrapers.vizag_scraper import VizagScraper                         # noqa: E402

HISTORY_FILE    = "docs/ships_data.json"
AIS_SNAPSHOT    = "docs/ais_snapshot.json"
CRUDE_WEEKLY    = "docs/crude_weekly_avg.json"
CRUDE_ANALYSIS  = "docs/crude_analysis.txt"
VESSEL_WATCH          = "docs/vessel_watch.json"
VESSEL_WATCH_ANALYSIS = "docs/vessel_watch_analysis.txt"
HORMUZ_SNAPSHOT       = "docs/hormuz_snapshot.json"
MAX_HISTORY     = 336   # 7 days × 48 half-hours
SNAPSHOT_MAX_AGE_HOURS = 3   # treat snapshot as stale if older than this (snapshots run every 2h)
CRUDE_MAX_AGE_HOURS    = 48  # show crude data up to 48h old, then flag as stale
WATCH_MAX_AGE_HOURS    = 14  # show vessel watch data up to 14h old (runs twice daily)


# ─────────────────────────────────────────────────────────────
# Scraper runner
# ─────────────────────────────────────────────────────────────

async def run_scrapers() -> list[dict]:
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
    records: list[dict] = []
    for (name, _), res in zip(scrapers, results):
        if isinstance(res, Exception):
            print(f"  [{name}] scraper error: {res}")
        else:
            print(f"  [{name}] {len(res)} vessels")
            records.extend(res)
    return records


async def run_expected_scrapers() -> list[dict]:
    scrapers = [
        ("ParadipExpected", ParadipExpectedScraper()),
        ("MundraExpected",  MundraExpectedScraper()),
    ]
    results = await asyncio.gather(
        *[s.run() for _, s in scrapers],
        return_exceptions=True,
    )
    records: list[dict] = []
    for (name, _), res in zip(scrapers, results):
        if isinstance(res, Exception):
            print(f"  [{name}] expected scraper error: {res}")
        else:
            print(f"  [{name}] {len(res)} expected vessels")
            records.extend(res)
    # Sort by ETA (soonest first); records without ETA go to the end
    records.sort(key=lambda r: r.get("eta", "9999"))
    return records


# ─────────────────────────────────────────────────────────────
# AIS snapshot (written by fetch_ais_snapshot.py every 2 hours)
# ─────────────────────────────────────────────────────────────

def load_ais_snapshot() -> dict:
    """
    Read docs/ais_snapshot.json written by fetch_ais_snapshot.py.
    Returns {} if the file is missing or older than SNAPSHOT_MAX_AGE_HOURS.
    """
    if not os.path.exists(AIS_SNAPSHOT):
        return {}
    try:
        with open(AIS_SNAPSHOT, encoding="utf-8") as f:
            data = json.load(f)
        fetched_at = data.get("fetched_at", "")
        if fetched_at:
            ts = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
            age = datetime.now(timezone.utc) - ts
            if age > timedelta(hours=SNAPSHOT_MAX_AGE_HOURS):
                print(f"  [AIS snapshot] stale ({age}), ignoring")
                return {}
        print(f"  [AIS snapshot] inbound={data.get('total_inbound',0)}"
              f" outbound={data.get('total_outbound',0)}"
              f" (from {data.get('fetched_at','')})")
        return data
    except Exception as e:
        print(f"  [AIS snapshot] read error: {e}")
        return {}


# ─────────────────────────────────────────────────────────────
# Crude weekly data loader
# ─────────────────────────────────────────────────────────────

def load_crude_weekly() -> dict:
    """Load docs/crude_weekly_avg.json if present and not too old."""
    if not os.path.exists(CRUDE_WEEKLY):
        return {}
    try:
        with open(CRUDE_WEEKLY, encoding="utf-8") as f:
            data = json.load(f)
        generated_at = data.get("generated_at", "")
        stale = False
        age_str = ""
        if generated_at:
            ts = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
            age = datetime.now(timezone.utc) - ts
            stale = age > timedelta(hours=CRUDE_MAX_AGE_HOURS)
            hours = int(age.total_seconds() // 3600)
            age_str = f"{hours}h ago" if hours < 48 else f"{age.days}d ago"
        data["_stale"] = stale
        data["_age_str"] = age_str
        print(f"  [Crude weekly] loaded (generated {age_str}{'  — stale' if stale else ''})")
        return data
    except Exception as e:
        print(f"  [Crude weekly] read error: {e}")
        return {}


def load_crude_analysis() -> str:
    """Load the Gemini analysis text from docs/crude_analysis.txt if present."""
    if not os.path.exists(CRUDE_ANALYSIS):
        return ""
    try:
        with open(CRUDE_ANALYSIS, encoding="utf-8") as f:
            text = f.read().strip()
        print(f"  [Crude analysis] loaded ({len(text)} chars)")
        return text
    except Exception as e:
        print(f"  [Crude analysis] read error: {e}")
        return ""


def load_vessel_watch() -> dict:
    """Load docs/vessel_watch.json if present and not too old."""
    if not os.path.exists(VESSEL_WATCH):
        return {}
    try:
        with open(VESSEL_WATCH, encoding="utf-8") as f:
            data = json.load(f)
        generated_at = data.get("generated_at", "")
        stale = False
        age_str = ""
        if generated_at:
            ts = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
            age = datetime.now(timezone.utc) - ts
            stale = age > timedelta(hours=WATCH_MAX_AGE_HOURS)
            hours = int(age.total_seconds() // 3600)
            age_str = f"{hours}h ago" if hours < 48 else f"{age.days}d ago"
        data["_stale"] = stale
        data["_age_str"] = age_str
        print(f"  [Vessel watch] loaded (generated {age_str}{'  — stale' if stale else ''})")
        return data
    except Exception as e:
        print(f"  [Vessel watch] read error: {e}")
        return {}


def load_vessel_watch_analysis() -> str:
    """Load the Gemini vessel watch analysis from docs/vessel_watch_analysis.txt."""
    if not os.path.exists(VESSEL_WATCH_ANALYSIS):
        return ""
    try:
        with open(VESSEL_WATCH_ANALYSIS, encoding="utf-8") as f:
            text = f.read().strip()
        print(f"  [Vessel watch analysis] loaded ({len(text)} chars)")
        return text
    except Exception as e:
        print(f"  [Vessel watch analysis] read error: {e}")
        return ""


# ─────────────────────────────────────────────────────────────
# Stats computation
# ─────────────────────────────────────────────────────────────

CARGO_ORDER = ["CRUDE", "LNG", "CNG", "PETROLEUM", "OTHER"]
ACTIVITY_RANK = {"LOADING": 0, "UNLOADING": 1, "BERTHED": 2, "ANCHORED": 3, "OTHER": 4}


def compute_stats(records: list[dict]) -> dict:
    cargo_counts: dict[str, int] = {c: 0 for c in CARGO_ORDER}
    port_counts:  dict[str, dict] = {}

    for r in records:
        cargo = r.get("cargo_category", "OTHER")
        if cargo not in cargo_counts:
            cargo = "OTHER"
        cargo_counts[cargo] += 1

        port = r.get("port_name", r.get("source", "Unknown"))
        if port not in port_counts:
            port_counts[port] = {c: 0 for c in CARGO_ORDER}
            port_counts[port]["total"] = 0
        port_counts[port][cargo] = port_counts[port].get(cargo, 0) + 1
        port_counts[port]["total"] += 1

    busiest = max(port_counts, key=lambda p: port_counts[p]["total"]) if port_counts else "—"

    return {
        "total_in_port":  len(records),
        "cargo_counts":   cargo_counts,
        "port_counts":    port_counts,
        "busiest_port":   busiest,
    }


# ─────────────────────────────────────────────────────────────
# History helpers
# ─────────────────────────────────────────────────────────────

def load_history() -> list:
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, encoding="utf-8") as f:
                return json.load(f).get("history", [])
        except Exception:
            pass
    return []


def save_history(history: list):
    os.makedirs("docs", exist_ok=True)
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump({"history": history[-MAX_HISTORY:]}, f, indent=2)


# ─────────────────────────────────────────────────────────────
# HTML builder
# ─────────────────────────────────────────────────────────────

CARGO_COLORS = {
    "CRUDE":     ("#fbbf24", "#f59e0b22", "#f59e0b44"),
    "LNG":       ("#60a5fa", "#3b82f622", "#3b82f644"),
    "CNG":       ("#34d399", "#10b98122", "#10b98144"),
    "PETROLEUM": ("#fb923c", "#f9731622", "#f9731644"),
    "OTHER":     ("#a3a3a3", "#52525222", "#52525244"),
}

ACTIVITY_COLORS = {
    "LOADING":   "#10b981",
    "UNLOADING": "#f59e0b",
    "BERTHED":   "#3b82f6",
    "ANCHORED":  "#8b5cf6",
    "OTHER":     "#6b7280",
}


def _cargo_badge(cargo: str) -> str:
    text, bg, border = CARGO_COLORS.get(cargo, CARGO_COLORS["OTHER"])
    return (f'<span style="font-size:.72rem;font-weight:600;padding:3px 8px;'
            f'border-radius:10px;background:{bg};color:{text};border:1px solid {border}">'
            f'{cargo}</span>')


def _activity_badge(activity: str) -> str:
    color = ACTIVITY_COLORS.get(activity, ACTIVITY_COLORS["OTHER"])
    return (f'<span style="font-size:.72rem;font-weight:600;padding:3px 8px;'
            f'border-radius:10px;background:{color}22;color:{color};border:1px solid {color}44">'
            f'{activity}</span>')


def _course_arrow(course: float) -> str:
    directions = ["↑", "↗", "→", "↘", "↓", "↙", "←", "↖"]
    return directions[round(course / 45) % 8]


def _vessel_rows(records: list[dict]) -> str:
    if not records:
        return '<tr><td colspan="6" style="text-align:center;color:#8892a4;padding:24px">No vessel data scraped yet — port scrapers may be rate-limited.</td></tr>'

    sorted_r = sorted(
        records,
        key=lambda r: (
            r.get("port_name", r.get("source", "z")),
            ACTIVITY_RANK.get(r.get("activity", "OTHER"), 4),
        ),
    )
    rows = ""
    for r in sorted_r:
        name     = r.get("ship_name", "Unknown")[:28]
        port     = r.get("port_name", r.get("source", "—"))
        berth    = r.get("berth", "—") or "—"
        cargo    = r.get("cargo_category", "OTHER")
        activity = r.get("activity", "OTHER")
        arrived  = r.get("arrival_time", "") or "—"
        rows += (
            f"<tr>"
            f"<td style='font-weight:500'>{name}</td>"
            f"<td>{port}</td>"
            f"<td style='font-size:.78rem;color:#8892a4'>{berth}</td>"
            f"<td>{_cargo_badge(cargo)}</td>"
            f"<td>{_activity_badge(activity)}</td>"
            f"<td style='font-size:.78rem;color:#8892a4'>{arrived}</td>"
            f"</tr>\n"
        )
    return rows


def _port_summary_rows(port_counts: dict) -> str:
    if not port_counts:
        return '<tr><td colspan="6" style="color:#8892a4">No data</td></tr>'
    rows = ""
    for port, counts in sorted(port_counts.items(), key=lambda x: -x[1]["total"]):
        rows += (
            f"<tr>"
            f"<td style='font-weight:600'>{port}</td>"
            f"<td style='text-align:right;color:#fbbf24'>{counts.get('CRUDE',0)}</td>"
            f"<td style='text-align:right;color:#60a5fa'>{counts.get('LNG',0)}</td>"
            f"<td style='text-align:right;color:#34d399'>{counts.get('CNG',0)}</td>"
            f"<td style='text-align:right;color:#fb923c'>{counts.get('PETROLEUM',0)}</td>"
            f"<td style='text-align:right;font-weight:700'>{counts.get('total',0)}</td>"
            f"</tr>\n"
        )
    return rows


def _direction_badge(direction: str) -> str:
    if direction == "INBOUND":
        return ('<span style="font-size:.72rem;font-weight:600;padding:3px 8px;'
                'border-radius:10px;background:#10b98122;color:#10b981;border:1px solid #10b98144">'
                '&#8595; INBOUND</span>')
    return ('<span style="font-size:.72rem;font-weight:600;padding:3px 8px;'
            'border-radius:10px;background:#f59e0b22;color:#f59e0b;border:1px solid #f59e0b44">'
            '&#8593; OUTBOUND</span>')


def _expected_arrival_rows(records: list[dict]) -> str:
    # Only show INBOUND expected vessels (arriving to discharge cargo)
    inbound = [r for r in records if r.get("direction", "INBOUND") == "INBOUND"]
    if not inbound:
        return ('<tr><td colspan="6" style="text-align:center;color:#8892a4;padding:24px">'
                'No expected arrivals found — port sites may be updating.</td></tr>')
    rows = ""
    for r in inbound:
        name  = (r.get("ship_name") or "Unknown")[:28]
        cargo = r.get("cargo_category", "OTHER")
        port  = r.get("port_name", "—")
        eta   = r.get("eta", "—")
        qty   = r.get("quantity_mt", 0)
        qty_str = f"{qty:,}" if qty else "—"
        rows += (
            f"<tr>"
            f"<td style='font-weight:500'>{name}</td>"
            f"<td>{_cargo_badge(cargo)}</td>"
            f"<td>{port}</td>"
            f"<td style='font-variant-numeric:tabular-nums;color:#10b981'>{eta}</td>"
            f"<td style='text-align:right;font-size:.78rem;color:#8892a4'>{qty_str}</td>"
            f"<td style='font-size:.72rem;color:#8892a4'>{r.get('source','')}</td>"
            f"</tr>\n"
        )
    return rows


def _expected_outbound_rows(records: list[dict]) -> str:
    # Ships arriving to LOAD (i.e. will leave India with cargo)
    outbound = [r for r in records if r.get("direction") == "OUTBOUND"]
    if not outbound:
        return ('<tr><td colspan="5" style="text-align:center;color:#8892a4;padding:24px">'
                'No outbound loadings scheduled.</td></tr>')
    rows = ""
    for r in outbound:
        name  = (r.get("ship_name") or "Unknown")[:28]
        cargo = r.get("cargo_category", "OTHER")
        port  = r.get("port_name", "—")
        eta   = r.get("eta", "—")
        qty   = r.get("quantity_mt", 0)
        qty_str = f"{qty:,}" if qty else "—"
        rows += (
            f"<tr>"
            f"<td style='font-weight:500'>{name}</td>"
            f"<td>{_cargo_badge(cargo)}</td>"
            f"<td>{port}</td>"
            f"<td style='font-variant-numeric:tabular-nums;color:#f59e0b'>{eta}</td>"
            f"<td style='text-align:right;font-size:.78rem;color:#8892a4'>{qty_str}&nbsp;MT</td>"
            f"</tr>\n"
        )
    return rows


def _inbound_rows_from_expected(expected: list[dict]) -> str:
    """Render expected-arrival records in the AIS inbound table format (fallback)."""
    inbound = [r for r in expected if r.get("direction", "INBOUND") == "INBOUND"]
    if not inbound:
        return ('<tr><td colspan="7" style="text-align:center;color:#8892a4;padding:24px">'
                'No inbound vessel data available — AIS offline, port schedules also empty.</td></tr>')
    rows = ""
    for r in inbound:
        name  = (r.get("ship_name") or "Unknown")[:28]
        cargo = r.get("cargo_category", "OTHER")
        port  = r.get("port_name", "—")
        eta   = r.get("eta", "—")
        qty   = r.get("quantity_mt", 0)
        qty_str = f"{qty:,}&nbsp;MT" if qty else "—"
        rows += (
            f"<tr>"
            f"<td style='font-weight:500'>{name}</td>"
            f"<td>{_cargo_badge(cargo)}</td>"
            f"<td>{port}</td>"
            f"<td style='text-align:right;color:#8892a4'>—</td>"
            f"<td style='text-align:right;color:#8892a4'>—</td>"
            f"<td style='font-size:.78rem;color:#8892a4'>{port}</td>"
            f"<td style='text-align:center;color:#10b981'>{eta}</td>"
            f"</tr>\n"
        )
    return rows


def _outbound_rows_from_expected(expected: list[dict]) -> str:
    """Render expected-outbound records in the AIS outbound table format (fallback)."""
    outbound = [r for r in expected if r.get("direction") == "OUTBOUND"]
    if not outbound:
        return ('<tr><td colspan="6" style="text-align:center;color:#8892a4;padding:24px">'
                'No outbound vessel data available — AIS offline, port schedules also empty.</td></tr>')
    rows = ""
    for r in outbound:
        name  = (r.get("ship_name") or "Unknown")[:28]
        cargo = r.get("cargo_category", "OTHER")
        port  = r.get("port_name", "—")
        eta   = r.get("eta", "—")
        rows += (
            f"<tr>"
            f"<td style='font-weight:500'>{name}</td>"
            f"<td>{_cargo_badge(cargo)}</td>"
            f"<td>{port}</td>"
            f"<td style='text-align:right;color:#8892a4'>—</td>"
            f"<td style='text-align:right;color:#8892a4'>—</td>"
            f"<td style='text-align:center;color:#f59e0b'>{eta}</td>"
            f"</tr>\n"
        )
    return rows


def _inbound_rows(vessels: list[dict]) -> str:
    if not vessels:
        return ('<tr><td colspan="7" style="text-align:center;color:#8892a4;padding:24px">'
                'No inbound vessels detected — AIS snapshot may be empty or stale.</td></tr>')
    rows = ""
    for v in vessels:
        name   = (v.get("ship_name") or "Unknown")[:28]
        cargo  = v.get("cargo_category", "OTHER")
        port   = v.get("nearest_port", "—")
        dist   = v.get("distance_nm", "—")
        speed  = float(v.get("speed") or 0)
        dest   = (v.get("destination") or "—")[:20]
        if speed > 0.5 and isinstance(dist, (int, float)):
            eta_h = dist / speed
            eta_str = f"{eta_h:.1f}&nbsp;h" if eta_h < 24 else f"{eta_h/24:.1f}&nbsp;d"
        else:
            eta_str = "—"
        rows += (
            f"<tr>"
            f"<td style='font-weight:500'>{name}</td>"
            f"<td>{_cargo_badge(cargo)}</td>"
            f"<td>{port}</td>"
            f"<td style='text-align:right;font-variant-numeric:tabular-nums'>{dist}</td>"
            f"<td style='text-align:right;font-variant-numeric:tabular-nums'>{speed:.1f}</td>"
            f"<td style='font-size:.78rem;color:#8892a4'>{dest}</td>"
            f"<td style='text-align:center'>{eta_str}</td>"
            f"</tr>\n"
        )
    return rows


def _outbound_rows(vessels: list[dict]) -> str:
    if not vessels:
        return ('<tr><td colspan="6" style="text-align:center;color:#8892a4;padding:24px">'
                'No outbound vessels detected — AIS snapshot may be empty or stale.</td></tr>')
    rows = ""
    for v in vessels:
        name   = (v.get("ship_name") or "Unknown")[:28]
        cargo  = v.get("cargo_category", "OTHER")
        port   = v.get("nearest_port", "—")
        dist   = v.get("distance_nm", "—")
        speed  = float(v.get("speed") or 0)
        course = float(v.get("course") or 0)
        arrow  = _course_arrow(course)
        rows += (
            f"<tr>"
            f"<td style='font-weight:500'>{name}</td>"
            f"<td>{_cargo_badge(cargo)}</td>"
            f"<td>{port}</td>"
            f"<td style='text-align:right;font-variant-numeric:tabular-nums'>{dist}</td>"
            f"<td style='text-align:right;font-variant-numeric:tabular-nums'>{speed:.1f}</td>"
            f"<td style='text-align:center'>{arrow} {int(course)}&deg;</td>"
            f"</tr>\n"
        )
    return rows


_MT_TO_BBL = 7.33  # approximate barrels per metric tonne for crude


def _crude_insight_section(crude_data: dict, analysis: str) -> str:
    """Build the HTML for the Crude Weekly Insight section."""
    if not crude_data:
        return ""

    stale        = crude_data.get("_stale", False)
    age_str      = crude_data.get("_age_str", "")
    official     = crude_data.get("official", {})
    live_d       = crude_data.get("live", {})
    rec          = crude_data.get("recommended_weekly_avg_mt")
    rec_note     = crude_data.get("recommendation_note", "")
    gen_at       = crude_data.get("generated_at", "")

    stale_badge  = (f'<span style="font-size:.7rem;color:#ef4444;margin-left:8px">'
                    f'&#9888; data {age_str} old</span>') if stale else ""
    fresh_badge  = (f'<span style="font-size:.7rem;color:#10b981;margin-left:8px">'
                    f'updated {age_str}</span>') if age_str and not stale else ""

    # ── recommended headline ──────────────────────────────────────────────────
    if rec:
        rec_bbl = int(rec * _MT_TO_BBL)
        headline_num  = f"{rec:,}"
        headline_bbl  = f"{rec_bbl:,}"
        headline_color = "#ef4444" if stale else "#fbbf24"
    else:
        headline_num  = "N/A"
        headline_bbl  = "—"
        headline_color = "#8892a4"

    # ── official card ─────────────────────────────────────────────────────────
    off_html = ""
    if official and not official.get("error"):
        off_wk  = official.get("weekly_avg_mt", 0)
        off_mo  = official.get("pol_crude_tonnes_monthly", 0)
        off_mon = official.get("report_month", "")
        off_sh  = official.get("pol_crude_share_pct", 0)
        off_html = f"""
      <div class="crude-card">
        <div class="crude-card-num">{off_wk:,}</div>
        <div class="crude-card-label">MT/week · Official</div>
        <div class="crude-card-sub">{off_mon} · {off_mo:,} MT/month · {off_sh}% of total port cargo</div>
      </div>"""

    # ── live card ─────────────────────────────────────────────────────────────
    live_html = ""
    if live_d and live_d.get("weekly_avg_mt") is not None:
        lv_wk  = live_d.get("weekly_avg_mt", 0)
        lv_vc  = live_d.get("vessel_count", 0)
        lv_dr  = live_d.get("date_range", "")
        sparse = live_d.get("data_sparse", True)
        sparse_flag = (' <span style="color:#f59e0b;font-size:.68rem">sparse</span>'
                       if sparse else "")
        live_html = f"""
      <div class="crude-card">
        <div class="crude-card-num">{lv_wk:,}{sparse_flag}</div>
        <div class="crude-card-label">MT/week · Live scraper</div>
        <div class="crude-card-sub">{lv_vc} crude vessel(s) · {lv_dr}</div>
      </div>"""

    # ── per-port breakdown ────────────────────────────────────────────────────
    per_port = live_d.get("per_port", {}) if live_d else {}
    port_rows_html = ""
    total_live = sum(per_port.values()) if per_port else 0
    if per_port:
        for port, mt in sorted(per_port.items(), key=lambda x: -x[1]):
            share = int(mt / total_live * 100) if total_live else 0
            bar_w = share
            port_rows_html += f"""
        <tr>
          <td style="font-weight:600">{port}</td>
          <td style="text-align:right;font-variant-numeric:tabular-nums;color:#fbbf24">{mt:,}</td>
          <td>
            <div style="background:#2e3352;border-radius:4px;height:8px;width:100%;min-width:60px">
              <div style="background:#fbbf24;border-radius:4px;height:8px;width:{bar_w}%"></div>
            </div>
          </td>
          <td style="text-align:right;color:#8892a4;font-size:.78rem">{share}%</td>
        </tr>"""

    port_table_html = ""
    if port_rows_html:
        port_table_html = f"""
    <div style="margin-top:18px">
      <div style="font-size:.8rem;font-weight:600;color:var(--muted);margin-bottom:10px;text-transform:uppercase;letter-spacing:.05em">Per-Port Breakdown (Live · 7-day)</div>
      <table>
        <thead>
          <tr>
            <th>Port</th>
            <th style="text-align:right">Crude (MT)</th>
            <th style="min-width:120px">Volume</th>
            <th style="text-align:right">Share</th>
          </tr>
        </thead>
        <tbody>{port_rows_html}
        </tbody>
      </table>
    </div>"""

    # ── Gemini analysis ───────────────────────────────────────────────────────
    analysis_html = ""
    if analysis:
        # Escape HTML chars, convert newlines to paragraphs
        safe = (analysis.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
        paragraphs = "".join(
            f"<p style='margin-bottom:.7em'>{p.strip()}</p>"
            for p in safe.split("\n\n") if p.strip()
        )
        analysis_html = f"""
    <details style="margin-top:20px">
      <summary style="cursor:pointer;font-size:.85rem;font-weight:600;color:#a78bfa;
                      padding:10px 14px;background:#1a1d27;border:1px solid #4c1d9544;
                      border-radius:8px;list-style:none;user-select:none">
        &#x2728; Gemini Analysis — Click to expand
      </summary>
      <div style="margin-top:12px;padding:16px 18px;background:#1a1d2790;border:1px solid #2e3352;
                  border-radius:8px;font-size:.82rem;line-height:1.65;color:#c4cad6">
        {paragraphs}
        <div style="margin-top:12px;font-size:.72rem;color:#8892a4">
          &#x1F916; Generated by Gemini 2.0 Flash · Pipeline data from {gen_at[:10] if gen_at else 'N/A'}
        </div>
      </div>
    </details>"""

    return f"""
  <!-- Crude Weekly Insight -->
  <section class="section" style="border-color:#f59e0b44;margin-bottom:28px">
    <div class="section-header" style="margin-bottom:20px">
      <h2 class="section-title" style="color:#fbbf24">
        &#128202; Crude Weekly Insight
        <span class="section-sub">India imports · avg MT/week{stale_badge}{fresh_badge}</span>
      </h2>
      <div style="font-size:.76rem;color:var(--muted);margin-top:4px">{rec_note}</div>
    </div>

    <!-- Headline + cards -->
    <div style="display:grid;grid-template-columns:auto 1fr 1fr;gap:20px;align-items:start;flex-wrap:wrap">

      <div style="text-align:center;padding:16px 24px;background:#f59e0b11;border:1px solid #f59e0b33;border-radius:10px;min-width:160px">
        <div style="font-size:2rem;font-weight:800;color:{headline_color};letter-spacing:-.5px;line-height:1">{headline_num}</div>
        <div style="font-size:.72rem;color:var(--muted);margin-top:5px">MT / week (recommended)</div>
        <div style="font-size:.72rem;color:#8892a4;margin-top:3px">&#8776; {headline_bbl} barrels</div>
      </div>
      {off_html}
      {live_html}
    </div>
    {port_table_html}
    {analysis_html}
  </section>"""


_ZONE_ICONS = {
    "Persian Gulf":                       "&#127467;",
    "Red Sea / Gulf of Aden":             "&#127465;",
    "Gulf of Aden / Arabian Sea entrance":"&#127465;",
    "Arabian Sea":                        "&#127470;",
    "Bay of Bengal":                      "&#127470;",
    "Indian Ocean (west)":                "&#127470;",
    "Indian Ocean (south)":               "&#127470;",
    "Strait of Malacca":                  "&#127474;",
    "South China Sea":                    "&#127464;",
    "Atlantic / South Africa":            "&#127467;",
}


def _vessel_watch_section(watch: dict, analysis: str) -> str:
    """Build the HTML for the Fleet Watch section."""
    if not watch:
        return ""

    stale   = watch.get("_stale", False)
    age_str = watch.get("_age_str", "")
    totals  = watch.get("totals", {})
    zones   = watch.get("sea_zones", {})
    gen_at  = watch.get("generated_at", "")
    ais_stale = watch.get("ais_stale", True)

    stale_badge = (f'<span style="font-size:.7rem;color:#ef4444;margin-left:8px">'
                   f'&#9888; data {age_str} old</span>') if stale else ""
    fresh_badge = (f'<span style="font-size:.7rem;color:#10b981;margin-left:8px">'
                   f'updated {age_str}</span>') if age_str and not stale else ""
    ais_warn    = (' <span style="font-size:.7rem;color:#f59e0b">(AIS stale)</span>'
                   if ais_stale else "")

    at_p   = totals.get("at_port",  0)
    inb    = totals.get("inbound",  0)
    outb   = totals.get("outbound", 0)
    ic     = totals.get("inbound_counts",  {})
    oc     = totals.get("outbound_counts", {})
    ac     = totals.get("at_port_counts",  {})

    # Summary cards
    cards_html = f"""
    <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin-bottom:20px">
      <div class="crude-card" style="background:#10b98108;border-color:#10b98122">
        <div class="crude-card-num" style="color:#10b981">{inb}</div>
        <div class="crude-card-label" style="color:#10b981">Inbound{ais_warn}</div>
        <div class="crude-card-sub">Crude {ic.get('CRUDE',0)} · LNG {ic.get('LNG',0)} · CNG {ic.get('CNG',0)}</div>
      </div>
      <div class="crude-card" style="background:#3b82f608;border-color:#3b82f622">
        <div class="crude-card-num" style="color:#60a5fa">{at_p}</div>
        <div class="crude-card-label" style="color:#60a5fa">At Indian Ports</div>
        <div class="crude-card-sub">Crude {ac.get('CRUDE',0)} · LNG {ac.get('LNG',0)} · CNG {ac.get('CNG',0)}</div>
      </div>
      <div class="crude-card" style="background:#f59e0b08;border-color:#f59e0b22">
        <div class="crude-card-num" style="color:#f59e0b">{outb}</div>
        <div class="crude-card-label" style="color:#f59e0b">Outbound</div>
        <div class="crude-card-sub">Crude {oc.get('CRUDE',0)} · LNG {oc.get('LNG',0)} · CNG {oc.get('CNG',0)}</div>
      </div>
    </div>"""

    # Sea zone breakdown
    zone_rows = ""
    total_zoned = sum(zones.values()) or 1
    for zone, cnt in sorted(zones.items(), key=lambda x: -x[1]):
        icon  = _ZONE_ICONS.get(zone, "&#127758;")
        share = int(cnt / total_zoned * 100)
        zone_rows += f"""
        <tr>
          <td>{icon} {zone}</td>
          <td style="text-align:right;color:#10b981">{cnt}</td>
          <td>
            <div style="background:#2e3352;border-radius:4px;height:7px;width:100%;min-width:60px">
              <div style="background:#10b981;border-radius:4px;height:7px;width:{share}%"></div>
            </div>
          </td>
          <td style="text-align:right;color:#8892a4;font-size:.78rem">{share}%</td>
        </tr>"""

    zone_table = ""
    if zone_rows:
        zone_table = f"""
    <div style="margin-top:0">
      <div style="font-size:.8rem;font-weight:600;color:var(--muted);margin-bottom:10px;
                  text-transform:uppercase;letter-spacing:.05em">Inbound Sea Zones</div>
      <table>
        <thead>
          <tr>
            <th>Sea Zone</th>
            <th style="text-align:right">Vessels</th>
            <th style="min-width:100px">Share</th>
            <th style="text-align:right">%</th>
          </tr>
        </thead>
        <tbody>{zone_rows}
        </tbody>
      </table>
    </div>"""

    # Hormuz pipeline panel
    hormuz_html = ""
    hormuz = watch.get("hormuz", {})
    if hormuz and not hormuz.get("stale"):
        hib   = hormuz.get("india_bound", 0)
        htrans = hormuz.get("transiting", 0)
        hgulf  = hormuz.get("in_gulf", 0)
        hibc   = hormuz.get("india_bound_counts", {})
        hdest  = hormuz.get("destination_ports", {})
        hflags = hormuz.get("flag_origins", {})
        hgen   = hormuz.get("generated_at", "")[:16].replace("T", " ")

        dest_rows = ""
        for port, cnt in sorted(hdest.items(), key=lambda x: -x[1])[:8]:
            dest_rows += (f"<tr><td>{port}</td>"
                          f"<td style='text-align:right;color:#10b981'>{cnt}</td></tr>\n")

        flag_rows = ""
        for flag, cnt in sorted(hflags.items(), key=lambda x: -x[1])[:6]:
            flag_rows += (f"<tr><td>{flag}</td>"
                          f"<td style='text-align:right;color:#60a5fa'>{cnt}</td></tr>\n")

        hormuz_html = f"""
    <div style="margin-top:20px;padding:16px 18px;background:#0f111780;
                border:1px solid #10b98133;border-radius:10px">
      <div style="font-size:.8rem;font-weight:700;color:#10b981;margin-bottom:12px;
                  text-transform:uppercase;letter-spacing:.06em">
        &#9875; Hormuz Pipeline · {hgen} UTC
      </div>
      <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:14px">
        <div style="text-align:center">
          <div style="font-size:1.6rem;font-weight:800;color:#10b981">{hib}</div>
          <div style="font-size:.7rem;color:var(--muted)">India-bound</div>
          <div style="font-size:.68rem;color:#8892a4">
            Crude {hibc.get('CRUDE',0)} · LNG {hibc.get('LNG',0)} · CNG {hibc.get('CNG',0)}
          </div>
        </div>
        <div style="text-align:center">
          <div style="font-size:1.6rem;font-weight:800;color:#60a5fa">{htrans}</div>
          <div style="font-size:.7rem;color:var(--muted)">Transiting Hormuz</div>
        </div>
        <div style="text-align:center">
          <div style="font-size:1.6rem;font-weight:800;color:#f59e0b">{hgulf}</div>
          <div style="font-size:.7rem;color:var(--muted)">In Persian Gulf</div>
        </div>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
        <div>
          <div style="font-size:.72rem;font-weight:600;color:var(--muted);margin-bottom:6px">
            INDIA DESTINATION PORTS</div>
          <table style="font-size:.78rem">{dest_rows or '<tr><td colspan=2 style="color:#8892a4">—</td></tr>'}</table>
        </div>
        <div>
          <div style="font-size:.72rem;font-weight:600;color:var(--muted);margin-bottom:6px">
            VESSEL FLAGS</div>
          <table style="font-size:.78rem">{flag_rows or '<tr><td colspan=2 style="color:#8892a4">—</td></tr>'}</table>
        </div>
      </div>
    </div>"""

    # Gemini analysis
    analysis_html = ""
    if analysis:
        safe = analysis.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        paragraphs = "".join(
            f"<p style='margin-bottom:.7em'>{p.strip()}</p>"
            for p in safe.split("\n\n") if p.strip()
        )
        analysis_html = f"""
    <details style="margin-top:20px">
      <summary style="cursor:pointer;font-size:.85rem;font-weight:600;color:#10b981;
                      padding:10px 14px;background:#1a1d27;border:1px solid #10b98144;
                      border-radius:8px;list-style:none;user-select:none">
        &#128674; Gemini Route Analysis — Click to expand
      </summary>
      <div style="margin-top:12px;padding:16px 18px;background:#1a1d2790;border:1px solid #2e3352;
                  border-radius:8px;font-size:.82rem;line-height:1.65;color:#c4cad6">
        {paragraphs}
        <div style="margin-top:12px;font-size:.72rem;color:#8892a4">
          &#x1F916; Generated by Gemini 2.5 Flash · {gen_at[:16].replace("T"," ")} UTC
        </div>
      </div>
    </details>"""

    return f"""
  <!-- Fleet Watch -->
  <section class="section" style="border-color:#10b98144;margin-bottom:28px">
    <div class="section-header" style="margin-bottom:16px">
      <h2 class="section-title" style="color:#10b981">
        &#128674; Fleet Watch
        <span class="section-sub">Crude · LNG · CNG vessels en-route &amp; at Indian ports{stale_badge}{fresh_badge}</span>
      </h2>
    </div>
    {cards_html}
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:24px">
      {zone_table}
    </div>
    {hormuz_html}
    {analysis_html}
  </section>"""


def build_html(records: list[dict], stats: dict, generated_at: str, history: list,
               live: dict | None = None, expected: list | None = None,
               crude_data: dict | None = None, crude_analysis: str = "",
               watch_data: dict | None = None, watch_analysis: str = "") -> str:
    total      = stats["total_in_port"]
    cargo      = stats["cargo_counts"]
    busiest    = stats["busiest_port"]
    inbound    = (live or {}).get("total_inbound", 0)
    outbound   = (live or {}).get("total_outbound", 0)
    ais_live   = bool(live)

    expected          = expected or []
    exp_inbound_count  = sum(1 for r in expected if r.get("direction", "INBOUND") == "INBOUND")
    exp_outbound_count = sum(1 for r in expected if r.get("direction") == "OUTBOUND")
    exp_arrival_rows   = _expected_arrival_rows(expected)
    exp_outbound_rows  = _expected_outbound_rows(expected)

    inbound_vessels  = (live or {}).get("inbound", [])
    outbound_vessels = (live or {}).get("outbound", [])
    ais_fetched_at   = (live or {}).get("fetched_at", "")

    # When AIS data is unavailable, fall back to port-scheduled expected movements
    ais_inbound_source  = "AIS"
    ais_outbound_source = "AIS"
    if not inbound_vessels:
        mov_inbound_rows = _inbound_rows_from_expected(expected)
        ais_inbound_source = "Port Schedules"
    else:
        mov_inbound_rows = _inbound_rows(inbound_vessels)
    if not outbound_vessels:
        mov_outbound_rows = _outbound_rows_from_expected(expected)
        ais_outbound_source = "Port Schedules"
    else:
        mov_outbound_rows = _outbound_rows(outbound_vessels)

    # Counts: AIS or fall back to expected counts
    if inbound_vessels:
        inbound_count_label = f"{inbound} vessel{'s' if inbound != 1 else ''}"
    else:
        fb_in = sum(1 for r in expected if r.get("direction", "INBOUND") == "INBOUND")
        inbound_count_label = (f"{fb_in} vessel{'s' if fb_in != 1 else ''} · {ais_inbound_source}"
                               if fb_in else f"0 vessels · {ais_inbound_source}")
    if outbound_vessels:
        outbound_count_label = f"{outbound} vessel{'s' if outbound != 1 else ''}"
    else:
        fb_out = sum(1 for r in expected if r.get("direction") == "OUTBOUND")
        outbound_count_label = (f"{fb_out} vessel{'s' if fb_out != 1 else ''} · {ais_outbound_source}"
                                if fb_out else f"0 vessels · {ais_outbound_source}")

    ais_note = (f'<span class="stale-note">AIS: {ais_fetched_at} UTC</span>'
                if ais_fetched_at else
                '<span class="stale-note">AIS offline · showing port schedules</span>')
    ais_footer = (f'AIS snapshot: {ais_fetched_at} UTC'
                  if ais_fetched_at else 'AIS offline — vessel movements from port schedule scrapers')

    crude_section = _crude_insight_section(crude_data or {}, crude_analysis)
    watch_section = _vessel_watch_section(watch_data or {}, watch_analysis)

    _dot = '<span class="ais-dot"></span>'
    _off = '<span class="ais-offline">AIS offline</span> '
    inbound_label  = f"{_dot}Inbound to India" if ais_live else f"{_off}Inbound"
    outbound_label = f"{_dot}Outbound"          if ais_live else f"{_off}Outbound"
    hist_json  = json.dumps(history)
    vessel_rows     = _vessel_rows(records)
    port_rows       = _port_summary_rows(stats["port_counts"])

    chart_script = ""
    chart_section = """
  <section class="section">
    <div class="section-header">
      <h2 class="section-title">Vessels At Port <span class="section-sub">(30-min snapshots)</span></h2>
    </div>
    <div style="position:relative;height:260px"><canvas id="histChart"></canvas></div>
  </section>"""

    if len(history) >= 2:
        chart_script = f"""
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<script>
  const H = {hist_json};
  const labels  = H.map(d => d.ts_ist);
  const inPort  = H.map(d => d.total_in_port);
  const crude   = H.map(d => d.crude);
  const lng     = H.map(d => d.lng);
  const petro   = H.map(d => d.petroleum);
  Chart.defaults.color = "#8892a4";
  Chart.defaults.borderColor = "#2e3352";
  new Chart(document.getElementById("histChart"), {{
    type: "line",
    data: {{
      labels,
      datasets: [
        {{label:"Total In-Port",data:inPort,borderColor:"#e2e8f0",backgroundColor:"#e2e8f011",tension:.3,fill:true,pointRadius:H.length>40?1:3}},
        {{label:"Crude",       data:crude,  borderColor:"#fbbf24",backgroundColor:"#fbbf2411",tension:.3,fill:false,pointRadius:0}},
        {{label:"LNG",         data:lng,    borderColor:"#60a5fa",backgroundColor:"transparent",tension:.3,fill:false,pointRadius:0}},
        {{label:"Petroleum",   data:petro,  borderColor:"#fb923c",backgroundColor:"transparent",tension:.3,fill:false,pointRadius:0}},
      ]
    }},
    options:{{
      responsive:true,maintainAspectRatio:false,
      interaction:{{mode:"index",intersect:false}},
      scales:{{
        x:{{ticks:{{maxTicksLimit:8,maxRotation:30}},grid:{{color:"#2e335244"}}}},
        y:{{min:0,ticks:{{stepSize:1}},grid:{{color:"#2e335244"}},
           title:{{display:true,text:"vessels",font:{{size:11}}}}}}
      }},
      plugins:{{
        legend:{{labels:{{color:"#e2e8f0",usePointStyle:true,padding:16}}}},
        tooltip:{{backgroundColor:"#1a1d27",borderColor:"#2e3352",borderWidth:1,
                  titleColor:"#e2e8f0",bodyColor:"#8892a4",padding:10}}
      }}
    }}
  }});
</script>"""
    else:
        chart_section = """
  <section class="section" style="text-align:center;padding:36px 24px">
    <p style="color:#8892a4">Building history — check back after the next update.</p>
  </section>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1.0"/>
  <title>India Energy Vessel Pipeline</title>
  <style>
    *,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
    :root{{
      --bg:#0f1117;--surface:#1a1d27;--surface2:#22263a;
      --border:#2e3352;--accent:#f59e0b;--accent2:#3b82f6;
      --accent3:#10b981;--text:#e2e8f0;--muted:#8892a4;
      --red:#ef4444;--radius:14px;--shadow:0 4px 24px rgba(0,0,0,.45);
    }}
    body{{font-family:"Segoe UI",system-ui,sans-serif;background:var(--bg);color:var(--text);min-height:100vh}}
    a{{color:var(--accent2);text-decoration:none}}
    header{{background:var(--surface);border-bottom:1px solid var(--border);padding:18px 32px}}
    .hdr{{max-width:1200px;margin:0 auto;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px}}
    .hdr-left{{display:flex;align-items:center;gap:14px}}
    .hdr-icon{{font-size:2.2rem}}
    h1{{font-size:1.35rem;font-weight:700}}
    .subtitle{{font-size:.82rem;color:var(--muted);margin-top:2px}}
    .badge{{font-size:.78rem;color:var(--muted);background:var(--surface2);border:1px solid var(--border);border-radius:20px;padding:5px 12px}}
    .hdr-links{{display:flex;align-items:center;gap:8px;flex-wrap:wrap}}
    main{{max-width:1200px;margin:36px auto;padding:0 24px 56px}}

    /* Stats strip */
    .stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:16px;margin-bottom:16px}}
    .stats-cargo{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:16px;margin-bottom:32px}}
    .stat{{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:20px 18px;box-shadow:var(--shadow);text-align:center}}
    .stat-num{{font-size:2.2rem;font-weight:800;letter-spacing:-.5px;line-height:1}}
    .stat-label{{font-size:.74rem;color:var(--muted);margin-top:6px}}
    .stat-crude   {{border-color:#f59e0b44}} .stat-crude   .stat-num{{color:#fbbf24}}
    .stat-lng     {{border-color:#3b82f644}} .stat-lng     .stat-num{{color:#60a5fa}}
    .stat-cng     {{border-color:#10b98144}} .stat-cng     .stat-num{{color:#34d399}}
    .stat-petro   {{border-color:#f9731644}} .stat-petro   .stat-num{{color:#fb923c}}
    .stat-total   {{border-color:#e2e8f033}} .stat-total   .stat-num{{color:#e2e8f0}}
    .stat-inbound {{border-color:#10b98144}} .stat-inbound .stat-num{{color:#10b981}}
    .stat-outbound{{border-color:#f59e0b44}} .stat-outbound .stat-num{{color:#f59e0b}}
    .stat-busiest {{grid-column:span 2}}     .stat-busiest .stat-num{{font-size:1.3rem;color:var(--accent)}}
    .ais-dot{{display:inline-block;width:7px;height:7px;border-radius:50%;background:#10b981;margin-right:5px;vertical-align:middle}}
    .ais-offline{{color:var(--muted);font-size:.74rem}}

    /* Sections */
    .section{{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:24px 24px 20px;box-shadow:var(--shadow);margin-bottom:28px}}
    .section-header{{margin-bottom:18px}}
    .section-title{{font-size:1rem;font-weight:700}}
    .section-sub{{font-size:.78rem;font-weight:400;color:var(--muted);margin-left:6px}}

    /* Tables */
    table{{width:100%;border-collapse:collapse;font-size:.82rem}}
    th{{text-align:left;color:var(--muted);font-weight:600;padding:8px 10px;border-bottom:1px solid var(--border)}}
    td{{padding:8px 10px;border-bottom:1px solid #2e335233;vertical-align:middle}}
    tr:last-child td{{border-bottom:none}}
    tr:hover td{{background:#ffffff05}}

    /* Footer */
    .footer{{font-size:.74rem;color:var(--muted);text-align:center;padding:16px 0;display:flex;align-items:center;justify-content:center;flex-wrap:wrap;gap:8px}}
    .sep{{opacity:.4}}

    @media(max-width:640px){{
      header{{padding:14px 16px}} main{{padding:0 14px 40px;margin-top:24px}}
      .stats{{grid-template-columns:repeat(2,1fr)}}
      .stat-busiest{{grid-column:span 2}}
      h1{{font-size:1.1rem}}
    }}
    .movement-grid{{display:grid;grid-template-columns:1fr 1fr;gap:24px;margin-bottom:28px}}
    .inbound-section .section-title{{color:#10b981}}
    .outbound-section .section-title{{color:#f59e0b}}
    .stale-note{{font-size:.72rem;color:var(--muted);font-style:italic;margin-left:8px}}
    @media(max-width:900px){{.movement-grid{{grid-template-columns:1fr}}}}
    .crude-card{{padding:14px 18px;background:#f59e0b0d;border:1px solid #f59e0b22;border-radius:10px}}
    .crude-card-num{{font-size:1.5rem;font-weight:800;color:#fbbf24;line-height:1;letter-spacing:-.5px}}
    .crude-card-label{{font-size:.72rem;color:var(--muted);margin-top:5px;font-weight:600}}
    .crude-card-sub{{font-size:.7rem;color:#8892a4;margin-top:3px}}
  </style>
</head>
<body>
<header>
  <div class="hdr">
    <div class="hdr-left">
      <span class="hdr-icon">&#128674;</span>
      <div>
        <h1>India Energy Vessel Pipeline</h1>
        <p class="subtitle">Live tracking — Crude, LNG, CNG &amp; Petroleum tankers at Indian ports</p>
      </div>
    </div>
    <div class="hdr-links">
      <a class="badge" href="https://sushobhanpatankar.github.io/Ind_crude_oil/" target="_blank" rel="noopener">&#128202; Crude Oil Prices</a>
      <span class="badge">Updated: {generated_at} IST</span>
    </div>
  </div>
</header>

<main>

  <!-- Pipeline row: inbound → at port → outbound -->
  <div class="stats">
    <div class="stat stat-inbound">
      <div class="stat-num">{inbound}</div>
      <div class="stat-label">{inbound_label}</div>
    </div>
    <div class="stat stat-total">
      <div class="stat-num">{total}</div>
      <div class="stat-label">At Indian Ports</div>
    </div>
    <div class="stat stat-outbound">
      <div class="stat-num">{outbound}</div>
      <div class="stat-label">{outbound_label}</div>
    </div>
  </div>

  <!-- Cargo breakdown -->
  <div class="stats-cargo">
    <div class="stat stat-crude">
      <div class="stat-num">{cargo.get("CRUDE", 0)}</div>
      <div class="stat-label">Crude Tankers</div>
    </div>
    <div class="stat stat-lng">
      <div class="stat-num">{cargo.get("LNG", 0)}</div>
      <div class="stat-label">LNG Carriers</div>
    </div>
    <div class="stat stat-cng">
      <div class="stat-num">{cargo.get("CNG", 0)}</div>
      <div class="stat-label">CNG Carriers</div>
    </div>
    <div class="stat stat-petro">
      <div class="stat-num">{cargo.get("PETROLEUM", 0)}</div>
      <div class="stat-label">Petroleum</div>
    </div>
    <div class="stat stat-busiest">
      <div class="stat-num">{busiest}</div>
      <div class="stat-label">Busiest Port</div>
    </div>
  </div>

  {watch_section}

  {crude_section}

  <!-- Expected vessel arrivals (port-scheduled) -->
  <div class="movement-grid">

    <section class="section inbound-section">
      <div class="section-header">
        <h2 class="section-title">Expected Arrivals
          <span class="section-sub">({exp_inbound_count} vessels · Paradip &amp; Mundra port schedules)</span>
        </h2>
      </div>
      <div style="overflow-x:auto">
      <table>
        <thead>
          <tr>
            <th>Vessel</th>
            <th>Cargo</th>
            <th>Port</th>
            <th>ETA</th>
            <th style="text-align:right">Qty&nbsp;(MT)</th>
            <th>Source</th>
          </tr>
        </thead>
        <tbody>
          {exp_arrival_rows}
        </tbody>
      </table>
      </div>
    </section>

    <section class="section outbound-section">
      <div class="section-header">
        <h2 class="section-title">Expected Loadings
          <span class="section-sub">({exp_outbound_count} vessels · arriving to load &amp; depart)</span>
        </h2>
      </div>
      <div style="overflow-x:auto">
      <table>
        <thead>
          <tr>
            <th>Vessel</th>
            <th>Cargo</th>
            <th>Port</th>
            <th>ETA to Port</th>
            <th style="text-align:right">Qty&nbsp;(MT)</th>
          </tr>
        </thead>
        <tbody>
          {exp_outbound_rows}
        </tbody>
      </table>
      </div>
    </section>

  </div>

  <!-- AIS live inbound / outbound (when AIS snapshot is fresh) -->
  <div class="movement-grid">

    <section class="section inbound-section">
      <div class="section-header">
        <h2 class="section-title">Inbound to India
          <span class="section-sub">({inbound_count_label}) {ais_note}</span>
        </h2>
      </div>
      <div style="overflow-x:auto">
      <table>
        <thead>
          <tr>
            <th>Vessel</th>
            <th>Cargo</th>
            <th>Nearest Port</th>
            <th style="text-align:right">Dist&nbsp;(nm)</th>
            <th style="text-align:right">Speed&nbsp;(kn)</th>
            <th>Destination</th>
            <th style="text-align:center">ETA</th>
          </tr>
        </thead>
        <tbody>
          {mov_inbound_rows}
        </tbody>
      </table>
      </div>
    </section>

    <section class="section outbound-section">
      <div class="section-header">
        <h2 class="section-title">Outbound from India
          <span class="section-sub">({outbound_count_label}) {ais_note}</span>
        </h2>
      </div>
      <div style="overflow-x:auto">
      <table>
        <thead>
          <tr>
            <th>Vessel</th>
            <th>Cargo</th>
            <th>Nearest Port</th>
            <th style="text-align:right">Dist&nbsp;(nm)</th>
            <th style="text-align:right">Speed&nbsp;(kn)</th>
            <th style="text-align:center">Heading</th>
          </tr>
        </thead>
        <tbody>
          {mov_outbound_rows}
        </tbody>
      </table>
      </div>
    </section>

  </div>

  <!-- History chart -->
  {chart_section}

  <!-- Port summary table -->
  <section class="section">
    <div class="section-header">
      <h2 class="section-title">Port Summary</h2>
    </div>
    <table>
      <thead>
        <tr>
          <th>Port</th>
          <th style="text-align:right;color:#fbbf24">Crude</th>
          <th style="text-align:right;color:#60a5fa">LNG</th>
          <th style="text-align:right;color:#34d399">CNG</th>
          <th style="text-align:right;color:#fb923c">Petroleum</th>
          <th style="text-align:right">Total</th>
        </tr>
      </thead>
      <tbody>
        {port_rows}
      </tbody>
    </table>
  </section>

  <!-- Vessel list -->
  <section class="section">
    <div class="section-header">
      <h2 class="section-title">Vessels Currently At Port <span class="section-sub">({total} vessels · scraped from port authority websites)</span></h2>
    </div>
    <div style="overflow-x:auto">
    <table>
      <thead>
        <tr>
          <th>Vessel</th>
          <th>Port</th>
          <th>Berth</th>
          <th>Cargo</th>
          <th>Status</th>
          <th>Arrival</th>
        </tr>
      </thead>
      <tbody>
        {vessel_rows}
      </tbody>
    </table>
    </div>
  </section>

  <div class="footer">
    <span>Data fetched: {generated_at} IST</span>
    <span class="sep">·</span>
    <span>Sources: JNPT · Paradip Port · Adani Mundra · Vizag Port</span>
    <span class="sep">·</span>
    <span>Auto-updates every 30 minutes via GitHub Actions</span>
    <span class="sep">·</span>
    <span>{ais_footer}</span>
  </div>

</main>
{chart_script}
</body>
</html>"""


# ─────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────

async def _main():
    print("Running port scrapers...")
    records, expected = await asyncio.gather(
        run_scrapers(),
        run_expected_scrapers(),
    )
    print(f"Total vessels scraped: {len(records)}")
    print(f"Total expected arrivals: {len(expected)}")

    live           = load_ais_snapshot()
    crude_data     = load_crude_weekly()
    crude_analysis = load_crude_analysis()
    watch_data     = load_vessel_watch()
    watch_analysis = load_vessel_watch_analysis()

    stats = compute_stats(records)
    print(f"Stats: {stats['total_in_port']} in port, busiest={stats['busiest_port']}")

    ist_offset   = timedelta(hours=5, minutes=30)
    now_utc      = datetime.now(timezone.utc)
    now_ist      = now_utc + ist_offset
    generated_at = now_ist.strftime("%d %b %Y, %I:%M %p")
    ts_ist_short = now_ist.strftime("%d %b, %I:%M %p")

    history = load_history()
    history.append({
        "ts_utc":      now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ts_ist":      ts_ist_short,
        "total_in_port": stats["total_in_port"],
        "crude":       stats["cargo_counts"].get("CRUDE", 0),
        "lng":         stats["cargo_counts"].get("LNG", 0),
        "cng":         stats["cargo_counts"].get("CNG", 0),
        "petroleum":   stats["cargo_counts"].get("PETROLEUM", 0),
        "busiest_port": stats["busiest_port"],
        "inbound":     live.get("total_inbound", 0),
        "outbound":    live.get("total_outbound", 0),
        "berthed":     sum(1 for r in records if r.get("activity") == "BERTHED"),
        "anchored":    sum(1 for r in records if r.get("activity") == "ANCHORED"),
        "ports":       {p: c["total"] for p, c in stats["port_counts"].items()},
    })
    save_history(history)
    print(f"History: {len(history)} point(s) saved to {HISTORY_FILE}")

    os.makedirs("docs", exist_ok=True)
    html = build_html(records, stats, generated_at, history, live, expected,
                      crude_data, crude_analysis, watch_data, watch_analysis)
    with open("docs/index.html", "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Generated docs/index.html at {generated_at} IST")


def main():
    asyncio.run(_main())


if __name__ == "__main__":
    main()

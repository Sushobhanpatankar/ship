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

from scrapers.jnpt_scraper import JNPTScraper          # noqa: E402
from scrapers.mundra_scraper import MundraScraper       # noqa: E402
from scrapers.paradip_scraper import ParadipScraper     # noqa: E402
from scrapers.vizag_scraper import VizagScraper         # noqa: E402

HISTORY_FILE    = "docs/ships_data.json"
AIS_SNAPSHOT    = "docs/ais_snapshot.json"
MAX_HISTORY     = 336   # 7 days × 48 half-hours
SNAPSHOT_MAX_AGE_HOURS = 7   # treat snapshot as stale if older than this


# ─────────────────────────────────────────────────────────────
# Scraper runner
# ─────────────────────────────────────────────────────────────

async def run_scrapers() -> list[dict]:
    scrapers = [
        ("JNPT",    JNPTScraper()),
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


# ─────────────────────────────────────────────────────────────
# AIS snapshot (written by fetch_ais_snapshot.py every 6 hours)
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


def build_html(records: list[dict], stats: dict, generated_at: str, history: list,
               live: dict | None = None) -> str:
    total      = stats["total_in_port"]
    cargo      = stats["cargo_counts"]
    busiest    = stats["busiest_port"]
    inbound    = (live or {}).get("total_inbound", 0)
    outbound   = (live or {}).get("total_outbound", 0)
    ais_live   = bool(live)

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
    <span>AIS layer active when ship tracker server is running</span>
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
    records = await run_scrapers()
    print(f"Total vessels scraped: {len(records)}")

    live = load_ais_snapshot()

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
    })
    save_history(history)
    print(f"History: {len(history)} point(s) saved to {HISTORY_FILE}")

    os.makedirs("docs", exist_ok=True)
    html = build_html(records, stats, generated_at, history, live)
    with open("docs/index.html", "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Generated docs/index.html at {generated_at} IST")


def main():
    asyncio.run(_main())


if __name__ == "__main__":
    main()

"""
Hormuz Tracker
==============
Streams AIS data from the Persian Gulf and Gulf of Oman, filters for
crude/LNG/CNG tankers with India-bound destinations or en-route to India,
and saves a snapshot to docs/hormuz_snapshot.json.

Adapted from yasumorishima/hormuz-ship-tracker:
  - Persian Gulf + Gulf of Oman bounding box
  - Strait of Hormuz gate line (26.05°N 56.50°E → 26.65°N 56.10°E)
  - Destination normalization and MMSI → flag lookup

Usage:
    python hormuz_tracker.py              # stream 300s (default)
    python hormuz_tracker.py --seconds 120
    python hormuz_tracker.py --seconds 60 --output docs/hormuz_snapshot.json

Environment:
    AISSTREAM_API_KEY   — aisstream.io WebSocket API key (required)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import websockets

# ── Output path ──────────────────────────────────────────────────────────────
DEFAULT_OUTPUT  = Path("docs/hormuz_snapshot.json")
STREAM_SECONDS  = 300   # override via --seconds / AIS_STREAM_SECONDS env

# ── AIS ship types that are energy tankers ────────────────────────────────────
ENERGY_SHIP_TYPES = list(range(80, 90))   # 80-89 inclusive

# Map AIS ship-type integer → cargo category (from our config.py)
_CARGO_MAP: dict[int, str] = {
    80: "CRUDE", 81: "PETROLEUM", 82: "PETROLEUM", 83: "LNG",
    84: "CNG",   85: "PETROLEUM", 86: "PETROLEUM", 87: "PETROLEUM",
    88: "CRUDE", 89: "OTHER",
}

# ── Bounding boxes (aisstream.io format: [[[min_lat, min_lon],[max_lat, max_lon]], ...])
# Extended box: Persian Gulf + Strait of Hormuz + Gulf of Oman + Arabian Sea approach
# Starts at 45°E to capture full Persian Gulf (starts ~48°E) + extends to 75°E for
# Arabian Sea vessels already en-route toward India.
# Adapted from yasumorishima/hormuz-ship-tracker (BBOX = [[22.0, 48.0], [30.5, 60.0]])
BBOXES = [
    [[14.0, 45.0], [31.0, 75.0]],   # Persian Gulf + Hormuz + Gulf of Oman + Arabian Sea
]

# ── Strait of Hormuz gate line (from yasumorishima/hormuz-ship-tracker analytics.py) ──
# Point A: Musandam/Oman side  Point B: Iran/Qeshm side
HORMUZ_GATE_A = (26.05, 56.50)   # (lat, lon)
HORMUZ_GATE_B = (26.65, 56.10)

# ── India destination keywords (from our config.py INDIA_DESTINATION_KEYWORDS) ──
_INDIA_DEST_KEYWORDS = {
    "vadinar", "mundra", "jnpt", "nhava sheva", "hazira", "dahej",
    "kochi", "cochin", "mangalore", "chennai", "madras", "paradip",
    "visakhapatnam", "vizag", "haldia", "kandla", "sikka", "mumbai",
    "india", "indian", "in ", "injai", "inmun", "innsa", "inhza",
    "indah", "incok", "inmrm", "inmaa", "inprt", "invtz", "inhal",
    "inknd", "deendayal", "surat", "okha",
}

# ── Destination normalization (adapted from hormuz-ship-tracker destinations.py) ──
_INDIA_PORT_VARIANTS: dict[str, str] = {
    # Canonical : raw AIS variants (upper-cased for lookup)
    "Paradip":       ["PARADIP", "PARADEEP", "IN PRT", "INPRT"],
    "Mundra":        ["MUNDRA", "ADANI MUNDRA", "IN MUN", "INMUN"],
    "Mumbai/JNPT":   ["MUMBAI", "IN BOM", "NHAVA SHEVA", "JNPT", "JAWAHARLAL NEHRU", "INNSA"],
    "Kochi":         ["KOCHI", "COCHIN", "IN COK", "INCOK"],
    "Hazira":        ["HAZIRA", "IN HZA", "INHZA"],
    "Dahej":         ["DAHEJ", "IN DAH", "INDAH"],
    "Vadinar":       ["VADINAR", "RELIANCE VADINAR"],
    "Kandla":        ["KANDLA", "DEENDAYAL", "IN KND", "INKND"],
    "Mangalore":     ["MANGALORE", "MANGALURU", "IN MRM", "INMRM", "NMPT"],
    "Chennai":       ["CHENNAI", "MADRAS", "IN MAA", "INMAA"],
    "Vizag":         ["VISAKHAPATNAM", "VIZAG", "IN VTZ", "INVTZ"],
    "Haldia":        ["HALDIA", "IN HAL", "INHAL"],
    "Sikka":         ["SIKKA", "ESSAR SIKKA"],
    "India (general)": ["INDIA", "INDIA OPL", "FOR INDIA"],
}

_INDIA_LOOKUP: dict[str, str] = {}
for _canon, _variants in _INDIA_PORT_VARIANTS.items():
    for _v in _variants:
        _INDIA_LOOKUP[_v.upper()] = _canon


def _normalize_dest_india(raw: str | None) -> str | None:
    """Return canonical Indian port name if dest is India-bound, else None."""
    if not raw:
        return None
    cleaned = re.sub(r"\s+", " ", raw.strip().upper())
    # Exact match
    if cleaned in _INDIA_LOOKUP:
        return _INDIA_LOOKUP[cleaned]
    # Keyword substring match
    lower = cleaned.lower()
    for kw in _INDIA_DEST_KEYWORDS:
        if kw in lower:
            # Try to get canonical name
            for variant, canon in _INDIA_LOOKUP.items():
                if variant in cleaned:
                    return canon
            return "India"
    return None


# ── MMSI → flag (from yasumorishima/hormuz-ship-tracker country_codes.py) ────
_MID_MAP: dict[int, tuple[str, str]] = {
    403: ("SA", "Saudi Arabia"),  408: ("BH", "Bahrain"),
    412: ("CN", "China"),         413: ("CN", "China"),
    414: ("CN", "China"),         419: ("IN", "India"),
    422: ("IR", "Iran"),          431: ("JP", "Japan"),
    432: ("JP", "Japan"),         440: ("KR", "South Korea"),
    441: ("KR", "South Korea"),   447: ("KW", "Kuwait"),
    450: ("OM", "Oman"),          461: ("KW", "Kuwait"),
    466: ("QA", "Qatar"),         470: ("AE", "UAE"),
    471: ("AE", "UAE"),           473: ("AE", "UAE"),
    477: ("HK", "Hong Kong"),     209: ("BS", "Bahamas"),
    256: ("MT", "Malta"),         319: ("KY", "Cayman Islands"),
    351: ("PA", "Panama"),        352: ("PA", "Panama"),
    353: ("PA", "Panama"),        354: ("PA", "Panama"),
    355: ("PA", "Panama"),        356: ("PA", "Panama"),
    357: ("PA", "Panama"),        370: ("PA", "Panama"),
    371: ("PA", "Panama"),        372: ("PA", "Panama"),
    373: ("PA", "Panama"),        374: ("PA", "Panama"),
    375: ("VC", "St Vincent"),    376: ("VC", "St Vincent"),
    377: ("VC", "St Vincent"),    525: ("ID", "Indonesia"),
    533: ("MY", "Malaysia"),      538: ("MH", "Marshall Islands"),
    548: ("PH", "Philippines"),   563: ("SG", "Singapore"),
    564: ("SG", "Singapore"),     565: ("SG", "Singapore"),
    620: ("KM", "Comoros"),       621: ("KM", "Comoros"),
    636: ("LR", "Liberia"),       637: ("LR", "Liberia"),
    219: ("DK", "Denmark"),       220: ("DK", "Denmark"),
    224: ("ES", "Spain"),         226: ("FR", "France"),
    229: ("MT", "Malta"),         235: ("GB", "United Kingdom"),
    237: ("GR", "Greece"),        239: ("GR", "Greece"),
    240: ("GR", "Greece"),        241: ("GR", "Greece"),
    244: ("NL", "Netherlands"),   247: ("IT", "Italy"),
    248: ("MT", "Malta"),         249: ("MT", "Malta"),
    255: ("PT", "Portugal"),      271: ("TR", "Turkey"),
    272: ("TR", "Turkey"),        273: ("RU", "Russia"),
    303: ("US", "United States"), 338: ("US", "United States"),
    366: ("US", "United States"), 669: ("IQ", "Iraq"),
    677: ("YE", "Yemen"),
}


def _mmsi_to_flag(mmsi: str | int | None) -> tuple[str, str]:
    """Return (iso_code, country_name) from MMSI, or ('', '') if unknown."""
    try:
        mid = int(str(mmsi)[:3]) if mmsi else 0
        return _MID_MAP.get(mid, ("", ""))
    except (ValueError, TypeError):
        return ("", "")


# ── Sea zone from lat/lon ─────────────────────────────────────────────────────

def _sea_zone(lat: float, lon: float) -> str:
    if 23 <= lat <= 30.5 and 48 <= lon <= 56.5:
        return "Persian Gulf"
    if 25.5 <= lat <= 27.5 and 55.5 <= lon <= 57.5:
        return "Strait of Hormuz"
    if 22 <= lat <= 26 and 56.5 <= lon <= 60:
        return "Gulf of Oman"
    if 11 <= lat <= 25 and 43 <= lon <= 56.5:
        return "Red Sea / Gulf of Aden"
    if 5 <= lat <= 22 and 56 <= lon <= 78:
        return "Arabian Sea"
    if -5 <= lat <= 10 and 50 <= lon <= 75:
        return "Indian Ocean"
    if 5 <= lat <= 22 and 78 <= lon <= 100:
        return "Bay of Bengal"
    return "Other"


# ── Strait crossing side check (from hormuz-ship-tracker analytics.py) ────────

def _cross_product_z(ax, ay, bx, by, px, py) -> float:
    """2-D cross product of (B-A) × (P-A). Sign tells which side P is on."""
    return (bx - ax) * (py - ay) - (by - ay) * (px - ax)


def _side_of_hormuz_gate(lat: float, lon: float) -> str:
    """Return 'gulf_side' (inside Persian Gulf) or 'ocean_side' (Gulf of Oman)."""
    # Gate A→B vector points roughly NW. Positive cross product = east/ocean side.
    ax, ay = HORMUZ_GATE_A[1], HORMUZ_GATE_A[0]   # lon, lat
    bx, by = HORMUZ_GATE_B[1], HORMUZ_GATE_B[0]
    z = _cross_product_z(ax, ay, bx, by, lon, lat)
    return "ocean_side" if z > 0 else "gulf_side"


def _nm_from_gate(lat: float, lon: float) -> float:
    """Distance (nm) from vessel to nearest point on the Hormuz gate line."""
    # Simple: distance to midpoint of gate
    mlat = (HORMUZ_GATE_A[0] + HORMUZ_GATE_B[0]) / 2
    mlon = (HORMUZ_GATE_A[1] + HORMUZ_GATE_B[1]) / 2
    dlat = (lat - mlat) * 60
    dlon = (lon - mlon) * 60 * math.cos(math.radians(lat))
    return round(math.sqrt(dlat ** 2 + dlon ** 2), 1)


# ── AIS WebSocket collector ───────────────────────────────────────────────────

async def stream_hormuz(api_key: str, seconds: int) -> tuple[dict, dict]:
    """
    Stream aisstream.io for `seconds`, covering the Persian Gulf + Gulf of Oman.
    Returns (vessel_meta, vessel_pos) dicts keyed by MMSI string.
    Only energy tankers (ship_type 80-89) are stored.
    """
    vessel_meta: dict[str, dict] = {}
    vessel_pos:  dict[str, dict] = {}

    subscription = json.dumps({
        "APIKey": api_key,
        "BoundingBoxes": BBOXES,
        "FilterMessageTypes": [
            "PositionReport",
            "ShipStaticData",
            "StandardClassBPositionReport",
            "ExtendedClassBPositionReport",
        ],
        "FilterShipTypes": ENERGY_SHIP_TYPES,
    })

    print(f"Connecting to aisstream.io (Persian Gulf region, {seconds}s)…",
          file=sys.stderr)
    async with websockets.connect(
        "wss://stream.aisstream.io/v0/stream",
        ping_interval=20,
        ping_timeout=30,
        close_timeout=10,
    ) as ws:
        await ws.send(subscription)
        print("Connected — collecting vessel data…", file=sys.stderr)

        deadline = asyncio.get_event_loop().time() + seconds
        count = 0

        while asyncio.get_event_loop().time() < deadline:
            remaining = deadline - asyncio.get_event_loop().time()
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=min(remaining, 10))
            except asyncio.TimeoutError:
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            mt = msg.get("MessageType", "")
            if mt == "ShipStaticData":
                _absorb_static(msg, vessel_meta)
            elif mt in ("PositionReport", "StandardClassBPositionReport",
                        "ExtendedClassBPositionReport"):
                _absorb_position(msg, mt, vessel_pos)
            count += 1
            if count % 1000 == 0:
                print(f"  {count} msgs, {len(vessel_pos)} positions…",
                      file=sys.stderr)

    print(f"Done — {count} msgs, {len(vessel_pos)} positions, "
          f"{len(vessel_meta)} static records", file=sys.stderr)
    return vessel_meta, vessel_pos


def _absorb_static(msg: dict, meta: dict) -> None:
    data  = msg.get("Message", {}).get("ShipStaticData", {})
    mmsi  = str(data.get("UserId") or data.get("Mmsi") or "")
    if not mmsi:
        return
    stype = int(data.get("Type") or 0)
    if stype not in ENERGY_SHIP_TYPES:
        return
    dim = data.get("Dimension") or {}
    meta[mmsi] = {
        "ship_name":   (data.get("Name") or "").strip(),
        "ship_type":   stype,
        "destination": (data.get("Destination") or "").strip(),
        "imo":         str(data.get("ImoNumber") or ""),
        "draught":     float(data.get("MaximumStaticDraught") or 0),
        "length":      float(dim.get("A") or 0) + float(dim.get("B") or 0),
        "width":       float(dim.get("C") or 0) + float(dim.get("D") or 0),
    }


def _absorb_position(msg: dict, mt: str, pos: dict) -> None:
    data = msg.get("Message", {}).get(mt, {})
    mmsi = str(data.get("UserId") or data.get("Mmsi") or "")
    if not mmsi:
        return
    lat = float(data.get("Latitude") or 0)
    lon = float(data.get("Longitude") or 0)
    if lat == 0.0 and lon == 0.0:
        return
    spd = float(data.get("Sog") or data.get("SpeedOverGround") or 0)
    if spd >= 40:          # AIS anomaly (102.3 = unavailable, 40+ = suspicious)
        return
    pos[mmsi] = {
        "lat":    round(lat, 5),
        "lon":    round(lon, 5),
        "speed":  spd,
        "course": float(data.get("Cog") or data.get("CourseOverGround") or 0),
    }


# ── Build vessel records ──────────────────────────────────────────────────────

def _build_records(
    vessel_meta: dict,
    vessel_pos:  dict,
) -> tuple[list[dict], list[dict], list[dict]]:
    """
    Returns (india_bound, transiting, in_gulf) lists.

    india_bound  — destination contains an Indian port
    transiting   — crossing or past the Hormuz gate (ocean_side), energy tanker
    in_gulf      — inside the Persian Gulf, energy tanker
    """
    india_bound: list[dict] = []
    transiting:  list[dict] = []
    in_gulf:     list[dict] = []

    for mmsi, pos in vessel_pos.items():
        meta    = vessel_meta.get(mmsi, {})
        lat     = pos["lat"]
        lon     = pos["lon"]
        stype   = meta.get("ship_type", 0)
        raw_dest = meta.get("destination", "")
        cargo   = _CARGO_MAP.get(stype, "OTHER")

        if cargo == "OTHER":
            continue   # only CRUDE, LNG, CNG, PETROLEUM

        flag_code, flag_country = _mmsi_to_flag(mmsi)
        zone    = _sea_zone(lat, lon)
        gate_side = _side_of_hormuz_gate(lat, lon)
        gate_nm   = _nm_from_gate(lat, lon)
        india_dest = _normalize_dest_india(raw_dest)

        record = {
            "mmsi":             mmsi,
            "ship_name":        meta.get("ship_name") or "Unknown",
            "cargo_category":   cargo,
            "destination_raw":  raw_dest,
            "destination_india": india_dest,        # None if not India-bound
            "flag_code":        flag_code,
            "flag_country":     flag_country,
            "lat":              lat,
            "lon":              lon,
            "speed":            pos["speed"],
            "course":           pos["course"],
            "sea_zone":         zone,
            "gate_side":        gate_side,           # "gulf_side" | "ocean_side"
            "nm_from_gate":     gate_nm,
            "length_m":         meta.get("length", 0),
            "draught_m":        meta.get("draught", 0),
        }

        if india_dest:
            india_bound.append(record)
        elif gate_side == "ocean_side" or zone in ("Gulf of Oman", "Arabian Sea",
                                                    "Indian Ocean", "Bay of Bengal"):
            transiting.append(record)
        else:
            in_gulf.append(record)

    # Sort india_bound: closest to gate first, then by distance
    india_bound.sort(key=lambda r: r["nm_from_gate"])
    transiting.sort(key=lambda r: r["nm_from_gate"])
    in_gulf.sort(key=lambda r: r["nm_from_gate"])

    return india_bound, transiting, in_gulf


# ── Snapshot builder ──────────────────────────────────────────────────────────

def _build_snapshot(
    india_bound: list[dict],
    transiting:  list[dict],
    in_gulf:     list[dict],
    seconds:     int,
) -> dict:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _counts(lst: list[dict]) -> dict:
        c = {"CRUDE": 0, "LNG": 0, "CNG": 0, "PETROLEUM": 0}
        for r in lst:
            cat = r.get("cargo_category", "OTHER")
            if cat in c:
                c[cat] += 1
        return c

    # Origin inference for India-bound: flag country + sea zone
    origins: dict[str, int] = {}
    for r in india_bound + transiting:
        country = r.get("flag_country", "") or r.get("sea_zone", "Unknown")
        origins[country] = origins.get(country, 0) + 1

    # Destination port distribution for India-bound
    dest_ports: dict[str, int] = {}
    for r in india_bound:
        p = r.get("destination_india") or "India"
        dest_ports[p] = dest_ports.get(p, 0) + 1

    return {
        "generated_at":  now,
        "stream_seconds": seconds,
        "totals": {
            "india_bound": len(india_bound),
            "transiting":  len(transiting),
            "in_gulf":     len(in_gulf),
            "india_bound_counts": _counts(india_bound),
            "transiting_counts":  _counts(transiting),
        },
        "destination_ports": dest_ports,
        "flag_origins":      origins,
        "india_bound":  india_bound,
        "transiting":   transiting[:30],   # cap to keep JSON manageable
        "in_gulf":      in_gulf[:20],
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Stream AIS data from the Persian Gulf / Hormuz region "
                    "and snapshot India-bound energy tankers."
    )
    parser.add_argument("--seconds", type=int,
                        default=int(os.environ.get("AIS_STREAM_SECONDS", STREAM_SECONDS)),
                        help=f"Seconds to stream (default {STREAM_SECONDS})")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT),
                        help=f"Output JSON path (default {DEFAULT_OUTPUT})")
    args = parser.parse_args()

    api_key = os.environ.get("AISSTREAM_API_KEY", "").strip()
    if not api_key:
        print("Error: AISSTREAM_API_KEY not set.", file=sys.stderr)
        sys.exit(1)

    vessel_meta, vessel_pos = asyncio.run(stream_hormuz(api_key, args.seconds))
    india_bound, transiting, in_gulf = _build_records(vessel_meta, vessel_pos)

    snap = _build_snapshot(india_bound, transiting, in_gulf, args.seconds)

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(snap, indent=2), encoding="utf-8")

    print(f"India-bound:  {snap['totals']['india_bound']}  "
          f"(Crude {snap['totals']['india_bound_counts']['CRUDE']}, "
          f"LNG {snap['totals']['india_bound_counts']['LNG']}, "
          f"CNG {snap['totals']['india_bound_counts']['CNG']})")
    print(f"Transiting:   {snap['totals']['transiting']}")
    print(f"In Gulf:      {snap['totals']['in_gulf']}")
    print(f"Saved: {out}")


if __name__ == "__main__":
    main()

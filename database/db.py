"""
SQLite database layer.
All public functions are async; synchronous SQLite calls are wrapped in
asyncio.to_thread() so they never block the event loop.
"""
import asyncio
import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

from config import DATABASE_PATH, POSITION_RETENTION_HOURS

log = logging.getLogger(__name__)

_conn: sqlite3.Connection | None = None


# ---------------------------------------------------------------------------
# Connection management
# ---------------------------------------------------------------------------

def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        raise RuntimeError("Database not initialised — call init_db() first")
    return _conn


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------

def _init_sync() -> None:
    global _conn
    _conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("PRAGMA foreign_keys=ON")
    _conn.execute("PRAGMA synchronous=NORMAL")

    _conn.executescript("""
    CREATE TABLE IF NOT EXISTS vessels (
        mmsi            TEXT PRIMARY KEY,
        imo             TEXT DEFAULT '',
        ship_name       TEXT DEFAULT '',
        ship_type       INTEGER DEFAULT 0,
        cargo_category  TEXT DEFAULT 'OTHER',
        flag            TEXT DEFAULT '',
        length          REAL DEFAULT 0,
        width           REAL DEFAULT 0,
        draft           REAL DEFAULT 0,
        deadweight      REAL DEFAULT 0,
        first_seen      TEXT,
        last_seen       TEXT,
        updated_at      TEXT
    );

    CREATE TABLE IF NOT EXISTS positions (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        mmsi        TEXT NOT NULL,
        latitude    REAL NOT NULL,
        longitude   REAL NOT NULL,
        speed       REAL DEFAULT 0,
        course      REAL DEFAULT 0,
        heading     REAL DEFAULT 0,
        nav_status  INTEGER DEFAULT 15,
        timestamp   TEXT NOT NULL,
        source      TEXT DEFAULT 'AIS'
    );
    CREATE INDEX IF NOT EXISTS idx_pos_mmsi_ts ON positions(mmsi, timestamp DESC);

    CREATE TABLE IF NOT EXISTS inbound_ships (
        mmsi                TEXT PRIMARY KEY,
        destination         TEXT DEFAULT '',
        eta                 TEXT DEFAULT '',
        origin_port         TEXT DEFAULT '',
        origin_country      TEXT DEFAULT '',
        cargo_category      TEXT DEFAULT 'OTHER',
        ship_name           TEXT DEFAULT '',
        current_lat         REAL DEFAULT 0,
        current_lon         REAL DEFAULT 0,
        speed               REAL DEFAULT 0,
        course              REAL DEFAULT 0,
        distance_to_port    REAL DEFAULT 0,
        nearest_port        TEXT DEFAULT '',
        status              TEXT DEFAULT 'UNDERWAY',
        first_detected      TEXT,
        last_updated        TEXT
    );

    CREATE TABLE IF NOT EXISTS outbound_ships (
        mmsi                TEXT PRIMARY KEY,
        departure_port      TEXT DEFAULT '',
        departure_time      TEXT DEFAULT '',
        destination         TEXT DEFAULT '',
        cargo_category      TEXT DEFAULT 'OTHER',
        ship_name           TEXT DEFAULT '',
        current_lat         REAL DEFAULT 0,
        current_lon         REAL DEFAULT 0,
        speed               REAL DEFAULT 0,
        course              REAL DEFAULT 0,
        distance_from_port  REAL DEFAULT 0,
        nav_status          INTEGER DEFAULT 0,
        ballast_confirmed   INTEGER DEFAULT 0,
        first_detected      TEXT,
        last_updated        TEXT
    );

    CREATE TABLE IF NOT EXISTS port_activity (
        mmsi                TEXT PRIMARY KEY,
        port_name           TEXT NOT NULL,
        berth               TEXT DEFAULT '',
        activity            TEXT DEFAULT 'UNKNOWN',
        cargo_category      TEXT DEFAULT 'OTHER',
        ship_name           TEXT DEFAULT '',
        arrival_time        TEXT DEFAULT '',
        expected_departure  TEXT DEFAULT '',
        current_lat         REAL DEFAULT 0,
        current_lon         REAL DEFAULT 0,
        source              TEXT DEFAULT 'AIS',
        last_updated        TEXT
    );

    CREATE TABLE IF NOT EXISTS aggregated_stats (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        computed_at     TEXT NOT NULL,
        total_inbound   INTEGER DEFAULT 0,
        total_outbound  INTEGER DEFAULT 0,
        total_in_port   INTEGER DEFAULT 0,
        crude_count     INTEGER DEFAULT 0,
        lng_count       INTEGER DEFAULT 0,
        cng_count       INTEGER DEFAULT 0,
        petroleum_count INTEGER DEFAULT 0,
        busiest_port    TEXT DEFAULT '',
        stats_json      TEXT DEFAULT '{}'
    );

    CREATE TABLE IF NOT EXISTS scraper_logs (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        scraper     TEXT NOT NULL,
        run_at      TEXT NOT NULL,
        status      TEXT DEFAULT 'OK',
        records     INTEGER DEFAULT 0,
        error_msg   TEXT DEFAULT ''
    );
    """)
    _conn.commit()
    log.info("Database initialised at %s", DATABASE_PATH)


async def init_db() -> None:
    await asyncio.to_thread(_init_sync)


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def _row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


def _exec(sql: str, params: tuple = ()) -> None:
    conn = _get_conn()
    conn.execute(sql, params)
    conn.commit()


def _fetchall(sql: str, params: tuple = ()) -> list[dict]:
    conn = _get_conn()
    cur = conn.execute(sql, params)
    return [_row_to_dict(r) for r in cur.fetchall()]


def _fetchone(sql: str, params: tuple = ()) -> dict | None:
    conn = _get_conn()
    cur = conn.execute(sql, params)
    row = cur.fetchone()
    return _row_to_dict(row) if row else None


# ---------------------------------------------------------------------------
# Vessel registry
# ---------------------------------------------------------------------------

def _upsert_vessel_sync(v: dict) -> None:
    now = _now_iso()
    _get_conn().execute("""
        INSERT INTO vessels (mmsi, imo, ship_name, ship_type, cargo_category,
                             flag, length, width, draft, deadweight,
                             first_seen, last_seen, updated_at)
        VALUES (:mmsi,:imo,:ship_name,:ship_type,:cargo_category,
                :flag,:length,:width,:draft,:deadweight,
                :now,:now,:now)
        ON CONFLICT(mmsi) DO UPDATE SET
            imo            = COALESCE(NULLIF(excluded.imo,''),       imo),
            ship_name      = COALESCE(NULLIF(excluded.ship_name,''), ship_name),
            ship_type      = CASE WHEN excluded.ship_type != 0 THEN excluded.ship_type ELSE ship_type END,
            cargo_category = CASE WHEN excluded.cargo_category != 'OTHER' THEN excluded.cargo_category ELSE cargo_category END,
            flag           = COALESCE(NULLIF(excluded.flag,''),      flag),
            length         = CASE WHEN excluded.length > 0 THEN excluded.length ELSE length END,
            width          = CASE WHEN excluded.width  > 0 THEN excluded.width  ELSE width  END,
            draft          = CASE WHEN excluded.draft  > 0 THEN excluded.draft  ELSE draft  END,
            last_seen      = :now,
            updated_at     = :now
    """, {**v, "now": now})
    _get_conn().commit()


async def upsert_vessel(v: dict) -> None:
    await asyncio.to_thread(_upsert_vessel_sync, v)


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------

def _upsert_position_sync(p: dict) -> None:
    _get_conn().execute("""
        INSERT INTO positions (mmsi, latitude, longitude, speed, course,
                               heading, nav_status, timestamp, source)
        VALUES (:mmsi,:latitude,:longitude,:speed,:course,
                :heading,:nav_status,:timestamp,:source)
    """, p)
    _get_conn().commit()


async def upsert_position(p: dict) -> None:
    await asyncio.to_thread(_upsert_position_sync, p)


async def purge_old_positions() -> None:
    def _purge():
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) -
                  timedelta(hours=POSITION_RETENTION_HOURS)).isoformat()
        _get_conn().execute("DELETE FROM positions WHERE timestamp < ?", (cutoff,))
        _get_conn().commit()
    await asyncio.to_thread(_purge)


async def get_vessel_positions(mmsi: str, limit: int = 50) -> list[dict]:
    def _q():
        return _fetchall(
            "SELECT * FROM positions WHERE mmsi=? ORDER BY timestamp DESC LIMIT ?",
            (mmsi, limit)
        )
    return await asyncio.to_thread(_q)


# ---------------------------------------------------------------------------
# Inbound ships
# ---------------------------------------------------------------------------

def _upsert_inbound_sync(s: dict) -> None:
    now = _now_iso()
    _get_conn().execute("""
        INSERT INTO inbound_ships
            (mmsi, destination, eta, origin_port, origin_country,
             cargo_category, ship_name, current_lat, current_lon,
             speed, course, distance_to_port, nearest_port, status,
             first_detected, last_updated)
        VALUES
            (:mmsi,:destination,:eta,:origin_port,:origin_country,
             :cargo_category,:ship_name,:current_lat,:current_lon,
             :speed,:course,:distance_to_port,:nearest_port,:status,
             :now,:now)
        ON CONFLICT(mmsi) DO UPDATE SET
            destination     = excluded.destination,
            eta             = excluded.eta,
            cargo_category  = excluded.cargo_category,
            ship_name       = COALESCE(NULLIF(excluded.ship_name,''), ship_name),
            current_lat     = excluded.current_lat,
            current_lon     = excluded.current_lon,
            speed           = excluded.speed,
            course          = excluded.course,
            distance_to_port= excluded.distance_to_port,
            nearest_port    = excluded.nearest_port,
            status          = excluded.status,
            last_updated    = :now
    """, {**s, "now": now})
    _get_conn().commit()


async def upsert_inbound(s: dict) -> None:
    await asyncio.to_thread(_upsert_inbound_sync, s)


async def remove_inbound(mmsi: str) -> None:
    await asyncio.to_thread(
        lambda: (_get_conn().execute("DELETE FROM inbound_ships WHERE mmsi=?", (mmsi,)),
                 _get_conn().commit())
    )


async def get_inbound_ships(cargo_type: str | None = None,
                             limit: int = 200) -> list[dict]:
    def _q():
        if cargo_type and cargo_type != "ALL":
            return _fetchall(
                "SELECT i.*, v.ship_name as vname FROM inbound_ships i "
                "LEFT JOIN vessels v ON i.mmsi=v.mmsi "
                "WHERE i.cargo_category=? ORDER BY i.distance_to_port LIMIT ?",
                (cargo_type.upper(), limit)
            )
        return _fetchall(
            "SELECT i.*, v.ship_name as vname FROM inbound_ships i "
            "LEFT JOIN vessels v ON i.mmsi=v.mmsi "
            "ORDER BY i.distance_to_port LIMIT ?",
            (limit,)
        )
    return await asyncio.to_thread(_q)


# ---------------------------------------------------------------------------
# Outbound ships
# ---------------------------------------------------------------------------

def _upsert_outbound_sync(s: dict) -> None:
    now = _now_iso()
    _get_conn().execute("""
        INSERT INTO outbound_ships
            (mmsi, departure_port, departure_time, destination,
             cargo_category, ship_name, current_lat, current_lon,
             speed, course, distance_from_port, nav_status,
             ballast_confirmed, first_detected, last_updated)
        VALUES
            (:mmsi,:departure_port,:departure_time,:destination,
             :cargo_category,:ship_name,:current_lat,:current_lon,
             :speed,:course,:distance_from_port,:nav_status,
             :ballast_confirmed,:now,:now)
        ON CONFLICT(mmsi) DO UPDATE SET
            destination       = COALESCE(NULLIF(excluded.destination,''), destination),
            cargo_category    = excluded.cargo_category,
            ship_name         = COALESCE(NULLIF(excluded.ship_name,''), ship_name),
            current_lat       = excluded.current_lat,
            current_lon       = excluded.current_lon,
            speed             = excluded.speed,
            course            = excluded.course,
            distance_from_port= excluded.distance_from_port,
            nav_status        = excluded.nav_status,
            ballast_confirmed = MAX(excluded.ballast_confirmed, ballast_confirmed),
            last_updated      = :now
    """, {**s, "now": now})
    _get_conn().commit()


async def upsert_outbound(s: dict) -> None:
    await asyncio.to_thread(_upsert_outbound_sync, s)


async def remove_outbound(mmsi: str) -> None:
    await asyncio.to_thread(
        lambda: (_get_conn().execute("DELETE FROM outbound_ships WHERE mmsi=?", (mmsi,)),
                 _get_conn().commit())
    )


async def get_outbound_ships(cargo_type: str | None = None,
                              limit: int = 200) -> list[dict]:
    def _q():
        if cargo_type and cargo_type != "ALL":
            return _fetchall(
                "SELECT * FROM outbound_ships WHERE cargo_category=? "
                "ORDER BY last_updated DESC LIMIT ?",
                (cargo_type.upper(), limit)
            )
        return _fetchall(
            "SELECT * FROM outbound_ships ORDER BY last_updated DESC LIMIT ?",
            (limit,)
        )
    return await asyncio.to_thread(_q)


# ---------------------------------------------------------------------------
# Port activity
# ---------------------------------------------------------------------------

def _upsert_port_activity_sync(a: dict) -> None:
    now = _now_iso()
    _get_conn().execute("""
        INSERT INTO port_activity
            (mmsi, port_name, berth, activity, cargo_category, ship_name,
             arrival_time, expected_departure, current_lat, current_lon,
             source, last_updated)
        VALUES
            (:mmsi,:port_name,:berth,:activity,:cargo_category,:ship_name,
             :arrival_time,:expected_departure,:current_lat,:current_lon,
             :source,:now)
        ON CONFLICT(mmsi) DO UPDATE SET
            port_name          = excluded.port_name,
            berth              = COALESCE(NULLIF(excluded.berth,''), berth),
            activity           = CASE WHEN excluded.activity != 'UNKNOWN' THEN excluded.activity ELSE activity END,
            cargo_category     = excluded.cargo_category,
            ship_name          = COALESCE(NULLIF(excluded.ship_name,''), ship_name),
            expected_departure = COALESCE(NULLIF(excluded.expected_departure,''), expected_departure),
            current_lat        = excluded.current_lat,
            current_lon        = excluded.current_lon,
            source             = excluded.source,
            last_updated       = :now
    """, {**a, "now": now})
    _get_conn().commit()


async def upsert_port_activity(a: dict) -> None:
    await asyncio.to_thread(_upsert_port_activity_sync, a)


async def remove_port_activity(mmsi: str) -> None:
    await asyncio.to_thread(
        lambda: (_get_conn().execute("DELETE FROM port_activity WHERE mmsi=?", (mmsi,)),
                 _get_conn().commit())
    )


async def get_port_activity(port: str | None = None,
                             cargo_type: str | None = None,
                             limit: int = 200) -> list[dict]:
    def _q():
        clauses, params = [], []
        if port and port != "ALL":
            clauses.append("port_name=?"); params.append(port)
        if cargo_type and cargo_type != "ALL":
            clauses.append("cargo_category=?"); params.append(cargo_type.upper())
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        return _fetchall(
            f"SELECT * FROM port_activity {where} ORDER BY arrival_time DESC LIMIT ?",
            tuple(params)
        )
    return await asyncio.to_thread(_q)


# ---------------------------------------------------------------------------
# Aggregated stats
# ---------------------------------------------------------------------------

def _save_stats_sync(s: dict) -> None:
    _get_conn().execute("""
        INSERT INTO aggregated_stats
            (computed_at, total_inbound, total_outbound, total_in_port,
             crude_count, lng_count, cng_count, petroleum_count,
             busiest_port, stats_json)
        VALUES
            (:computed_at,:total_inbound,:total_outbound,:total_in_port,
             :crude_count,:lng_count,:cng_count,:petroleum_count,
             :busiest_port,:stats_json)
    """, s)
    # Keep only last 100 rows
    _get_conn().execute("""
        DELETE FROM aggregated_stats WHERE id NOT IN (
            SELECT id FROM aggregated_stats ORDER BY id DESC LIMIT 100
        )
    """)
    _get_conn().commit()


async def save_aggregated_stats(s: dict) -> None:
    await asyncio.to_thread(_save_stats_sync, s)


async def get_latest_stats() -> dict | None:
    def _q():
        return _fetchone(
            "SELECT * FROM aggregated_stats ORDER BY id DESC LIMIT 1"
        )
    return await asyncio.to_thread(_q)


# ---------------------------------------------------------------------------
# Scraper logs
# ---------------------------------------------------------------------------

async def log_scraper_run(scraper: str, status: str,
                           records: int, error: str = "") -> None:
    def _l():
        _get_conn().execute(
            "INSERT INTO scraper_logs (scraper,run_at,status,records,error_msg) "
            "VALUES (?,?,?,?,?)",
            (scraper, _now_iso(), status, records, error)
        )
        _get_conn().commit()
    await asyncio.to_thread(_l)


# ---------------------------------------------------------------------------
# Vessel detail (for API)
# ---------------------------------------------------------------------------

async def get_vessel(mmsi: str) -> dict | None:
    def _q():
        return _fetchone("SELECT * FROM vessels WHERE mmsi=?", (mmsi,))
    return await asyncio.to_thread(_q)


async def get_stale_port_ships(older_than_iso: str) -> list[dict]:
    def _q():
        return _fetchall(
            "SELECT mmsi FROM port_activity WHERE last_updated < ?",
            (older_than_iso,)
        )
    return await asyncio.to_thread(_q)


async def get_stale_outbound_ships(older_than_iso: str) -> list[dict]:
    def _q():
        return _fetchall(
            "SELECT mmsi FROM outbound_ships WHERE first_detected < ?",
            (older_than_iso,)
        )
    return await asyncio.to_thread(_q)

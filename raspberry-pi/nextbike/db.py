"""SQLite-Datenbank (WAL-Modus: Sammler schreibt, Web-GUI liest gleichzeitig).

Ohne Cloud-Limits wird hier alles normalisiert gespeichert, direkt analysierbar mit SQL/pandas:
  obs             alle Stationen alle 5 Min (Prognose für jede Station)
  station_change  jede Bestandsänderung jeder Station (Abruf-Auflösung, z. B. 1 Min)
  bike_event      jede Bewegung jedes einzelnen Velos
  trip            erkannte Fahrten (Ausleihe → Rückgabe), inkl. Umverteilungs-Hinweis
  stations/bikes  Stammdaten (Namen, Koordinaten, Velotypen)
  poll            Protokoll jedes Abrufs (Erfolg, Dauer, Fehler)
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .feed import ParsedFeed
from .predict import SlotStat
from .timeutil import week_slot
from .tracker import Step, Tracker, haversine_m, loc_coords

SCHEMA = """
CREATE TABLE IF NOT EXISTS stations (
  uid INTEGER PRIMARY KEY, number INTEGER, name TEXT NOT NULL, lat REAL, lng REAL, place_type TEXT,
  first_seen INTEGER NOT NULL, last_seen INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS bikes (
  number TEXT PRIMARY KEY, type TEXT, first_seen INTEGER NOT NULL, last_seen INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS obs (
  uid INTEGER NOT NULL, wslot INTEGER NOT NULL, ts INTEGER NOT NULL,
  bikes INTEGER NOT NULL, ebikes INTEGER NOT NULL, booked INTEGER NOT NULL,
  PRIMARY KEY (uid, wslot, ts)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS obs_ts ON obs (ts);
CREATE TABLE IF NOT EXISTS station_change (
  ts INTEGER NOT NULL, uid INTEGER NOT NULL,
  bikes INTEGER, ebikes INTEGER, booked INTEGER, active INTEGER,  -- NULL = Station aus dem Feed verschwunden
  PRIMARY KEY (uid, ts)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS station_change_ts ON station_change (ts);
CREATE TABLE IF NOT EXISTS bike_event (
  ts INTEGER NOT NULL, prev_ts INTEGER, bike TEXT NOT NULL,
  from_uid INTEGER, from_lat REAL, from_lng REAL,   -- alle NULL = war nicht sichtbar
  to_uid INTEGER, to_lat REAL, to_lng REAL,         -- alle NULL = ist jetzt nicht sichtbar (ausgeliehen)
  PRIMARY KEY (bike, ts)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS bike_event_ts ON bike_event (ts);
CREATE TABLE IF NOT EXISTS trip (
  id INTEGER PRIMARY KEY, bike TEXT NOT NULL,
  start_ts INTEGER NOT NULL, start_uid INTEGER, start_lat REAL, start_lng REAL,
  end_ts INTEGER NOT NULL, end_uid INTEGER, end_lat REAL, end_lng REAL,
  duration_s INTEGER NOT NULL, distance_m INTEGER,
  direct INTEGER NOT NULL,              -- 1 = innerhalb eines Abrufintervalls direkt A → B
  arrivals_same_poll INTEGER NOT NULL   -- Anzahl Velos, die im selben Abruf an derselben Station ankamen
);
CREATE INDEX IF NOT EXISTS trip_end ON trip (end_ts);
CREATE INDEX IF NOT EXISTS trip_start ON trip (start_ts);
CREATE INDEX IF NOT EXISTS trip_bike ON trip (bike, end_ts);
CREATE TABLE IF NOT EXISTS poll (
  ts INTEGER PRIMARY KEY, ok INTEGER NOT NULL, ms INTEGER, n_moves INTEGER, n_trips INTEGER, error TEXT
);
CREATE TABLE IF NOT EXISTS state (k INTEGER PRIMARY KEY CHECK (k = 1), data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""

EXPORT_TABLES = {
    "stations": "SELECT * FROM stations ORDER BY uid",
    "bikes": "SELECT * FROM bikes ORDER BY number",
    "obs": "SELECT * FROM obs WHERE ts >= ? ORDER BY ts, uid",
    "station_change": "SELECT * FROM station_change WHERE ts >= ? ORDER BY ts, uid",
    "bike_event": "SELECT * FROM bike_event WHERE ts >= ? ORDER BY ts, bike",
    "trip": "SELECT * FROM trip WHERE end_ts >= ? ORDER BY end_ts, id",
    "poll": "SELECT * FROM poll WHERE ts >= ? ORDER BY ts",
}


def connect(path: Path | str, readonly: bool = False) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")  # schont die SD-Karte, sicher mit WAL
    conn.execute("PRAGMA busy_timeout=30000")
    if readonly:
        conn.execute("PRAGMA query_only=ON")
    else:
        conn.executescript(SCHEMA)
    return conn


# ------------------------------------------------------------------ Zustand / Meta

def load_tracker(conn: sqlite3.Connection) -> tuple[Tracker, dict]:
    row = conn.execute("SELECT data FROM state WHERE k = 1").fetchone()
    if not row:
        return Tracker(), {}
    try:
        d = json.loads(row["data"])
    except ValueError:
        return Tracker(), {}
    return Tracker.from_json(d.get("tracker")), d.get("extra", {})


def save_state(conn: sqlite3.Connection, tracker: Tracker, extra: dict) -> None:
    conn.execute(
        "INSERT INTO state (k, data) VALUES (1, ?) ON CONFLICT(k) DO UPDATE SET data = excluded.data",
        (json.dumps({"tracker": tracker.to_json(), "extra": extra}, separators=(",", ":")),),
    )


def set_meta(conn: sqlite3.Connection, k: str, v: str) -> None:
    conn.execute("INSERT INTO meta (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v", (k, v))


def get_meta(conn: sqlite3.Connection, k: str) -> str | None:
    r = conn.execute("SELECT v FROM meta WHERE k = ?", (k,)).fetchone()
    return r["v"] if r else None


# ------------------------------------------------------------------ Schreiben

def station_coords(conn: sqlite3.Connection) -> dict[int, tuple[float, float]]:
    return {r["uid"]: (r["lat"], r["lng"]) for r in conn.execute("SELECT uid, lat, lng FROM stations WHERE lat IS NOT NULL")}


def write_step(conn: sqlite3.Connection, step: Step, feed: ParsedFeed, coords: dict[int, tuple[float, float]]) -> None:
    ts = step.ts
    conn.executemany(
        "INSERT OR REPLACE INTO station_change (ts, uid, bikes, ebikes, booked, active) VALUES (?, ?, ?, ?, ?, ?)",
        [(ts, uid, *(t if t else (None, None, None, None))) for uid, t in step.station_changes],
    )
    conn.executemany(
        "INSERT OR REPLACE INTO bike_event VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(ts, step.prev_ts, n, *loc_coords(a, coords), *loc_coords(b, coords)) for n, a, b in step.moves],
    )
    rows = []
    for t in step.trips:
        s, e = loc_coords(t.start, coords), loc_coords(t.end, coords)
        rows.append((t.bike, t.start_ts, *s, t.end_ts, *e, t.end_ts - t.start_ts, haversine_m(s[1:], e[1:]), int(t.direct), t.arrivals_same_poll))
    conn.executemany(
        """INSERT INTO trip (bike, start_ts, start_uid, start_lat, start_lng, end_ts, end_uid, end_lat, end_lng,
           duration_s, distance_m, direct, arrivals_same_poll) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )


def write_obs(conn: sqlite3.Connection, ts5: int, feed: ParsedFeed) -> int:
    ws = week_slot(ts5)
    rows = [(s.uid, ws, ts5, s.bikes, s.ebikes, s.booked) for s in feed.stations if s.active]
    conn.executemany("INSERT OR IGNORE INTO obs VALUES (?, ?, ?, ?, ?, ?)", rows)
    return len(rows)


def upsert_master(conn: sqlite3.Connection, ts: int, feed: ParsedFeed) -> None:
    conn.executemany(
        """INSERT INTO stations VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(uid) DO UPDATE SET number=excluded.number, name=excluded.name, lat=excluded.lat,
             lng=excluded.lng, place_type=excluded.place_type, last_seen=excluded.last_seen""",
        [(s.uid, s.number, s.name, s.lat, s.lng, s.place_type, ts, ts) for s in feed.stations],
    )
    conn.executemany(
        """INSERT INTO bikes VALUES (?, ?, ?, ?)
           ON CONFLICT(number) DO UPDATE SET type = coalesce(excluded.type, type), last_seen = excluded.last_seen""",
        [(n, feed.bike_types.get(n), ts, ts) for n in feed.bikes],
    )


def log_poll(conn: sqlite3.Connection, ts: int, ok: bool, ms: int, n_moves: int = 0, n_trips: int = 0, error: str | None = None) -> None:
    conn.execute("INSERT OR REPLACE INTO poll VALUES (?, ?, ?, ?, ?, ?)", (ts, int(ok), ms, n_moves, n_trips, error))


# ------------------------------------------------------------------ Lesen

def load_stats(conn: sqlite3.Connection, uids: list[int], wslots: list[int]) -> dict[int, dict[int, SlotStat]]:
    out: dict[int, dict[int, SlotStat]] = {}
    if not uids or not wslots:
        return out
    q = f"""SELECT uid, wslot, count(*) n, sum(bikes > 0) a, sum(bikes) b, sum(ebikes > 0) ea, sum(ebikes) e
            FROM obs WHERE uid IN ({",".join("?" * len(uids))}) AND wslot IN ({",".join("?" * len(set(wslots)))})
            GROUP BY uid, wslot"""
    for r in conn.execute(q, [*uids, *sorted(set(wslots))]):
        out.setdefault(r["uid"], {})[r["wslot"]] = SlotStat(r["n"], r["a"], r["b"], r["ea"], r["e"])
    return out


def db_size_bytes(path: Path) -> int:
    total = 0
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(path) + suffix)
        if p.exists():
            total += p.stat().st_size
    return total

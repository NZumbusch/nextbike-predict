"""Arbeits-Datenbank (SQLite, WAL). Enthält die letzten `hot_days` Tage plus alle offenen Zustände.
Das dauerhafte Analyse-Archiv sind die täglichen Parquet-Dateien (archive.py)."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .model import Snapshot
from .tracker import P_COLS, S_COLS, T_COLS, V_COLS, Changes

SCHEMA = """
CREATE TABLE IF NOT EXISTS vehicle_spell (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL,
  provider TEXT NOT NULL,
  vehicle_id TEXT NOT NULL,      -- Tracking-Schlüssel: Fahrzeug-ID oder "@lat,lon,typ#n" (Positions-Identität)
  raw_id TEXT,                   -- Original-ID, falls abweichend vom Schlüssel
  id_mode TEXT NOT NULL,         -- 'id' | 'pos'
  vehicle_type TEXT,
  lat REAL, lon REAL,
  station_id TEXT,
  reserved INTEGER NOT NULL,
  disabled INTEGER NOT NULL,
  range_m INTEGER,               -- Restreichweite zu Beginn
  start_ts INTEGER NOT NULL,     -- erste Beobachtung in diesem Zustand
  start_prev_ts INTEGER,         -- letzte Beobachtung davor (NULL = unbekannt, z. B. erster Abruf)
  start_reason TEXT NOT NULL,    -- initial | appeared (Ankunft) | moved | changed | rekey
  end_ts INTEGER,                -- letzte Beobachtung (NULL = Zustand dauert an)
  end_next_ts INTEGER,           -- erste Beobachtung danach
  end_reason TEXT                -- gone (Abfahrt) | moved | changed | rekey
);
CREATE INDEX IF NOT EXISTS vehicle_spell_open ON vehicle_spell (source) WHERE end_ts IS NULL;
CREATE INDEX IF NOT EXISTS vehicle_spell_end ON vehicle_spell (end_ts);

CREATE TABLE IF NOT EXISTS station_spell (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL, provider TEXT NOT NULL, station_id TEXT NOT NULL,
  bikes INTEGER NOT NULL, docks INTEGER,
  is_installed INTEGER NOT NULL, is_renting INTEGER NOT NULL, is_returning INTEGER NOT NULL,
  by_type TEXT,                  -- JSON {typ: anzahl}, falls verfügbar
  start_ts INTEGER NOT NULL, start_prev_ts INTEGER,
  end_ts INTEGER, end_next_ts INTEGER, end_reason TEXT
);
CREATE INDEX IF NOT EXISTS station_spell_open ON station_spell (source) WHERE end_ts IS NULL;
CREATE INDEX IF NOT EXISTS station_spell_end ON station_spell (end_ts);

CREATE TABLE IF NOT EXISTS trip (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL, provider TEXT NOT NULL, vehicle_id TEXT NOT NULL, vehicle_type TEXT,
  kind TEXT NOT NULL,            -- ride | round_trip | visible_move
  dep_ts INTEGER NOT NULL,       -- zuletzt am Start gesehen
  dep_next_ts INTEGER,           -- erstmals nicht mehr am Start  ⇒ Abfahrt in (dep_ts, dep_next_ts]
  arr_prev_ts INTEGER,           -- letzter Abruf vor der Ankunft
  arr_ts INTEGER NOT NULL,       -- erstmals am Ziel gesehen      ⇒ Ankunft in (arr_prev_ts, arr_ts]
  from_lat REAL, from_lon REAL, from_station TEXT,
  to_lat REAL, to_lon REAL, to_station TEXT,
  distance_m INTEGER,            -- Luftlinie
  from_spell INTEGER, to_spell INTEGER
);
CREATE INDEX IF NOT EXISTS trip_arr ON trip (arr_ts);

CREATE TABLE IF NOT EXISTS poll (
  source TEXT NOT NULL, ts INTEGER NOT NULL,
  ok INTEGER NOT NULL, stale INTEGER NOT NULL DEFAULT 0,
  ms INTEGER, bytes_in INTEGER, obs_ts INTEGER,
  n_vehicles INTEGER, n_stations INTEGER, v_changes INTEGER, s_changes INTEGER, trips INTEGER,
  endpoint TEXT, error TEXT,
  PRIMARY KEY (source, ts)
) WITHOUT ROWID;

-- Kennzahlen pro Anbieter in 5-Minuten-Fenstern (Datenqualität: ID-Wechsel, Aktivität)
CREATE TABLE IF NOT EXISTS provider_stat (
  ts INTEGER NOT NULL, source TEXT NOT NULL, provider TEXT NOT NULL, id_mode TEXT,
  n_vehicles INTEGER, n_new_ids INTEGER, n_gone_ids INTEGER, n_opened INTEGER, n_closed INTEGER, n_polls INTEGER,
  n_departed INTEGER DEFAULT 0,  -- Abfahrten (Fahrzeug verschwunden)
  n_returned INTEGER DEFAULT 0,  -- Wiederauftauchen mit gleicher ID ⇒ Fahrten exakt rekonstruierbar
  PRIMARY KEY (source, provider, ts)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS station_info (
  source TEXT NOT NULL, provider TEXT NOT NULL, station_id TEXT NOT NULL,
  name TEXT, lat REAL, lon REAL, capacity INTEGER, address TEXT,
  first_seen INTEGER NOT NULL, last_seen INTEGER NOT NULL,
  PRIMARY KEY (source, provider, station_id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS provider_info (
  source TEXT NOT NULL, provider TEXT NOT NULL, name TEXT, vehicle_type TEXT, data TEXT,
  first_seen INTEGER NOT NULL, last_seen INTEGER NOT NULL,
  PRIMARY KEY (source, provider)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS vehicle_type_info (
  source TEXT NOT NULL, vehicle_type_id TEXT NOT NULL, data TEXT,
  first_seen INTEGER NOT NULL, last_seen INTEGER NOT NULL,
  PRIMARY KEY (source, vehicle_type_id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS bike_info (
  source TEXT NOT NULL, provider TEXT NOT NULL, vehicle_id TEXT NOT NULL, vehicle_type TEXT,
  first_seen INTEGER NOT NULL, last_seen INTEGER NOT NULL,
  PRIMARY KEY (source, provider, vehicle_id)
) WITHOUT ROWID;
-- Datenlücken: Zeiträume ohne frische Daten einer Quelle (für die Analyse wichtig: "keine Daten" ≠ "keine Fahrten")
CREATE TABLE IF NOT EXISTS gap (
  source TEXT NOT NULL,
  start_ts INTEGER NOT NULL,     -- letzter frischer Abruf vor der Lücke
  end_ts INTEGER NOT NULL,       -- erster frischer Abruf danach
  duration_s INTEGER NOT NULL,
  reason TEXT NOT NULL,          -- collector_down | fetch_error | stale_feed
  n_failed INTEGER NOT NULL, n_stale INTEGER NOT NULL,
  PRIMARY KEY (source, start_ts)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""


def connect(path: Path | str, readonly: bool = False) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=60, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")  # sicher mit WAL, schont SD-Karte/SSD
    conn.execute("PRAGMA busy_timeout=60000")
    conn.execute("PRAGMA temp_store=MEMORY")
    if readonly:
        conn.execute("PRAGMA query_only=ON")
    else:
        conn.executescript(SCHEMA)
        migrate(conn)
    return conn


# Spalten, die nach der ersten Version dazukamen: (Tabelle, Spalte, Definition)
_ADDED_COLUMNS = [
    ("provider_stat", "n_departed", "INTEGER DEFAULT 0"),
    ("provider_stat", "n_returned", "INTEGER DEFAULT 0"),
]


def migrate(conn: sqlite3.Connection) -> None:
    """Fehlende Spalten in bestehenden Datenbanken ergänzen (Updates ohne Datenverlust)."""
    for table, col, decl in _ADDED_COLUMNS:
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.conn = connect(self.path)
        self._next = {
            "vehicle_spell": (self.conn.execute("SELECT coalesce(max(id), 0) FROM vehicle_spell").fetchone()[0]),
            "station_spell": (self.conn.execute("SELECT coalesce(max(id), 0) FROM station_spell").fetchone()[0]),
        }

    # IDs werden im Tracker vergeben (kein Roundtrip zur DB pro Zeile)
    def id_allocator(self, table: str):
        def nxt() -> int:
            self._next[table] += 1
            return self._next[table]

        return nxt

    # ------------------------------------------------------------ Meta
    def get_meta(self, k: str, default: str | None = None) -> str | None:
        r = self.conn.execute("SELECT v FROM meta WHERE k = ?", (k,)).fetchone()
        return r["v"] if r else default

    def set_meta(self, k: str, v: str) -> None:
        with self.conn:
            self.conn.execute("INSERT INTO meta VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v", (k, v))

    # ------------------------------------------------------------ Wiederaufnahme
    def restore_args(self, source: str, link_max_s: int) -> dict:
        c = self.conn
        last_v = c.execute("SELECT max(obs_ts) FROM poll WHERE source = ? AND ok = 1 AND stale = 0 AND n_vehicles IS NOT NULL", (source,)).fetchone()[0]
        last_s = c.execute("SELECT max(obs_ts) FROM poll WHERE source = ? AND ok = 1 AND stale = 0 AND n_stations IS NOT NULL", (source,)).fetchone()[0]
        modes = json.loads(self.get_meta(f"id_modes:{source}", "{}") or "{}")
        cut = (last_v or 0) - link_max_s
        return {
            "open_v_rows": c.execute("SELECT * FROM vehicle_spell WHERE source = ? AND end_ts IS NULL", (source,)),
            "open_s_rows": c.execute("SELECT * FROM station_spell WHERE source = ? AND end_ts IS NULL", (source,)),
            "recent_rows": c.execute(
                "SELECT * FROM vehicle_spell WHERE source = ? AND end_reason = 'gone' AND id_mode = 'id' AND end_ts >= ? ORDER BY end_ts",
                (source, cut)),
            "last_v_ts": last_v,
            "last_s_ts": last_s,
            "prov_modes": modes,
        }

    # ------------------------------------------------------------ Schreiben (1 Transaktion pro Abruf)
    def write(self, ch: Changes | None, snap: Snapshot | None, poll: dict, id_modes: dict[str, str] | None = None) -> None:
        c = self.conn
        with c:
            if ch is not None:
                c.executemany("UPDATE vehicle_spell SET end_ts = ?, end_next_ts = ?, end_reason = ? WHERE id = ?", ch.v_close)
                c.executemany(f"INSERT INTO vehicle_spell ({','.join(V_COLS)}) VALUES ({','.join('?' * len(V_COLS))})", ch.v_open)
                c.executemany("UPDATE station_spell SET end_ts = ?, end_next_ts = ?, end_reason = ? WHERE id = ?", ch.s_close)
                c.executemany(f"INSERT INTO station_spell ({','.join(S_COLS)}) VALUES ({','.join('?' * len(S_COLS))})", ch.s_open)
                c.executemany(f"INSERT INTO trip ({','.join(T_COLS)}) VALUES ({','.join('?' * len(T_COLS))})", ch.trips)
                bucket = ch.obs_ts // 300 * 300
                c.executemany(
                    f"""INSERT INTO provider_stat ({','.join(P_COLS)}, n_polls) VALUES ({','.join('?' * len(P_COLS))}, 1)
                        ON CONFLICT(source, provider, ts) DO UPDATE SET id_mode = excluded.id_mode, n_vehicles = excluded.n_vehicles,
                          n_new_ids = n_new_ids + excluded.n_new_ids, n_gone_ids = n_gone_ids + excluded.n_gone_ids,
                          n_opened = n_opened + excluded.n_opened, n_closed = n_closed + excluded.n_closed, n_polls = n_polls + 1,
                          n_departed = n_departed + excluded.n_departed, n_returned = n_returned + excluded.n_returned""",
                    [(bucket, *r[1:]) for r in ch.provider_stats],
                )
            if snap is not None:
                self._write_info(snap)
            if id_modes is not None:
                c.execute("INSERT INTO meta VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                          (f"id_modes:{poll['source']}", json.dumps(id_modes, sort_keys=True)))
            cols = list(poll)
            c.execute(f"INSERT OR REPLACE INTO poll ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", [poll[k] for k in cols])

    def _write_info(self, snap: Snapshot) -> None:
        c, ts, src = self.conn, snap.obs_ts, snap.source
        if snap.station_info:
            c.executemany(
                """INSERT INTO station_info VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(source, provider, station_id) DO UPDATE SET name = excluded.name, lat = excluded.lat,
                     lon = excluded.lon, capacity = excluded.capacity, address = excluded.address, last_seen = excluded.last_seen""",
                [(src, i.provider, i.station_id, i.name, i.lat, i.lon, i.capacity, i.address, ts, ts) for i in snap.station_info],
            )
        if snap.providers:
            c.executemany(
                """INSERT INTO provider_info VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(source, provider) DO UPDATE SET name = excluded.name, vehicle_type = excluded.vehicle_type,
                     data = excluded.data, last_seen = excluded.last_seen""",
                [(src, p.get("provider_id"), p.get("name"), p.get("vehicle_type"), json.dumps(p, ensure_ascii=False, sort_keys=True), ts, ts)
                 for p in snap.providers if p.get("provider_id")],
            )
        if snap.vehicle_types:
            c.executemany(
                """INSERT INTO vehicle_type_info VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(source, vehicle_type_id) DO UPDATE SET data = excluded.data, last_seen = excluded.last_seen""",
                [(src, v.get("vehicle_type_id"), json.dumps(v, ensure_ascii=False, sort_keys=True), ts, ts)
                 for v in snap.vehicle_types if v.get("vehicle_type_id")],
            )
        if snap.bike_types:
            c.executemany(
                """INSERT INTO bike_info VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(source, provider, vehicle_id) DO UPDATE SET vehicle_type = coalesce(excluded.vehicle_type, vehicle_type),
                     last_seen = excluded.last_seen""",
                [(src, p, v, t, ts, ts) for (p, v), t in snap.bike_types.items()],
            )

    def last_fresh_poll(self, source: str) -> int | None:
        return self.conn.execute("SELECT max(ts) FROM poll WHERE source = ? AND ok = 1 AND stale = 0", (source,)).fetchone()[0]

    def record_gap(self, source: str, start_ts: int, end_ts: int) -> dict:
        """Lücke zwischen zwei frischen Abrufen einordnen und speichern."""
        c = self.conn
        failed, stale = c.execute(
            "SELECT coalesce(sum(ok = 0), 0), coalesce(sum(ok = 1 AND stale = 1), 0) FROM poll WHERE source = ? AND ts > ? AND ts < ?",
            (source, start_ts, end_ts)).fetchone()
        reason = "collector_down" if failed + stale == 0 else ("fetch_error" if failed >= stale else "stale_feed")
        row = {"source": source, "start_ts": start_ts, "end_ts": end_ts, "duration_s": end_ts - start_ts,
               "reason": reason, "n_failed": failed, "n_stale": stale}
        with c:
            c.execute("INSERT OR REPLACE INTO gap VALUES (:source, :start_ts, :end_ts, :duration_s, :reason, :n_failed, :n_stale)", row)
        return row

    def size_bytes(self) -> int:
        return sum(p.stat().st_size for p in self.path.parent.glob(self.path.name + "*") if p.exists())

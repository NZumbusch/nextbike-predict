"""Dauerhafte Ablage für die Analyse.

archive/<tabelle>/date=YYYY-MM-DD/part-0.parquet   abgeschlossene Zeilen eines Tages (unveränderlich)
archive/open/<tabelle>.parquet                      aktuell offene Zustände (täglich überschrieben)
archive/dim/<tabelle>.parquet                       Stammdaten (Stationen, Anbieter, Typen), täglich überschrieben
raw/YYYY-MM-DD/HHMM_<quelle>_<feed>.json.zst        Rohantworten alle N Minuten
backups/sharedmob-YYYY-MM-DD.sqlite.zst             Kopie der Arbeits-DB

Partitioniert wird nach dem Zeitpunkt, an dem eine Zeile geschrieben/abgeschlossen wurde
(end_next_ts, arr_ts, ts). Diese Zeit steigt monoton ⇒ ein exportierter Tag ist endgültig.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.parquet as pq
import zstandard

TZ = ZoneInfo("Europe/Zurich")
log = logging.getLogger(__name__)

# Tabelle → Spalte, deren Tag die Partition bestimmt (nur abgeschlossene Zeilen)
DAILY = {
    "vehicle_spell": "end_next_ts",
    "station_spell": "end_next_ts",
    "trip": "arr_ts",
    "poll": "ts",
    "provider_stat": "ts",
}
OPEN = {"vehicle_spell": "end_ts IS NULL", "station_spell": "end_ts IS NULL"}
DIMS = ("station_info", "provider_info", "vehicle_type_info", "bike_info")
_SQL2ARROW = {"INTEGER": pa.int64(), "REAL": pa.float64(), "TEXT": pa.string()}


def day_bounds(d: date) -> tuple[int, int]:
    start = datetime(d.year, d.month, d.day, tzinfo=TZ)
    end = start + timedelta(days=1)
    # timedelta auf aware datetime ist Wanduhr-Arithmetik ⇒ DST-Tage haben korrekt 23/25 h
    return int(start.timestamp()), int(datetime(end.year, end.month, end.day, tzinfo=TZ).timestamp())


def local_date(ts: float) -> date:
    return datetime.fromtimestamp(ts, TZ).date()


def schema_of(conn: sqlite3.Connection, table: str) -> pa.Schema:
    cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return pa.schema([(c["name"], _SQL2ARROW.get(c["type"].upper(), pa.string())) for c in cols])


def _write_query(conn: sqlite3.Connection, table: str, sql: str, params: tuple, target: Path, batch: int = 50_000) -> int:
    """Streamt eine Abfrage in eine Parquet-Datei (speicherschonend), atomar per Umbenennen."""
    schema = schema_of(conn, table)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.tmp")
    cur = conn.execute(sql, params)
    n = 0
    with pq.ParquetWriter(tmp, schema, compression="zstd", compression_level=6) as w:
        while rows := cur.fetchmany(batch):
            cols = list(zip(*rows))
            w.write_table(pa.Table.from_arrays([pa.array(c, type=f.type) for c, f in zip(cols, schema)], schema=schema))
            n += len(rows)
        if n == 0:
            w.write_table(schema.empty_table())
    os.replace(tmp, target)
    return n


def export_day(conn: sqlite3.Connection, archive_dir: Path, d: date) -> dict[str, int]:
    a, b = day_bounds(d)
    counts = {}
    for table, col in DAILY.items():
        target = archive_dir / table / f"date={d.isoformat()}" / "part-0.parquet"
        counts[table] = _write_query(conn, table, f"SELECT * FROM {table} WHERE {col} >= ? AND {col} < ? ORDER BY {col}", (a, b), target)
    return counts


def export_snapshots(conn: sqlite3.Connection, archive_dir: Path) -> None:
    for table, where in OPEN.items():
        _write_query(conn, table, f"SELECT * FROM {table} WHERE {where}", (), archive_dir / "open" / f"{table}.parquet")
    for table in DIMS:
        _write_query(conn, table, f"SELECT * FROM {table}", (), archive_dir / "dim" / f"{table}.parquet")


def pending_days(conn: sqlite3.Connection, today: date) -> list[date]:
    """Tage vor heute, die noch nicht archiviert sind (ab dem ersten Tag mit Daten)."""
    r = conn.execute("SELECT v FROM meta WHERE k = 'archived_until'").fetchone()
    if r:
        start = date.fromisoformat(r["v"]) + timedelta(days=1)
    else:
        first = conn.execute("SELECT min(ts) FROM poll").fetchone()[0]
        if first is None:
            return []
        start = local_date(first)
    out = []
    d = start
    while d < today:
        out.append(d)
        d += timedelta(days=1)
    return out


def run_archive(db_path: Path, archive_dir: Path, hot_days: int, now: float | None = None) -> list[str]:
    """Exportiert alle fertigen Tage, schreibt Snapshots, räumt die Arbeits-DB auf. Idempotent."""
    now = time.time() if now is None else now
    conn = sqlite3.connect(str(db_path), timeout=120)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=120000")
    done = []
    try:
        for d in pending_days(conn, local_date(now)):
            counts = export_day(conn, archive_dir, d)
            with conn:
                conn.execute("INSERT INTO meta VALUES ('archived_until', ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v", (d.isoformat(),))
            log.info("Archiv %s: %s", d, counts)
            done.append(d.isoformat())
        export_snapshots(conn, archive_dir)
        r = conn.execute("SELECT v FROM meta WHERE k = 'archived_until'").fetchone()
        if r:
            keep_from = day_bounds(date.fromisoformat(r["v"]) - timedelta(days=hot_days - 1))[0]
            with conn:
                for table, col in DAILY.items():
                    conn.execute(f"DELETE FROM {table} WHERE {col} < ?", (keep_from,))
            conn.execute("PRAGMA optimize")
    finally:
        conn.close()
    return done


def remove_temp_files(data_dir: Path) -> int:
    """Reste abgebrochener atomarer Schreibvorgänge (.<name>.tmp) entfernen."""
    n = 0
    for sub in ("archive", "raw", "backups"):
        d = data_dir / sub
        if d.exists():
            for p in d.rglob(".*.tmp"):
                p.unlink(missing_ok=True)
                n += 1
    return n


# ------------------------------------------------------------------ Rohdaten
class RawWriter:
    def __init__(self, raw_dir: Path, every_min: int, level: int = 10):
        self.dir = raw_dir
        self.every = every_min * 60
        self.cctx = zstandard.ZstdCompressor(level=level)
        self.last_bucket: dict[str, int] = {}

    def _bucket(self, ts: int) -> int:
        """Fenster nach Zürcher Wanduhrzeit (z. B. 00/06/12/18 Uhr), nicht nach UTC."""
        off = datetime.fromtimestamp(ts, TZ).utcoffset()
        return (ts + int(off.total_seconds() if off else 0)) // self.every

    def due(self, source: str, ts: int) -> bool:
        return self.every > 0 and self._bucket(ts) != self.last_bucket.get(source)

    def write(self, source: str, ts: int, raw: dict[str, bytes]) -> list[Path]:
        if not raw or not self.due(source, ts):
            return []
        self.last_bucket[source] = self._bucket(ts)
        dt = datetime.fromtimestamp(ts, TZ)
        folder = self.dir / dt.strftime("%Y-%m-%d")
        folder.mkdir(parents=True, exist_ok=True)
        out = []
        for feed, body in raw.items():
            p = folder / f"{dt.strftime('%H%M')}_{source}_{feed}.json.zst"
            tmp = p.with_name("." + p.name + ".tmp")
            tmp.write_bytes(self.cctx.compress(body))
            os.replace(tmp, p)
            out.append(p)
        return out


def prune_raw(raw_dir: Path, keep_days: int, today: date) -> int:
    if keep_days <= 0 or not raw_dir.exists():
        return 0
    n = 0
    for d in raw_dir.iterdir():
        try:
            day = date.fromisoformat(d.name)
        except ValueError:
            continue
        if (today - day).days > keep_days:
            for f in d.iterdir():
                f.unlink()
            d.rmdir()
            n += 1
    return n


# ------------------------------------------------------------------ SQLite-Backup
def backup_sqlite(db_path: Path, backup_dir: Path, keep: int, now: float) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"sharedmob-{local_date(now).isoformat()}.sqlite.zst"
    tmp_db = backup_dir / ".backup.sqlite.tmp"
    src, dst = sqlite3.connect(str(db_path), timeout=120), sqlite3.connect(str(tmp_db))
    try:
        src.backup(dst, pages=8192)
    finally:
        dst.close()
        src.close()
    tmp = target.with_name("." + target.name + ".tmp")
    with open(tmp_db, "rb") as fi, open(tmp, "wb") as fo:
        zstandard.ZstdCompressor(level=6, threads=-1).copy_stream(fi, fo)
    tmp_db.unlink()
    os.replace(tmp, target)
    if keep > 0:
        for old in sorted(backup_dir.glob("sharedmob-*.sqlite.zst"))[:-keep]:
            old.unlink()
    return target

"""Sammler: ruft alle Quellen im Takt ab, verfolgt Zustände, schreibt in SQLite,
legt Rohdaten ab und archiviert täglich nach Parquet.

  python -m sharedmob.collector           Dauerbetrieb
  python -m sharedmob.collector --once    ein Abruf (Test)
  python -m sharedmob.collector --archive-now
"""
from __future__ import annotations

import argparse
import logging
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from . import archive
from .config import Config, load_config
from .sources import make_source
from .store import Store
from .tracker import Tracker

log = logging.getLogger("collector")


class Collector:
    def __init__(self, cfg: Config, fetch=None):
        self.cfg = cfg
        self.store = Store(cfg.db_path)
        self.sources = [make_source(s, fetch=fetch) for s in cfg.sources]
        self.trackers: dict[str, Tracker] = {}
        for s in cfg.sources:
            t = Tracker(
                s.id,
                move_tolerance_m=cfg.move_tolerance_m,
                trip_min_distance_m=cfg.trip_min_distance_m,
                round_trip_min_s=cfg.round_trip_min_s,
                trip_link_max_h=cfg.trip_link_max_h,
                id_strategy=cfg.id_strategy,
                position_providers=cfg.position_providers,
                auto_churn_threshold=cfg.auto_churn_threshold,
                next_vehicle_id=self.store.id_allocator("vehicle_spell"),
                next_station_id=self.store.id_allocator("station_spell"),
            )
            t.restore(**self.store.restore_args(s.id, t.link_max_s))
            log.info("[%s] wiederaufgenommen: %d offene Fahrzeug-, %d Stations-Zustände", s.id, len(t.open_v), len(t.open_s))
            self.trackers[s.id] = t
        self.raw = archive.RawWriter(cfg.raw_dir, cfg.raw_every_min)
        n = archive.remove_temp_files(cfg.data_dir)
        if n:
            log.warning("%d unvollständige Temp-Dateien eines früheren Abbruchs entfernt", n)
        self.pool = ThreadPoolExecutor(max_workers=max(1, len(self.sources)), thread_name_prefix="fetch")
        self.io = ThreadPoolExecutor(max_workers=1, thread_name_prefix="raw")
        self.maint_thread: threading.Thread | None = None
        self.last_ok: dict[str, float] = {}

    # ------------------------------------------------------------ Takt
    def interval(self, now: float) -> int:
        h = datetime.fromtimestamp(now, archive.TZ).hour
        return self.cfg.night_interval_s if self.cfg.night_from_hour <= h < self.cfg.night_to_hour else self.cfg.interval_s

    # ------------------------------------------------------------ ein Durchgang
    def tick(self, now: float | None = None, maintenance: bool = True) -> dict[str, dict]:
        now = time.time() if now is None else now
        futures = {src.cfg.id: (src, self.pool.submit(self._poll, src, now)) for src in self.sources}
        results = {}
        for sid, (src, fut) in futures.items():
            snap, err, ms = fut.result()
            t0 = time.monotonic()
            poll = {"source": sid, "ts": int(now), "ok": 0, "stale": 0, "ms": ms, "bytes_in": None, "obs_ts": None,
                    "n_vehicles": None, "n_stations": None, "v_changes": 0, "s_changes": 0, "trips": 0,
                    "endpoint": getattr(src, "base_url", src.cfg.url), "error": err}
            if snap is None:
                log.error("[%s] Abruf fehlgeschlagen: %s – nichts geschrieben", sid, err)
                self.store.write(None, None, poll)
                results[sid] = poll
                continue
            tr = self.trackers[sid]
            ch = tr.apply(snap)
            poll.update(ok=1, bytes_in=snap.bytes_in, obs_ts=snap.obs_ts,
                        n_vehicles=None if snap.vehicles is None else len(snap.vehicles),
                        n_stations=None if snap.stations is None else len(snap.stations))
            if ch is None:
                poll["stale"] = 1  # Feed seit letztem Abruf unverändert
            else:
                poll.update(v_changes=ch.n_vehicle_changes, s_changes=len(ch.s_open) + len(ch.s_close), trips=len(ch.trips))
            self.store.write(ch, snap, poll, dict(tr.prov_mode) if ch is not None and ch.mode_switches else None)
            if snap.raw and self.raw.due(sid, snap.obs_ts):
                self.io.submit(self._write_raw, sid, snap.obs_ts, snap.raw)
            self.last_ok[sid] = now
            poll["write_ms"] = int((time.monotonic() - t0) * 1000)
            results[sid] = poll
            log.info("[%s] %s: %s Fahrzeuge, %s Stationen, Δ %d/%d, Fahrten %d, %d KB, %d+%d ms%s", sid,
                     datetime.fromtimestamp(snap.obs_ts, archive.TZ).strftime("%H:%M:%S"), poll["n_vehicles"], poll["n_stations"],
                     poll["v_changes"], poll["s_changes"], poll["trips"], (snap.bytes_in or 0) // 1000, ms, poll["write_ms"],
                     " (unverändert)" if poll["stale"] else "")
        self._heartbeat()
        if maintenance:
            self._maybe_maintenance(now)
        return results

    def _poll(self, src, now: float):
        t0 = time.monotonic()
        try:
            return src.poll(now), None, int((time.monotonic() - t0) * 1000)
        except Exception as e:  # noqa: BLE001
            return None, f"{type(e).__name__}: {e}"[:500], int((time.monotonic() - t0) * 1000)

    def _write_raw(self, sid: str, ts: int, raw: dict[str, bytes]) -> None:
        try:
            for p in self.raw.write(sid, ts, raw):
                log.info("Rohdaten: %s (%.0f KB)", p.name, p.stat().st_size / 1000)
        except Exception:  # noqa: BLE001
            log.exception("Rohdaten konnten nicht geschrieben werden")

    def _heartbeat(self) -> None:
        try:
            (self.cfg.data_dir / "heartbeat").write_text(str(int(time.time())))
        except OSError:
            pass

    # ------------------------------------------------------------ tägliche Wartung (eigener Thread)
    def _maybe_maintenance(self, now: float) -> None:
        dt = datetime.fromtimestamp(now, archive.TZ)
        if dt.hour < self.cfg.archive_hour or self.store.get_meta("maint_date") == dt.date().isoformat():
            return
        if self.maint_thread and self.maint_thread.is_alive():
            return
        self.store.set_meta("maint_date", dt.date().isoformat())
        self.maint_thread = threading.Thread(target=self.maintenance, args=(now,), name="maintenance", daemon=True)
        self.maint_thread.start()

    def maintenance(self, now: float) -> None:
        try:
            days = archive.run_archive(self.cfg.db_path, self.cfg.archive_dir, self.cfg.hot_days, now)
            log.info("Archiv aktualisiert: %s", ", ".join(days) or "keine neuen Tage")
            p = archive.backup_sqlite(self.cfg.db_path, self.cfg.backup_dir, self.cfg.sqlite_backups_keep, now)
            log.info("DB-Backup: %s (%.1f MB)", p.name, p.stat().st_size / 1e6)
            n = archive.prune_raw(self.cfg.raw_dir, self.cfg.raw_keep_local_days, archive.local_date(now))
            if n:
                log.info("Rohdaten: %d alte Tage lokal gelöscht", n)
            self._meta_own_conn("last_maintenance", str(int(time.time())))
        except Exception as e:  # noqa: BLE001
            log.exception("Wartung fehlgeschlagen – wird morgen erneut versucht")
            self._meta_own_conn("maint_error", f"{int(time.time())} {type(e).__name__}: {e}"[:500])

    def _meta_own_conn(self, k: str, v: str) -> None:
        """Eigene Verbindung: Die Wartung läuft in einem anderen Thread als der Abruf."""
        from .store import connect

        c = connect(self.cfg.db_path)
        try:
            with c:
                c.execute("INSERT INTO meta VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v", (k, v))
        finally:
            c.close()

    # ------------------------------------------------------------ Dauerbetrieb
    def run_forever(self, stop: threading.Event) -> None:
        log.info("Sammler gestartet: %d Quellen, Takt %d s (nachts %d s), Daten in %s",
                 len(self.sources), self.cfg.interval_s, self.cfg.night_interval_s, self.cfg.data_dir)
        while not stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 – der Sammler darf nie sterben
                log.exception("Unerwarteter Fehler im Durchgang")
            now = time.time()
            iv = self.interval(now)
            stop.wait(iv - (now % iv) + 2)  # auf volle Minute + 2 s ausrichten (Feeds aktualisieren zur Minute)
        self.pool.shutdown(wait=True)
        self.io.shutdown(wait=True)
        if self.maint_thread:
            self.maint_thread.join(timeout=600)
        log.info("Sammler beendet")


def main() -> None:
    ap = argparse.ArgumentParser(description="Shared-Mobility-Sammler")
    ap.add_argument("--once", action="store_true", help="einen Abruf machen und beenden")
    ap.add_argument("--archive-now", action="store_true", help="Archiv/Backup sofort ausführen")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    c = Collector(cfg)
    if args.archive_now:
        c.maintenance(time.time())
        return
    if args.once:
        for sid, r in c.tick(maintenance=False).items():
            print(sid, r)
        c.io.shutdown(wait=True)
        return
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    c.run_forever(stop)


if __name__ == "__main__":
    main()

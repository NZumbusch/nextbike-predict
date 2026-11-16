"""Sammler: ruft den Feed im Takt ab und schreibt alles in SQLite.

Start:  python -m nextbike.collector [--once]
"""
from __future__ import annotations

import argparse
import logging
import signal
import threading
import time
from pathlib import Path

from . import db
from .backup import make_backup
from .config import Config, load_config
from .feed import FeedError, fetch_feed
from .timeutil import local

log = logging.getLogger("collector")


class Collector:
    def __init__(self, cfg: Config, fetch=fetch_feed) -> None:
        self.cfg = cfg
        self.fetch = fetch
        self.conn = db.connect(cfg.db_path)
        self.tracker, self.extra = db.load_tracker(self.conn)
        self.backup_thread: threading.Thread | None = None

    # ------------------------------------------------------------ Takt
    def interval(self, now: float) -> int:
        h = local(now).hour
        night = self.cfg.night_from_hour <= h < self.cfg.night_to_hour
        return self.cfg.night_poll_seconds if night else self.cfg.poll_seconds

    def need_full(self, now: float) -> bool:
        return now - self.extra.get("full_ts", 0) >= self.cfg.full_feed_every_minutes * 60

    def maintenance_due(self, now: float) -> bool:
        last = self.extra.get("maint_ts", 0)
        if not last:
            return False  # erster Tag: erst ab der nächsten Wartungsstunde
        d = local(now)
        return d.hour >= self.cfg.maintenance_hour and local(last).date() != d.date()

    # ------------------------------------------------------------ ein Abruf
    def poll_once(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        ts = int(now // 60 * 60) if self.cfg.poll_seconds >= 60 else int(now)
        full = self.need_full(now)
        t0 = time.monotonic()
        try:
            feed = self.fetch(self.cfg.feed_url_full if full else self.cfg.feed_url, self.cfg.city_id)
        except Exception as e:  # noqa: BLE001
            msg = str(e) if isinstance(e, FeedError) else f"{type(e).__name__}: {e}"
            log.error("Feed-Fehler: %s – nichts geschrieben", msg)
            with self.conn:
                db.log_poll(self.conn, ts, False, int((time.monotonic() - t0) * 1000), error=msg[:500])
            return {"ok": False, "error": msg}

        if self.tracker.ts is not None and ts <= self.tracker.ts:
            return {"ok": True, "skipped": True}  # doppelter Abruf in derselben Minute
        with self.conn:  # eine Transaktion pro Abruf
            if full or not self.extra.get("full_ts"):
                db.upsert_master(self.conn, ts, feed)
                self.extra["full_ts"] = int(now)
            coords = {**db.station_coords(self.conn), **feed.coords()}
            step = self.tracker.step(ts, feed)
            db.write_step(self.conn, step, feed, coords)
            ts5 = ts // 300 * 300
            n_obs = 0
            if ts5 > self.extra.get("obs_ts", 0):
                n_obs = db.write_obs(self.conn, ts5, feed)
                self.extra["obs_ts"] = ts5
            if not self.extra.get("maint_ts"):
                self.extra["maint_ts"] = int(now)
            db.save_state(self.conn, self.tracker, self.extra)
            db.log_poll(self.conn, ts, True, int((time.monotonic() - t0) * 1000), len(step.moves), len(step.trips))
        self._heartbeat()
        res = {"ok": True, "moves": len(step.moves), "trips": len(step.trips), "obs": n_obs, "stations": len(step.station_changes), "full": full}
        log.info("Abruf %s: %s", local(ts).strftime("%H:%M"), res)
        if self.maintenance_due(now):
            self.maintenance(now)
        return res

    # ------------------------------------------------------------ Wartung
    def maintenance(self, now: float) -> None:
        cut = int(now) - self.cfg.poll_log_days * 86400
        with self.conn:
            self.conn.execute("DELETE FROM poll WHERE ts < ?", (cut,))
            pruned = self.tracker.prune_away(int(now))
            self.extra["maint_ts"] = int(now)
            db.save_state(self.conn, self.tracker, self.extra)
        self.conn.execute("PRAGMA optimize")
        log.info("Wartung: Abruf-Log bereinigt, %d alte 'unterwegs'-Einträge entfernt", pruned)
        if self.backup_thread and self.backup_thread.is_alive():
            return

        def run():
            try:
                p = make_backup(self.cfg.db_path, self.cfg.backup_dir, self.cfg.keep_local_backups, now)
                log.info("Backup erstellt: %s (%.1f MB)", p.name, p.stat().st_size / 1e6)
                with db.connect(self.cfg.db_path) as c:
                    db.set_meta(c, "last_backup", f"{int(time.time())} {p.name}")
            except Exception:  # noqa: BLE001
                log.exception("Backup fehlgeschlagen")

        self.backup_thread = threading.Thread(target=run, name="backup", daemon=True)
        self.backup_thread.start()

    def _heartbeat(self) -> None:
        try:
            (self.cfg.data_dir / "heartbeat").write_text(str(int(time.time())))
        except OSError:
            pass

    # ------------------------------------------------------------ Schleife
    def run_forever(self, stop: threading.Event) -> None:
        log.info("Sammler gestartet: %d s Takt (nachts %d s), DB %s", self.cfg.poll_seconds, self.cfg.night_poll_seconds, self.cfg.db_path)
        while not stop.is_set():
            try:
                self.poll_once()
            except Exception:  # noqa: BLE001 – Sammler darf nie sterben
                log.exception("Unerwarteter Fehler im Abruf")
            now = time.time()
            iv = self.interval(now)
            stop.wait(iv - (now % iv) + 1)  # auf die nächste volle Minute ausrichten
        if self.backup_thread:
            self.backup_thread.join(timeout=120)
        log.info("Sammler beendet")


def main() -> None:
    ap = argparse.ArgumentParser(description="nextbike-Sammler")
    ap.add_argument("--once", action="store_true", help="nur einen Abruf machen")
    ap.add_argument("--backup-now", action="store_true", help="sofort ein Backup erstellen")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    c = Collector(cfg)
    if args.backup_now:
        p = make_backup(cfg.db_path, cfg.backup_dir, cfg.keep_local_backups, time.time())
        print(p)
        return
    if args.once:
        print(c.poll_once())
        return
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    c.run_forever(stop)


if __name__ == "__main__":
    main()

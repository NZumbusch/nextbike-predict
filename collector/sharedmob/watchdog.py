"""Watchdog: prüft jede Minute, ob alles läuft, und schickt Push-Nachrichten über ntfy.

Eigener Dienst (nicht im Sammler), damit er auch dann meldet, wenn der Sammler hängt.

  python -m sharedmob.watchdog            Dauerbetrieb
  python -m sharedmob.watchdog --test     Testnachricht senden

Einrichtung: ntfy-App installieren (Android/iOS), ein zufälliges Thema abonnieren und dasselbe
in .env als NTFY_TOPIC eintragen. Nachrichten enthalten nur Betriebsdaten, keine persönlichen Daten.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sqlite3
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .archive import TZ, local_date
from .config import Config, load_config
from .usage import usage

log = logging.getLogger("watchdog")


@dataclass
class Alert:
    key: str
    active: bool
    title: str
    message: str
    priority: int = 3          # ntfy: 1 min … 5 max
    tags: str = "warning"
    recover_msg: str | None = None  # Nachricht beim Ende des Problems (None = keine)


class NtfySender:
    """Veröffentlicht per JSON (UTF-8 in Titel und Text, anders als bei HTTP-Headern)."""

    def __init__(self, server: str, topic: str, token: str | None = None):
        self.url = server.rstrip("/")
        self.topic = topic
        self.token = token

    def __call__(self, title: str, message: str, priority: int = 3, tags: str = "") -> None:
        body = {"topic": self.topic, "title": title, "message": message, "priority": priority,
                "tags": [t for t in tags.split(",") if t]}
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(self.url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=20) as r:
            r.read()


class Watchdog:
    def __init__(self, cfg: Config, send=None, now=time.time):
        self.cfg = cfg
        self.send = send
        self.now = now
        self.state_path = cfg.data_dir / "watchdog-state.json"
        try:
            self.state: dict = json.loads(self.state_path.read_text("utf-8"))
        except (OSError, ValueError):
            self.state = {}

    # ------------------------------------------------------------ Prüfungen
    def _db(self) -> sqlite3.Connection | None:
        if not self.cfg.db_path.exists():
            return None
        c = sqlite3.connect(f"file:{self.cfg.db_path}?mode=ro", uri=True, timeout=30)
        c.row_factory = sqlite3.Row
        return c

    def checks(self) -> list[Alert]:
        a = self.cfg.alerts
        now = self.now()
        out: list[Alert] = []
        hb = self.cfg.data_dir / "heartbeat"
        hb_age = now - hb.stat().st_mtime if hb.exists() else None
        down = hb_age is None or hb_age > a.stale_min * 60
        out.append(Alert("collector", down, "Sammler meldet sich nicht",
                         f"Seit {int(hb_age // 60)} min kein Durchgang." if hb_age else "Noch nie ein Durchgang (läuft der Container?).",
                         priority=5, tags="rotating_light", recover_msg="Sammler läuft wieder."))
        c = self._db()
        if c is not None:
            try:
                for s in self.cfg.sources:
                    last = c.execute("SELECT max(ts) FROM poll WHERE source = ? AND ok = 1 AND stale = 0", (s.id,)).fetchone()[0]
                    err = c.execute("SELECT error FROM poll WHERE source = ? AND ok = 0 ORDER BY ts DESC LIMIT 1", (s.id,)).fetchone()
                    silent = not down and last is not None and now - last > a.stale_min * 60
                    out.append(Alert(f"source:{s.id}", silent, f"Quelle {s.id}: keine frischen Daten",
                                     f"Seit {int((now - (last or now)) // 60)} min. Letzter Fehler: {err[0] if err else '–'}",
                                     priority=4, recover_msg=f"Quelle {s.id} liefert wieder Daten."))
                # Anbieter verschwunden: vor ~24 h ≥ N Fahrzeuge, seit 1 h kein Eintrag mehr
                gone = c.execute(
                    """SELECT source, provider, max(n_vehicles) n FROM provider_stat WHERE ts BETWEEN ? AND ?
                       GROUP BY source, provider HAVING n >= ? AND NOT EXISTS (
                         SELECT 1 FROM provider_stat q WHERE q.source = provider_stat.source AND q.provider = provider_stat.provider
                           AND q.ts >= ? AND q.n_vehicles > 0)""",
                    (now - 26 * 3600, now - 22 * 3600, a.provider_min_vehicles, now - 3600)).fetchall()
                gone_keys = {f"provider:{r['source']}:{r['provider']}": r for r in gone}
                for k, r in gone_keys.items():
                    out.append(Alert(k, True, f"Anbieter {r['provider']} fehlt im Feed",
                                     f"Gestern noch {r['n']} Fahrzeuge, seit über 1 h keine mehr ({r['source']}).", priority=3,
                                     recover_msg=f"Anbieter {r['provider']} ist wieder im Feed."))
                for k in [k for k, v in self.state.items() if k.startswith("provider:") and v.get("active") and k not in gone_keys]:
                    out.append(Alert(k, False, "", "", recover_msg=f"Anbieter {k.split(':', 2)[2]} ist wieder im Feed."))
                new = c.execute("SELECT provider, name FROM provider_info WHERE first_seen >= ?", (now - 3600,)).fetchall()
                known_since = self.state.get("_first_check", now)
                for r in new:
                    if now - known_since > 3600:  # nicht beim allerersten Start alle Anbieter melden
                        out.append(Alert(f"newprov:{r['provider']}", True, "Neuer Anbieter im Feed",
                                         f"{r['provider']} ({r['name'] or '–'})", priority=2, tags="sparkles"))
                me = c.execute("SELECT v FROM meta WHERE k = 'maint_error'").fetchone()
                if me:
                    out.append(Alert(f"maint:{me[0][:10]}", True, "Wartung fehlgeschlagen", me[0], priority=4))
            finally:
                c.close()
        u = usage(self.cfg, local_date(now))
        low = u["free_ratio"] is not None and u["free_ratio"] < a.disk_free_pct / 100
        out.append(Alert("disk", low, "Speicher fast voll",
                         f"Nur noch {u['disk_free'] / 1e9:.1f} GB frei ({(u['free_ratio'] or 0) * 100:.0f} %).", priority=5,
                         recover_msg="Speicher wieder ausreichend."))
        soon = u["days_left"] is not None and u["days_left"] < a.days_left_warn
        out.append(Alert("forecast", soon, "Speicher bald voll",
                         f"Bei aktuellem Wachstum ({(u['per_day'] or 0) / 1e6:.1f} MB/Tag) voll in ≈ {u['days_left']} Tagen.", priority=3))
        up = self.cfg.data_dir / "upload-status.txt"
        if up.exists():
            txt = up.read_text("utf-8").strip()
            stale_up = now - up.stat().st_mtime > a.upload_stale_h * 3600
            bad = "FEHLER" in txt or stale_up
            out.append(Alert("upload", bad, "Upload zu Google Drive gestört",
                             txt if "FEHLER" in txt else f"Seit {int((now - up.stat().st_mtime) // 3600)} h kein Upload-Versuch.",
                             priority=4, recover_msg="Upload funktioniert wieder."))
        return out

    # ------------------------------------------------------------ Benachrichtigen
    def run_once(self) -> list[tuple[str, str]]:
        now = self.now()
        self.state.setdefault("_first_check", now)
        sent: list[tuple[str, str]] = []
        for al in self.checks():
            st = self.state.get(al.key, {"active": False})
            if al.active:
                repeat = now - st.get("last_sent", 0) >= self.cfg.alerts.repeat_h * 3600
                if not st.get("active") or (repeat and not al.key.startswith(("newprov:", "maint:"))):
                    self._send(al.title, al.message, al.priority, al.tags, sent)
                    st = {"active": True, "since": st.get("since", now) if st.get("active") else now, "last_sent": now}
                else:
                    st["active"] = True
            elif st.get("active"):
                if al.recover_msg:
                    mins = int((now - st.get("since", now)) // 60)
                    self._send("✓ " + al.recover_msg.split(".")[0], f"{al.recover_msg} (Dauer ≈ {mins} min)", 2, "white_check_mark", sent)
                st = {"active": False}
            self.state[al.key] = st
        self._save()
        return sent

    def _send(self, title: str, msg: str, prio: int, tags: str, sent: list) -> None:
        sent.append((title, msg))
        log.warning("Meldung: %s – %s", title, msg)
        if self.send is not None:
            try:
                self.send(title, msg, prio, tags)
            except Exception as e:  # noqa: BLE001 – Benachrichtigung darf den Watchdog nicht stoppen
                log.error("ntfy fehlgeschlagen: %s", e)

    def _save(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(self.state), "utf-8")
            os.replace(tmp, self.state_path)
        except OSError:
            pass


def sender_from_env() -> NtfySender | None:
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        return None
    return NtfySender(os.environ.get("NTFY_SERVER", "https://ntfy.sh"), topic, os.environ.get("NTFY_TOKEN") or None)


def main() -> None:
    ap = argparse.ArgumentParser(description="Watchdog mit Push-Meldungen (ntfy)")
    ap.add_argument("--test", action="store_true", help="Testnachricht senden und beenden")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    send = sender_from_env()
    if send is None:
        log.warning("NTFY_TOPIC nicht gesetzt – Meldungen erscheinen nur im Log")
    if args.test:
        if send:
            send("sharedmob: Test", "Push-Meldungen funktionieren. 👍", 3, "tada")
            print("gesendet")
        return
    wd = Watchdog(cfg, send)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    stop.wait(0 if os.environ.get("WATCHDOG_NO_DELAY") else 120)  # Sammler beim Start Zeit lassen
    while not stop.is_set():
        try:
            wd.run_once()
        except Exception:  # noqa: BLE001
            log.exception("Prüfung fehlgeschlagen")
        stop.wait(60)


if __name__ == "__main__":
    main()

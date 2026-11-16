import json
import os
import time
from datetime import date, datetime

from sharedmob import archive
from sharedmob.collector import Collector
from sharedmob.usage import usage
from sharedmob.watchdog import Watchdog
from sharedmob.web import create_app

TZ = archive.TZ
NOW = datetime(2026, 11, 16, 7, 0, tzinfo=TZ).timestamp()


def run(c, fake, minute):
    fake.ts = int(NOW + minute * 60)
    return c.tick(NOW + minute * 60, maintenance=False)


def test_gap_detection_and_reasons(cfg, fake):
    c = Collector(cfg, fetch=fake)
    run(c, fake, 0)
    fake.fail.add("free_bike_status.json")
    for m in range(1, 6):
        run(c, fake, m)  # 5 Min Abruffehler
    fake.fail.clear()
    run(c, fake, 6)
    (g,) = c.store.conn.execute("SELECT * FROM gap WHERE source = 'sharedmobility'").fetchall()
    assert (g["duration_s"], g["reason"], g["n_failed"]) == (360, "fetch_error", 5)
    assert not c.store.conn.execute("SELECT * FROM gap WHERE source = 'nextbike'").fetchall()
    c.store.conn.close()
    # Sammler 20 Min aus (Neustart) ⇒ collector_down
    c2 = Collector(cfg, fetch=fake)
    run(c2, fake, 26)
    reasons = {r["reason"] for r in c2.store.conn.execute("SELECT reason FROM gap")}
    assert reasons == {"fetch_error", "collector_down"}
    # Lücken gehen ins Archiv und bleiben in der Arbeits-DB
    archive.run_archive(cfg.db_path, cfg.archive_dir, 0, datetime(2026, 11, 17, 3, tzinfo=TZ).timestamp())
    assert (cfg.archive_dir / "gap" / "date=2026-11-16" / "part-0.parquet").exists()
    assert c2.store.conn.execute("SELECT count(*) FROM gap").fetchone()[0] == 3


def test_usage_forecast(cfg):
    for d, mb in [("2026-11-14", 6), ("2026-11-15", 8)]:
        p = cfg.archive_dir / "trip" / f"date={d}"
        p.mkdir(parents=True)
        (p / "part-0.parquet").write_bytes(b"x" * mb * 1_000_000)
    (cfg.raw_dir / "2026-11-15").mkdir(parents=True)
    (cfg.raw_dir / "2026-11-15" / "a.zst").write_bytes(b"x" * 2_000_000)
    (cfg.raw_dir / "2026-11-16").mkdir(parents=True)  # heute: zählt nicht
    (cfg.raw_dir / "2026-11-16" / "a.zst").write_bytes(b"x" * 50_000_000)
    u = usage(cfg, date(2026, 11, 16))
    assert u["per_day"] == 8_000_000 and u["days_measured"] == 2
    assert u["days_left"] == u["disk_free"] // 8_000_000


class Sent(list):
    def __call__(self, title, msg, prio, tags):
        self.append(title)


def test_watchdog_collector_down_and_recovery(cfg, fake):
    c = Collector(cfg, fetch=fake)
    run(c, fake, 0)
    hb = cfg.data_dir / "heartbeat"
    t = [NOW + 60]
    os.utime(hb, (t[0], t[0]))
    sent = Sent()
    wd = Watchdog(cfg, sent, now=lambda: t[0])
    assert wd.run_once() == []                       # alles ok
    os.utime(hb, (t[0] - 20 * 60, t[0] - 20 * 60))  # 20 Min kein Durchgang
    wd.run_once()
    assert sent == ["Sammler meldet sich nicht"]
    wd.run_once()
    assert len(sent) == 1                            # nicht jede Minute wiederholen
    t[0] += 7 * 3600
    wd.run_once()
    assert sent[1] == "Sammler meldet sich nicht"    # Erinnerung nach repeat_h
    os.utime(hb, (t[0], t[0]))
    wd.run_once()
    assert any(x.startswith("✓ Sammler läuft wieder") for x in sent)
    # Zustand übersteht Neustart des Watchdogs
    assert json.loads((cfg.data_dir / "watchdog-state.json").read_text())["collector"]["active"] is False


def test_watchdog_upload_disk_provider(cfg, fake):
    c = Collector(cfg, fetch=fake)
    run(c, fake, 0)
    t = NOW + 60
    (cfg.data_dir / "upload-status.txt").write_text("x: FEHLER beim Upload")
    cfg.alerts.disk_free_pct = 101  # erzwingt "Speicher fast voll"
    # Anbieter mit 25 Fahrzeugen vor 24 h, seither nicht mehr
    with c.store.conn:
        c.store.conn.execute("INSERT INTO provider_stat (ts, source, provider, id_mode, n_vehicles, n_new_ids, n_gone_ids, n_opened, n_closed, n_polls) VALUES (?, 'sharedmobility', 'weg_ag', 'id', 25, 0, 0, 0, 0, 1)", (int(t - 24 * 3600),))
    sent = Sent()
    titles = {a.title for a in Watchdog(cfg, sent, now=lambda: t).checks() if a.active}
    assert {"Upload zu Google Drive gestört", "Speicher fast voll", "Anbieter weg_ag fehlt im Feed"} <= titles
    assert "Sammler meldet sich nicht" not in titles


def test_status_and_provider_pages(cfg, fake):
    c = Collector(cfg, fetch=fake)
    run(c, fake, 0)
    run(c, fake, 1)
    client = create_app(cfg).test_client()
    r = client.get("/")
    assert r.status_code == 200 and "Datenlücken".encode() in r.data and b"Prognose" in r.data
    r = client.get("/provider/sharedmobility/bird-zurich")
    assert r.status_code == 200 and b"<svg" in r.data and "Anteil neuer IDs".encode() in r.data
    assert client.get("/provider/sharedmobility/gibtsnicht").status_code == 404

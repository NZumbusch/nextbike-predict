import gzip
import sqlite3
from datetime import date, datetime

import pyarrow.parquet as pq
import zstandard

from sharedmob import archive
from sharedmob.collector import Collector
from sharedmob.web import create_app

TZ = archive.TZ
NOW = datetime(2026, 11, 16, 7, 0, tzinfo=TZ).timestamp()


def q(c, sql, *a):
    return c.store.conn.execute(sql, a).fetchall()


def nb_move(fake, bike, to_uid):
    """Velo im nextbike-Feed verschieben (to_uid=None ⇒ ausgeliehen)."""
    for p in fake.places():
        if bike in p["bike_numbers"]:
            p["bike_numbers"].remove(bike)
    if to_uid is not None:
        next(p for p in fake.places() if p["uid"] == to_uid)["bike_numbers"].append(bike)


def run(c, fake, minute):
    fake.ts = int(NOW + minute * 60)
    return c.tick(NOW + minute * 60, maintenance=False)


def test_end_to_end_trip_across_restart(cfg, fake):
    c = Collector(cfg, fetch=fake)
    r = run(c, fake, 0)
    assert r["sharedmobility"]["ok"] and r["nextbike"]["ok"]
    assert r["sharedmobility"]["n_vehicles"] == 75
    station = next(p for p in fake.places() if p["spot"] and p["bike_numbers"])
    target = next(p for p in fake.places() if p["spot"] and p["uid"] != station["uid"])
    bike = station["bike_numbers"][0]
    nb_move(fake, bike, None)
    run(c, fake, 1)
    c.store.conn.close()

    c2 = Collector(cfg, fetch=fake)  # Neustart (z. B. Stromausfall)
    nb_move(fake, bike, target["uid"])
    r = run(c2, fake, 9)
    assert r["nextbike"]["trips"] == 1
    (t,) = q(c2, "SELECT * FROM trip WHERE source = 'nextbike'")
    assert (t["vehicle_id"], t["from_station"], t["to_station"], t["kind"]) == (bike, str(station["uid"]), str(target["uid"]), "ride")
    assert t["arr_ts"] - t["dep_next_ts"] == 8 * 60
    # nichts doppelt: jedes Fahrzeug hat genau einen offenen Zustand
    assert not q(c2, "SELECT source, provider, vehicle_id FROM vehicle_spell WHERE end_ts IS NULL GROUP BY 1, 2, 3 HAVING count(*) > 1")


def test_feed_error_and_stale(cfg, fake):
    c = Collector(cfg, fetch=fake)
    run(c, fake, 0)
    before = q(c, "SELECT count(*) FROM vehicle_spell")[0][0]
    fake.fail.add("free_bike_status.json")
    r = run(c, fake, 1)
    assert r["sharedmobility"]["ok"] == 0 and "503" in r["sharedmobility"]["error"]
    assert q(c, "SELECT count(*) FROM vehicle_spell")[0][0] == before
    fake.fail.clear()
    assert run(c, fake, 2)["sharedmobility"]["stale"] == 0
    r = c.tick(NOW + 180, maintenance=False)  # Feed hat sich nicht aktualisiert (gleiches last_updated)
    assert r["sharedmobility"]["stale"] == 1
    assert q(c, "SELECT count(*) FROM vehicle_spell WHERE end_ts IS NOT NULL")[0][0] == 0


def test_archive_partitions_and_cleanup(cfg, fake):
    c = Collector(cfg, fetch=fake)
    run(c, fake, 0)
    bike = next(p for p in fake.places() if p["spot"] and p["bike_numbers"])["bike_numbers"][0]
    nb_move(fake, bike, None)
    run(c, fake, 1)
    nb_move(fake, bike, next(p for p in fake.places() if p["spot"])["uid"])
    run(c, fake, 10)
    next_day = datetime(2026, 11, 17, 3, 0, tzinfo=TZ).timestamp()
    done = archive.run_archive(cfg.db_path, cfg.archive_dir, hot_days=21, now=next_day)
    assert done == ["2026-11-16"]
    part = cfg.archive_dir / "vehicle_spell" / "date=2026-11-16" / "part-0.parquet"
    closed = q(c, "SELECT count(*) FROM vehicle_spell WHERE end_ts IS NOT NULL")[0][0]
    assert pq.read_table(part).num_rows == closed > 0
    assert pq.read_table(cfg.archive_dir / "trip" / "date=2026-11-16" / "part-0.parquet").num_rows >= 1
    opened = pq.read_table(cfg.archive_dir / "open" / "vehicle_spell.parquet")
    assert opened.num_rows == q(c, "SELECT count(*) FROM vehicle_spell WHERE end_ts IS NULL")[0][0]
    assert pq.read_table(cfg.archive_dir / "dim" / "station_info.parquet").num_rows > 0
    assert archive.run_archive(cfg.db_path, cfg.archive_dir, 21, next_day) == []  # idempotent
    # hot_days = 1 ⇒ archivierter Tag wird aus der Arbeits-DB entfernt, offene Zustände bleiben
    archive.run_archive(cfg.db_path, cfg.archive_dir, hot_days=0, now=next_day)
    assert q(c, "SELECT count(*) FROM vehicle_spell WHERE end_ts IS NOT NULL")[0][0] == 0
    assert q(c, "SELECT count(*) FROM vehicle_spell WHERE end_ts IS NULL")[0][0] == opened.num_rows


def test_day_bounds_dst():
    a, b = archive.day_bounds(date(2026, 3, 29))
    assert b - a == 23 * 3600
    a, b = archive.day_bounds(date(2026, 10, 25))
    assert b - a == 25 * 3600
    assert archive.day_bounds(date(2026, 11, 16))[0] == datetime(2026, 11, 16, tzinfo=TZ).timestamp()


def test_raw_writer_every_6h(tmp_path):
    w = archive.RawWriter(tmp_path, 360)
    t = int(datetime(2026, 11, 16, 6, 1, tzinfo=TZ).timestamp())  # 06:01 Zürich
    assert w.write("sm", t, {"feed": b'{"a":1}'})
    assert not w.write("sm", t + 3600, {"feed": b"{}"})  # 07:01 – gleiche Periode 06–12 Uhr
    (p,) = list((tmp_path / "2026-11-16").iterdir())
    assert p.name == "0601_sm_feed.json.zst"
    assert zstandard.ZstdDecompressor().decompress(p.read_bytes()) == b'{"a":1}'
    assert w.write("sm", t + 6 * 3600, {"feed": b"{}"})


def test_backup_and_temp_cleanup(cfg, fake):
    c = Collector(cfg, fetch=fake)
    run(c, fake, 0)
    p = archive.backup_sqlite(cfg.db_path, cfg.backup_dir, 3, NOW)
    raw = cfg.data_dir / "restore.sqlite"
    with open(p, "rb") as fi, open(raw, "wb") as fo:
        zstandard.ZstdDecompressor().copy_stream(fi, fo)
    assert sqlite3.connect(raw).execute("SELECT count(*) FROM vehicle_spell").fetchone()[0] > 0
    (cfg.archive_dir / "trip").mkdir(parents=True)
    (cfg.archive_dir / "trip" / ".part-0.parquet.tmp").write_text("x")
    assert archive.remove_temp_files(cfg.data_dir) == 1


def test_status_page(cfg, fake):
    c = Collector(cfg, fetch=fake)
    run(c, fake, 0)
    run(c, fake, 1)
    client = create_app(cfg).test_client()
    r = client.get("/")
    assert r.status_code == 200 and b"sharedmobility" in r.data and b"nextbike_ch" in r.data
    assert client.get("/api/status").json["sources"][0]["agg"]["n"] == 2
    assert client.get("/files/../hot/sharedmob.sqlite").status_code == 404
    assert client.get("/files/hot/sharedmob.sqlite").status_code == 404
    cfg.web_token = "geheim"
    t = create_app(cfg).test_client()
    assert t.get("/").status_code == 401
    assert t.get("/?token=geheim").status_code == 200

import gzip
import sqlite3
from datetime import datetime, timezone

from nextbike import db
from nextbike.backup import make_backup
from nextbike.collector import Collector
from nextbike.feed import FeedError, parse_feed
from nextbike.tracker import Tracker
from tests.conftest import FIXTURE, move_bike

T0 = datetime(2026, 11, 16, 6, 0, tzinfo=timezone.utc).timestamp()  # Mo 07:00 Zürich


def fake_fetch(jsons):
    """Liefert nacheinander die gegebenen Feed-JSONs; merkt sich die URLs."""
    it = iter(jsons)
    urls = []

    def f(url, city_id):
        urls.append(url)
        j = next(it)
        if isinstance(j, Exception):
            raise j
        return parse_feed(j, city_id)

    f.urls = urls
    return f


def count(c, t):
    return c.execute(f"SELECT count(*) FROM {t}").fetchone()[0]


def test_trip_detection_end_to_end(cfg, feed_json):
    import copy

    base = copy.deepcopy(FIXTURE)
    gone = move_bike(copy.deepcopy(FIXTURE), "401133", None)  # Ausleihe Neustadtstrasse 10
    back = move_bike(copy.deepcopy(FIXTURE), "401133", 70900)  # Rückgabe Südpol
    rebal = copy.deepcopy(back)
    for b in ["400608", "400405", "400852"]:
        rebal = move_bike(rebal, b, 3024979)  # 3 Velos gleichzeitig zur Messe
    fetch = fake_fetch([base, gone, gone, back, rebal])
    c = Collector(cfg, fetch=fetch)
    for i, m in enumerate([0, 1, 2, 9, 10]):
        r = c.poll_once(T0 + m * 60)
        assert r["ok"], r
    assert "bikes=false" not in fetch.urls[0] and "bikes=false" in fetch.urls[1]  # erster Abruf = Vollabruf

    conn = c.conn
    trips = conn.execute("SELECT * FROM trip ORDER BY id").fetchall()
    t = trips[0]
    assert (t["bike"], t["start_uid"], t["end_uid"], t["duration_s"], t["direct"]) == ("401133", 3024956, 70900, 8 * 60, 0)
    assert 1000 < t["distance_m"] < 2000
    assert [(r["end_uid"], r["direct"], r["arrivals_same_poll"]) for r in trips[1:]] == [(3024979, 1, 3)] * 3
    ev = conn.execute("SELECT * FROM bike_event WHERE bike = '401133' ORDER BY ts").fetchall()
    assert [(e["from_uid"], e["to_uid"]) for e in ev] == [(3024956, None), (None, 70900)]
    assert ev[0]["from_lat"] is not None and ev[0]["to_lat"] is None
    active = sum(1 for s in parse_feed(FIXTURE, 126).stations if s.active)
    assert active == 24  # eine Station im Ausschnitt ist inaktiv ⇒ keine obs-Zeile
    assert count(conn, "obs") == 3 * active  # 5-Min-Fenster 07:00, 07:05, 07:10 – alle aktiven Stationen
    assert count(conn, "stations") == 25 and count(conn, "bikes") >= 80
    assert conn.execute("SELECT type FROM bikes WHERE number = '400767'").fetchone()[0] == "183"
    # Stationsänderungen: Neustadtstrasse 10 von 2 auf 1
    ch = conn.execute("SELECT ts, bikes FROM station_change WHERE uid = 3024956 ORDER BY ts").fetchall()
    assert [r["bikes"] for r in ch] == [2, 1]


def test_restart_keeps_state(cfg):
    import copy

    gone = move_bike(copy.deepcopy(FIXTURE), "401133", None)
    back = move_bike(copy.deepcopy(FIXTURE), "401133", 70900)
    c1 = Collector(cfg, fetch=fake_fetch([FIXTURE, gone]))
    c1.poll_once(T0)
    c1.poll_once(T0 + 60)
    c1.conn.close()
    c2 = Collector(cfg, fetch=fake_fetch([back]))  # Neustart des Containers
    assert "401133" in c2.tracker.away
    c2.poll_once(T0 + 600)
    assert count(c2.conn, "trip") == 1


def test_feed_error_writes_nothing(cfg):
    c = Collector(cfg, fetch=fake_fetch([FIXTURE, FeedError("HTTP 502")]))
    c.poll_once(T0)
    before = {t: count(c.conn, t) for t in ["station_change", "obs", "bike_event"]}
    r = c.poll_once(T0 + 60)
    assert not r["ok"]
    assert before == {t: count(c.conn, t) for t in before}
    assert c.conn.execute("SELECT error FROM poll WHERE ok = 0").fetchone()[0] == "HTTP 502"


def test_tracker_json_roundtrip():
    t = Tracker()
    t.step(100, parse_feed(FIXTURE, 126))
    t.away["x"] = (50, (47.1, 8.3))
    t2 = Tracker.from_json(t.to_json())
    assert t2.bk == t.bk and t2.st == t.st and t2.away == t.away


def test_interval_night(cfg):
    c = Collector(cfg, fetch=fake_fetch([]))
    c.cfg.night_poll_seconds = 300
    assert c.interval(datetime(2026, 11, 16, 1, 30, tzinfo=timezone.utc).timestamp()) == 300  # 02:30 lokal
    assert c.interval(datetime(2026, 11, 16, 6, 30, tzinfo=timezone.utc).timestamp()) == 60


def test_backup_consistent_and_rotates(cfg):
    c = Collector(cfg, fetch=fake_fetch([FIXTURE]))
    c.poll_once(T0)
    for d in range(1, 10):
        p = make_backup(cfg.db_path, cfg.backup_dir, 3, T0 + d * 86400)
    files = sorted(x.name for x in cfg.backup_dir.glob("nextbike-*.sqlite.gz"))
    assert files == ["nextbike-2026-11-23.sqlite.gz", "nextbike-2026-11-24.sqlite.gz", "nextbike-2026-11-25.sqlite.gz"]
    raw = cfg.data_dir / "restore.sqlite"
    raw.write_bytes(gzip.decompress(p.read_bytes()))
    assert sqlite3.connect(raw).execute("SELECT count(*) FROM stations").fetchone()[0] == 25


def test_maintenance_triggers_backup(cfg):
    c = Collector(cfg, fetch=fake_fetch([FIXTURE] * 3))
    c.poll_once(T0)  # erster Lauf: maint_ts = jetzt, keine Wartung
    assert not list(cfg.backup_dir.glob("*.gz")) if cfg.backup_dir.exists() else True
    next_day = datetime(2026, 11, 17, 2, 10, tzinfo=timezone.utc).timestamp()  # 03:10 Zürich
    c.poll_once(next_day)
    c.backup_thread.join(30)
    assert len(list(cfg.backup_dir.glob("nextbike-*.sqlite.gz"))) == 1
    assert db.get_meta(c.conn, "last_backup")

import copy
from datetime import datetime, timezone

import pytest

from nextbike.collector import Collector
from nextbike.web import create_app
from tests.conftest import FIXTURE, move_bike
from tests.test_collector import fake_fetch

PAGES = ["/", "/stations", "/station/3024956", "/map", "/trips", "/trips?station=70900&nodirect=1", "/bike/401133", "/export",
         "/api/now", "/api/predict?station=home&at=07:45", "/api/predict?station=38568", "/api/plan?at=07:45&dir=ziel",
         "/api/stations", "/export/trip.csv", "/export/obs.csv?days=7"]


@pytest.fixture
def filled(cfg):
    t0 = datetime.now(timezone.utc).timestamp() - 600
    gone = move_bike(copy.deepcopy(FIXTURE), "401133", None)
    back = move_bike(copy.deepcopy(FIXTURE), "401133", 70900)
    c = Collector(cfg, fetch=fake_fetch([FIXTURE, gone, back]))
    for m in (0, 1, 5):
        c.poll_once(t0 + m * 60)
    c.conn.close()
    return cfg


def test_all_pages_render(filled):
    client = create_app(filled).test_client()
    for p in PAGES:
        r = client.get(p)
        assert r.status_code == 200, (p, r.data[:300])
    assert b"Neustadtstrasse 10" in client.get("/").data
    assert b"401133" in client.get("/trips").data
    csv = client.get("/export/trip.csv").data.decode()
    assert csv.splitlines()[0].startswith("id,bike,start_ts")
    assert len(csv.splitlines()) == 2


def test_api_errors(filled):
    client = create_app(filled).test_client()
    assert client.get("/api/predict").status_code == 400
    assert client.get("/api/predict?station=nix").status_code == 404
    assert client.get("/api/predict?station=home&at=quatsch").status_code == 400
    assert client.get("/api/plan?dir=xy").status_code == 400
    assert client.get("/station/1").status_code == 404
    assert client.get("/backups/../nextbike.sqlite").status_code == 404


def test_sqlite_download(filled):
    client = create_app(filled).test_client()
    r = client.get("/export/nextbike.sqlite")
    assert r.status_code == 200 and r.data[:15] == b"SQLite format 3"
    r.close()


def test_token(filled, monkeypatch):
    filled.web_token = "geheim"
    client = create_app(filled).test_client()
    assert client.get("/").status_code == 401
    assert client.get("/api/now", headers={"Authorization": "Bearer falsch"}).status_code == 401
    assert client.get("/api/now", headers={"Authorization": "Bearer geheim"}).status_code == 200
    r = client.get("/?token=geheim")
    assert r.status_code == 200 and "nb_token" in r.headers.get("Set-Cookie", "")
    assert client.get("/stations").status_code == 200  # Cookie gesetzt


def test_empty_db_does_not_crash(cfg):
    client = create_app(cfg).test_client()
    assert client.get("/").status_code == 200
    assert client.get("/api/now").json["stale"] is True
    assert client.get("/healthz").status_code == 503

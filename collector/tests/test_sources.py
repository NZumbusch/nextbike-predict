import json

import pytest

from sharedmob import http
from sharedmob.config import SourceConfig
from sharedmob.sources.gbfs import GbfsSource, parse_station_status, parse_vehicles, provider_of
from sharedmob.sources.nextbike import NextbikeSource, parse_live
from tests.conftest import fx

SM = SourceConfig(id="sharedmobility", kind="gbfs", url="https://sharedmobility.ch/gbfs.json",
                  auth_url="https://sharedmobility.ch/v2/gbfs", raw_feeds=["free_bike_status"])


def test_parse_vehicles_real_fixture():
    v = parse_vehicles(fx("sm_free_bike_status.json"), "x")
    assert len(v) == 75
    nb = next(x for x in v if x.provider == "nextbike_ch")
    assert not nb.vehicle_id.startswith("nextbike_ch:")  # Präfix entfernt
    assert nb.vehicle_type == "150" and nb.lat and nb.lon
    assert {x.provider for x in v} == {"nextbike_ch", "bird-zurich", "voiscooters.com", "velospot"}


def test_provider_from_prefix_for_gbfs23():
    """GBFS 2.3 ohne provider_id-Feld: Anbieter aus 'anbieter:id' ableiten."""
    assert provider_of({}, "bird-zurich:abc", "x", {"bird-zurich"}) == "bird-zurich"
    assert provider_of({}, "unbekannt:abc", "x", {"bird-zurich"}) == "x"
    assert provider_of({"provider_id": "p"}, "q:1", "x") == "p"
    d = {"data": {"bikes": [{"bike_id": "bird-zurich:abc", "lat": 47.3, "lon": 8.5, "is_reserved": 0, "is_disabled": 0}]}}
    assert parse_vehicles(d, "x", {"bird-zurich"})[0].provider == "bird-zurich"


def test_parse_vehicles_robust():
    d = {"data": {"vehicles": [  # GBFS v3
        {"vehicle_id": "a", "lat": 47.1, "lon": 8.1, "is_reserved": True, "is_disabled": False, "current_range_meters": 1234},
        {"vehicle_id": "", "lat": 1, "lon": 1},
        {"vehicle_id": "b", "lat": 999, "lon": 8.1},
        "kaputt",
    ]}}
    v = parse_vehicles(d, "p")
    assert [x.vehicle_id for x in v] == ["a", "b"]
    assert v[0].reserved and v[0].range_m == 1234
    assert v[1].lat is None  # unplausible Koordinate verworfen


def test_parse_station_status():
    s = parse_station_status(fx("sm_station_status.json"), "x")
    assert len(s) == 24
    assert all(x.bikes >= 0 for x in s)
    vt = {"data": {"stations": [{"station_id": "1", "num_vehicles_available": 3, "vehicle_types_available": [
        {"vehicle_type_id": "p:ebike", "count": 2}, {"vehicle_type_id": "p:bike", "count": 1}], "provider_id": "p"}]}}
    assert json.loads(parse_station_status(vt, "x")[0].by_type) == {"bike": 1, "ebike": 2}


def test_gbfs_source_poll(fake):
    src = GbfsSource(SM, fetch=fake)
    snap = src.poll(fake.ts + 5)
    assert len(snap.vehicles) == 75 and len(snap.stations) == 24
    assert len(snap.station_info) == 24 and len(snap.providers) == 5
    assert snap.obs_ts == fake.ts  # Datenzeit = last_updated
    assert "free_bike_status" in snap.raw
    assert not any("Authorization" in h for _, h in fake.calls)  # ohne E-Mail: offener Endpunkt
    fake.calls.clear()
    snap2 = src.poll(fake.ts + 65)
    assert snap2.station_info is None  # Infos nur alle info_interval
    assert all("station_information" not in u for u, _ in fake.calls)


def test_auth_endpoint_with_fallback(fake):
    cfg = SourceConfig(**{**SM.__dict__, "auth": "ich@example.org"})
    src = GbfsSource(cfg, fetch=fake)
    src.poll(fake.ts)  # v2/gbfs existiert im Fake nicht → Fallback auf 2.0
    assert fake.calls[0] == ("https://sharedmobility.ch/v2/gbfs", {"Authorization": "ich@example.org"})
    assert src.fallback_reason and not src.use_auth
    assert all("Authorization" not in h for _, h in fake.calls[1:])


def test_auth_endpoint_used(fake):
    fake.docs["gbfs"] = fake.docs["gbfs.json"]
    src = GbfsSource(SourceConfig(**{**SM.__dict__, "auth": "ich@example.org"}), fetch=fake)
    src.poll(fake.ts)
    assert src.use_auth
    assert all(h.get("Authorization") == "ich@example.org" for _, h in fake.calls)


def test_parse_nextbike_live():
    v, s, info, types = parse_live(fx("nextbike_live.json"), {})
    assert all(x.provider == "nextbike_ch" for x in v)
    assert len(s) == len(info) >= 20
    on_station = [x for x in v if x.station_id]
    free = [x for x in v if not x.station_id]
    assert on_station and free
    assert all(x.vehicle_id.isdigit() for x in v)  # echte Velonummern
    with pytest.raises(http.FetchError):
        parse_live({"countries": []}, {})


def test_nextbike_source_types_from_full_feed(fake):
    cfg = SourceConfig(id="nextbike", kind="nextbike", url="https://x/nextbike-live.json?bikes=false",
                       full_url="https://x/nextbike-live.json", info_interval_s=3600)
    pl = fake.places()
    first = next(p for p in pl if p["bike_numbers"])
    first["bike_list"] = [{"number": n, "bike_type": 183} for n in first["bike_numbers"]]
    src = NextbikeSource(cfg, fetch=fake)
    snap = src.poll(1000)
    assert "bikes=false" not in fake.calls[0][0]
    assert snap.bike_types and all(t == "183" for t in snap.bike_types.values())
    del first["bike_list"]
    snap2 = src.poll(1060)
    assert "bikes=false" in fake.calls[1][0]
    assert next(v for v in snap2.vehicles if v.vehicle_id == first["bike_numbers"][0]).vehicle_type == "183"


def test_hot_days_minimum(tmp_path):
    from sharedmob.config import load_config

    cfgfile = tmp_path / "c.toml"
    cfgfile.write_text("[storage]\nhot_days = 1\n[tracking]\ntrip_link_max_h = 72\n")
    assert load_config(cfgfile, env={}).hot_days == 4
    cfgfile.write_text("[storage]\nhot_days = 10\n")
    assert load_config(cfgfile, env={}).hot_days == 10
    assert load_config(tmp_path / "fehlt.toml", env={}).hot_days == 4

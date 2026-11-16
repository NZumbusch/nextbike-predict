import pytest

from nextbike.feed import FeedError, fetch_feed, parse_feed
from tests.conftest import FIXTURE, places


def test_parse_real_fixture(cfg):
    f = parse_feed(FIXTURE, 126)
    assert len(f.stations) == 25
    by = {s.uid: s for s in f.stations}
    for s in cfg.stations:
        assert s.id in by, s.name
    assert by[70900].name == "Südpol / Arsenalstrasse 28"
    assert (by[70900].bikes, by[70900].ebikes) == (4, 4)  # 4× Typ 183
    assert by[3024979].ebikes == 2
    assert f.free_floating == 2
    assert f.bikes["400767"] == 70900
    assert f.bikes["401529"] == (47.04904, 8.31191)
    assert f.bike_types["400767"] == "183"
    assert len(f.bikes) == sum(len(p["bike_numbers"]) for p in places(FIXTURE))


def test_invalid_values_skipped(feed_json):
    p = places(feed_json)[0]
    p["bikes_available_to_rent"] = -1
    p["bikes"] = "x"
    assert p["uid"] not in {s.uid for s in parse_feed(feed_json, 126).stations}


@pytest.mark.parametrize("bad", [{}, None, {"countries": []}, {"countries": [{"cities": [{"uid": 126, "places": []}]}]}])
def test_broken_feed(bad):
    with pytest.raises(FeedError):
        parse_feed(bad, 126)


def test_fetch_retries_then_fails(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    calls = []

    def opener(req, timeout):
        calls.append(req.full_url)
        raise OSError("down")

    with pytest.raises(FeedError, match="down"):
        fetch_feed("https://x", 126, opener=opener)
    assert len(calls) == 2

from datetime import datetime, timezone

import pytest

from nextbike.config import Station
from nextbike.predict import SlotStat, predict, recommend
from nextbike.timeutil import BadInput, neighbor_slots, parse_at, slot_label, week_slot

NOW = datetime(2026, 11, 16, 13, 0, tzinfo=timezone.utc).timestamp()  # Mo 14:00 Zürich


def test_week_slot_dst():
    # Mo 07:50 lokal: Winter = 06:50 UTC, Sommer = 05:50 UTC
    assert week_slot(datetime(2026, 11, 16, 6, 50, tzinfo=timezone.utc).timestamp()) == 7 * 4 + 3
    assert week_slot(datetime(2026, 6, 15, 5, 50, tzinfo=timezone.utc).timestamp()) == 7 * 4 + 3
    # DST-Wechsel 29.03.2026: 00:59 UTC = 01:59 MEZ, 01:00 UTC = 03:00 MESZ (Sonntag)
    assert slot_label(week_slot(datetime(2026, 3, 29, 0, 59, tzinfo=timezone.utc).timestamp())) == "So 01:45"
    assert slot_label(week_slot(datetime(2026, 3, 29, 1, 0, tzinfo=timezone.utc).timestamp())) == "So 03:00"


def test_neighbors_wrap():
    assert neighbor_slots(0) == [671, 0, 1]
    assert neighbor_slots(671) == [670, 671, 0]


def test_parse_at():
    assert parse_at(None, NOW)[1] == 14 * 4
    dt, ws = parse_at("7:45", NOW)
    assert ws == 7 * 4 + 3 and dt.utcoffset().total_seconds() == 3600
    assert parse_at("2026-06-17T08:10", NOW)[1] == 2 * 96 + 32  # Mi
    assert parse_at("2026-06-17T06:10Z", NOW)[1] == 2 * 96 + 32
    for bad in ["25:00", "morgen", "2026-13-01T08:00"]:
        with pytest.raises(BadInput):
            parse_at(bad, NOW)


def test_predict_smoothing_and_min_obs():
    ws = 31
    stats = {30: SlotStat(3, 3, 9, 1, 1), 31: SlotStat(3, 2, 4), 32: SlotStat(4, 1, 1, 1, 2), 33: SlotStat(100, 0, 0)}
    p = predict(stats, ws, 5)
    assert (p["status"], p["n"], p["probability"], p["avg_bikes"], p["probability_ebike"]) == ("ok", 10, 0.6, 1.4, 0.2)
    assert p["slots_used"] == ["Mo 07:30", "Mo 07:45", "Mo 08:00"]
    q = predict({31: SlotStat(4, 4, 8)}, ws, 5)
    assert q["status"] == "zu_wenig_daten" and q["probability"] is None


def test_recommend():
    home = Station(1, "home", "Home", "start", True)
    fb = Station(2, "fb", "FB", "start", False, 150)
    ok = lambda p: {"status": "ok", "probability": p, "avg_bikes": 1}  # noqa: E731
    assert recommend([(home, ok(0.85)), (fb, ok(0.99))], 0.8)["alias"] == "home"
    r = recommend([(home, ok(0.4)), (fb, ok(0.9))], 0.8)
    assert r["alias"] == "fb" and "150 m" in r["text"]
    assert recommend([(home, {"status": "zu_wenig_daten", "probability": None})], 0.8)["station_id"] is None

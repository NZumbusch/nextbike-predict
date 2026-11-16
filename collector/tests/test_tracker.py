from sharedmob.model import Snapshot, StationObs, VehicleObs
from sharedmob.tracker import S_COLS, T_COLS, V_COLS, Tracker, haversine_m

T0 = 1_800_000_000


def V(vid, lat=47.0, lon=8.0, prov="p", station=None, **kw):
    return VehicleObs(prov, vid, lat, lon, kw.pop("vt", "bike"), station, **kw)


def snap(ts, vehicles=None, stations=None):
    return Snapshot("src", ts, ts, vehicles=vehicles, stations=stations)


def trips(ch):
    return [dict(zip(T_COLS, t)) for t in ch.trips]


def test_spells_only_on_change():
    t = Tracker("src")
    ch = t.apply(snap(T0, [V("a"), V("b")]))
    assert len(ch.v_open) == 2 and not ch.v_close
    for m in range(1, 180):  # 3 h stillstehen = keine einzige Änderung
        ch = t.apply(snap(T0 + 60 * m, [V("a"), V("b", lat=47.0 + 0.0001)]))  # 11 m GPS-Rauschen
        assert not ch.v_open and not ch.v_close
    assert t.open_v[("p", "a")].last_ts == T0 + 60 * 179


def test_start_reasons():
    t = Tracker("src")
    r = lambda ch: [dict(zip(V_COLS, x))["start_reason"] for x in ch.v_open]  # noqa: E731
    assert r(t.apply(snap(T0, [V("a")]))) == ["initial"]
    assert r(t.apply(snap(T0 + 60, [V("a"), V("b")]))) == ["appeared"]
    assert r(t.apply(snap(T0 + 120, [V("a", lat=47.01), V("b", reserved=True)]))) == ["moved", "changed"]


def test_ride_with_uncertainty_windows():
    t = Tracker("src")
    t.apply(snap(T0, [V("a")]))
    t.apply(snap(T0 + 60, [V("a")]))
    ch = t.apply(snap(T0 + 120, []))  # ausgeliehen
    assert ch.v_close == [(T0 + 60, T0 + 120, "gone", 1)]
    t.apply(snap(T0 + 180, []))
    ch = t.apply(snap(T0 + 600, [V("a", lat=47.01)]))  # ≈ 1.1 km weiter
    (tr,) = trips(ch)
    assert tr["kind"] == "ride"
    assert (tr["dep_ts"], tr["dep_next_ts"], tr["arr_prev_ts"], tr["arr_ts"]) == (T0 + 60, T0 + 120, T0 + 180, T0 + 600)
    assert 1100 < tr["distance_m"] < 1120
    assert tr["from_spell"] == 1 and tr["to_spell"] == 2


def test_round_trip_and_glitch():
    t = Tracker("src", round_trip_min_s=300)
    t.apply(snap(T0, [V("a")]))
    t.apply(snap(T0 + 60, []))
    ch = t.apply(snap(T0 + 120, [V("a")]))  # 1 Min weg: Feed-Aussetzer, keine Fahrt
    assert not ch.trips
    t.apply(snap(T0 + 180, []))
    ch = t.apply(snap(T0 + 900, [V("a")]))
    assert trips(ch)[0]["kind"] == "round_trip"


def test_visible_move_and_station_change():
    t = Tracker("src")
    t.apply(snap(T0, [V("a", station="s1")]))
    ch = t.apply(snap(T0 + 60, [V("a", lat=47.005, station="s2")]))
    assert ch.v_close[0][2] == "moved"
    assert trips(ch)[0]["kind"] == "visible_move" and trips(ch)[0]["to_station"] == "s2"
    ch = t.apply(snap(T0 + 120, [V("a", lat=47.005, station="s2", reserved=True)]))
    assert ch.v_close[0][2] == "changed" and not ch.trips  # Reservation: neuer Zustand, keine Fahrt


def test_auto_position_identity_for_rotating_ids():
    t = Tracker("src")
    fleet = lambda gen, n=30: [V(f"{gen}-{i}", lat=47 + i * 0.001, prov="bird") for i in range(n)]  # noqa: E731
    t.apply(snap(T0, fleet("x")))
    for i, gen in enumerate(["y", "z"]):  # Neuvergabe 1 und 2: bleibt bei ID, aber ohne Schein-Fahrten
        ch = t.apply(snap(T0 + 60 * (i + 1), fleet(gen)))
        assert ch.rekeyed == ["bird"] and not ch.mode_switches and not ch.trips
        assert {c[2] for c in ch.v_close} == {"rekey"}
        assert {dict(zip(V_COLS, o))["start_reason"] for o in ch.v_open} == {"rekey"}
    ch = t.apply(snap(T0 + 180, fleet("q")))  # 3. Neuvergabe in 24 h ⇒ Positions-Identität
    assert ch.mode_switches == [("bird", "pos")]
    ch = t.apply(snap(T0 + 240, fleet("r")))  # ab jetzt stabil über die Position
    assert not ch.v_open and not ch.v_close
    moved = fleet("w")
    moved[0] = V("w-0", lat=47.5, prov="bird")
    ch = t.apply(snap(T0 + 300, moved))
    assert len(ch.v_open) == 1 and len(ch.v_close) == 1 and not ch.trips  # Abfahrt + Ankunft, keine erfundene Fahrt
    assert dict(zip(V_COLS, ch.v_open[0]))["vehicle_id"].startswith("@47.50000,8.00000")


def test_colocated_vehicles_in_position_mode():
    t = Tracker("src", id_strategy="position")
    two = [V("1", prov="q"), V("2", prov="q")]
    ch = t.apply(snap(T0, two))
    assert len(ch.v_open) == 2  # gleiche Koordinate → #0 und #1
    ch = t.apply(snap(T0 + 60, [V("3", prov="q")]))
    assert len(ch.v_close) == 1 and not ch.v_open


def test_provider_outage_keeps_spells_open():
    t = Tracker("src", provider_absent_close_s=3600)
    p1 = [V(f"a{i}", lat=47 + i / 100, prov="p1") for i in range(12)]
    t.apply(snap(T0, p1 + [V("b", prov="p2")]))
    ch = t.apply(snap(T0 + 60, [V("b", prov="p2")]))  # p1 fehlt komplett (Teilausfall)
    assert not ch.v_close
    ch = t.apply(snap(T0 + 120, p1 + [V("b", prov="p2")]))
    assert not ch.v_open and not ch.v_close and not ch.trips  # keine 12 Scheinfahrten
    t.apply(snap(T0 + 180, [V("b", prov="p2")]))
    ch = t.apply(snap(T0 + 180 + 3600, [V("b", prov="p2")]))  # nach 1 h endgültig weg
    assert [c[2] for c in ch.v_close] == ["gone"] * 12


def test_stale_feed_is_ignored():
    t = Tracker("src")
    t.apply(snap(T0, [V("a")]))
    assert t.apply(snap(T0, [])) is None  # gleiche Datenzeit ⇒ nichts schliessen
    assert ("p", "a") in t.open_v


def test_station_spells():
    t = Tracker("src")
    S = lambda n, sid="s": StationObs("p", sid, n)  # noqa: E731
    ch = t.apply(snap(T0, stations=[S(3), S(1, "t")]))
    assert len(ch.s_open) == 2
    ch = t.apply(snap(T0 + 60, stations=[S(3), S(1, "t")]))
    assert not ch.s_open and not ch.s_close
    ch = t.apply(snap(T0 + 120, stations=[S(2)]))
    assert sorted(c[2] for c in ch.s_close) == ["changed", "gone"]
    row = dict(zip(S_COLS, ch.s_open[0]))
    assert (row["bikes"], row["start_prev_ts"]) == (2, T0 + 60)


def test_restore_continues_trip_after_restart():
    t = Tracker("src")
    t.apply(snap(T0, [V("a")]))
    ch = t.apply(snap(T0 + 60, []))
    rows_open = []
    closed = {**dict(zip(V_COLS, (1, "src", "p", "a", None, "id", "bike", 47.0, 8.0, None, 0, 0, None, T0, None, "initial"))),
              "end_ts": T0, "end_next_ts": T0 + 60, "end_reason": "gone"}
    t2 = Tracker("src")
    t2.restore(iter(rows_open), iter([]), iter([closed]), T0 + 60, None, {})
    ch = t2.apply(snap(T0 + 900, [V("a", lat=47.02)]))
    assert trips(ch)[0]["kind"] == "ride"


def test_haversine():
    assert round(haversine_m(47.0, 8.0, 47.01, 8.0)) == 1112
    assert haversine_m(None, 8, 47, 8) is None


def test_single_reshuffle_keeps_id_tracking():
    """Seltene Komplett-Neuvergabe (z. B. 1× täglich) darf exakte Fahrten nicht dauerhaft abschalten."""
    t = Tracker("src")
    fleet = lambda gen: [V(f"{gen}-{i}", lat=47 + i * 0.001, prov="velo") for i in range(25)]  # noqa: E731
    t.apply(snap(T0, fleet("a")))
    t.apply(snap(T0 + 60, fleet("b")))  # einmalige Neuvergabe
    assert t.mode_for("velo") == "id"
    b = fleet("b")
    t.apply(snap(T0 + 120, b[1:]))                       # b-0 ausgeliehen
    ch = t.apply(snap(T0 + 900, [V("b-0", lat=47.3, prov="velo")] + b[1:]))
    assert trips(ch)[0]["kind"] == "ride"
    t.apply(snap(T0 + 2 * 86400, fleet("c")))          # nächste Neuvergabe erst nach > 24 h
    assert t.mode_for("velo") == "id"


def test_position_mode_tolerates_gps_jitter():
    """Bird/Lime: Positionen schwanken um wenige Meter – darf keine Abfahrt/Ankunft erzeugen."""
    t = Tracker("src", id_strategy="position", move_tolerance_m=25)
    fleet = [V(f"x{i}", lat=47 + i * 0.01, prov="b") for i in range(10)]
    t.apply(snap(T0, fleet))
    jitter = [V(f"y{i}", lat=47 + i * 0.01 + 0.00002, lon=8.00003, prov="b") for i in range(10)]  # ≈ 3 m
    ch = t.apply(snap(T0 + 60, jitter))
    assert not ch.v_open and not ch.v_close
    moved = jitter[:9] + [V("z", lat=47.2, prov="b")]  # eins wirklich weg, eins neu
    ch = t.apply(snap(T0 + 120, moved))
    assert len(ch.v_close) == 1 and len(ch.v_open) == 1


def test_position_mode_two_vehicles_same_spot():
    t = Tracker("src", id_strategy="position")
    t.apply(snap(T0, [V("1", prov="q"), V("2", prov="q")]))
    ch = t.apply(snap(T0 + 60, [V("3", prov="q", lat=47.00001), V("4", prov="q")]))
    assert not ch.v_open and not ch.v_close

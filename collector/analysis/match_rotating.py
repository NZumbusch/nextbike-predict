"""Fahrten für Anbieter mit rotierenden IDs rekonstruieren (Heuristik) – und an der Wahrheit messen.

Idee: Ein Fahrzeug verschwindet an A (Abfahrt) und taucht mit neuer ID an B auf (Ankunft).
Kandidaten = gleicher Anbieter und Fahrzeugtyp, Ankunft 1–max_gap Min nach der Abfahrt,
Distanz mit plausibler Geschwindigkeit erreichbar. Zuordnung 1:1, gierig nach kleinster Zeitlücke.

Validierung: nextbike steckt im Aggregator (rotierende IDs) UND kommt direkt mit festen Velonummern.
  python match_rotating.py ../archive --validate
vergleicht die rekonstruierten nextbike_ch-Fahrten mit den exakten Fahrten der direkten Quelle.

  python match_rotating.py ../archive --provider lime_zurich --out lime_trips.parquet
"""
from __future__ import annotations

import argparse

import load

HAVERSINE = """2 * 6371000 * asin(sqrt(
    pow(sin(radians({b}.lat - {a}.lat) / 2), 2) +
    cos(radians({a}.lat)) * cos(radians({b}.lat)) * pow(sin(radians({b}.lon - {a}.lon) / 2), 2)))"""


def candidates(con, source: str, provider: str, max_gap_min: int, max_speed_kmh: float):
    dist = HAVERSINE.format(a="d", b="a")
    return con.sql(f"""
        WITH d AS (SELECT row_number() OVER (ORDER BY gone_ts) AS did, * FROM departure
                   WHERE source = '{source}' AND provider = '{provider}' AND lat IS NOT NULL),
             a AS (SELECT row_number() OVER (ORDER BY seen_ts) AS aid, * FROM arrival
                   WHERE source = '{source}' AND provider = '{provider}' AND lat IS NOT NULL)
        SELECT did, aid, d.gone_ts AS dep_ts, a.seen_ts AS arr_ts, d.lat AS from_lat, d.lon AS from_lon,
               a.lat AS to_lat, a.lon AS to_lon, d.station_id AS from_station, a.station_id AS to_station,
               d.vehicle_type, {dist} AS distance_m, (a.seen_ts - d.gone_ts) AS gap_s
        FROM d JOIN a ON coalesce(d.vehicle_type, '') = coalesce(a.vehicle_type, '')
                     AND a.seen_ts > d.gone_ts AND a.seen_ts - d.gone_ts <= {max_gap_min * 60}
        WHERE {dist} <= {max_speed_kmh / 3.6} * (a.seen_ts - d.gone_ts + 60) + 150
    """).df()


def greedy(c):
    """1:1-Zuordnung: kleinste Zeitlücke zuerst (einfache, nachvollziehbare Basis-Heuristik)."""
    c = c.sort_values(["gap_s", "distance_m"])
    used_d, used_a, keep = set(), set(), []
    for row in c.itertuples(index=False):
        if row.did in used_d or row.aid in used_a:
            continue
        used_d.add(row.did)
        used_a.add(row.aid)
        keep.append(row)
    import pandas as pd

    return pd.DataFrame(keep, columns=c.columns)


def validate(con, args) -> None:
    rec = greedy(candidates(con, "sharedmobility", "nextbike_ch", args.max_gap_min, args.max_speed_kmh))
    truth = con.sql(f"""SELECT dep_next_ts AS dep_ts, arr_ts, from_lat, from_lon, to_lat, to_lon FROM trip
                        WHERE source = 'nextbike' AND provider = 'nextbike_ch' AND kind = 'ride'
                          AND arr_ts - dep_next_ts <= {args.max_gap_min * 60}""").df()
    if truth.empty or rec.empty:
        print(f"Zu wenig Daten (exakt: {len(truth)}, rekonstruiert: {len(rec)})")
        return
    con.register("rec", rec)
    con.register("truth", truth)
    hit = con.sql("""
        SELECT count(DISTINCT t.rowid) FROM (SELECT row_number() OVER () AS rowid, * FROM truth) t
        JOIN rec r ON abs(r.dep_ts - t.dep_ts) <= 120 AND abs(r.arr_ts - t.arr_ts) <= 120
          AND abs(r.from_lat - t.from_lat) < 0.0005 AND abs(r.from_lon - t.from_lon) < 0.0007
          AND abs(r.to_lat - t.to_lat) < 0.0005 AND abs(r.to_lon - t.to_lon) < 0.0007
    """).fetchone()[0]
    print(f"nextbike_ch – exakte Fahrten (direkte Quelle): {len(truth)}")
    print(f"rekonstruiert aus rotierenden IDs:           {len(rec)}")
    print(f"davon richtig (Start/Ziel ±50 m, Zeit ±2 min): {hit}")
    print(f"Trefferquote (Recall):    {hit / len(truth):.0%}")
    print(f"Genauigkeit (Precision):  {hit / len(rec):.0%}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("archive")
    ap.add_argument("--provider", default="nextbike_ch")
    ap.add_argument("--source", default="sharedmobility")
    ap.add_argument("--max-gap-min", type=int, default=90)
    ap.add_argument("--max-speed-kmh", type=float, default=30)
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--out")
    args = ap.parse_args()
    con = load.connect(args.archive)
    if args.validate:
        validate(con, args)
        return
    trips = greedy(candidates(con, args.source, args.provider, args.max_gap_min, args.max_speed_kmh))
    print(trips.describe() if not trips.empty else "keine Kandidaten")
    if args.out:
        trips.to_parquet(args.out)
        print(f"→ {args.out}")


if __name__ == "__main__":
    main()

"""Beispiel-Auswertungen. Aufruf:  python examples.py pfad/zu/data/archive

Grundlage für "wiederkehrende vs. überraschende Fahrten":
  - Exakte Fahrten (Tabelle trip) gibt es für Anbieter mit stabiler ID (nextbike direkt, ggf. weitere,
    siehe Statusseite "ID kommt zurück").
  - Für alle Anbieter gibt es Abfahrten (departure) und Ankünfte (arrival) mit Ort und Zeit.
"""
import sys

import load

con = load.connect(sys.argv[1] if len(sys.argv) > 1 else "../data/archive")


def show(title: str, sql: str, n: int = 15) -> None:
    print(f"\n=== {title}")
    con.sql(sql).show(max_rows=n, max_width=200)


show("Datenüberblick pro Anbieter", """
    SELECT source, provider, any_value(id_mode) AS identitaet, count(*) AS zustaende,
           count(*) FILTER (start_reason = 'appeared') AS ankuenfte,
           count(*) FILTER (end_reason = 'gone') AS abfahrten
    FROM vehicle_spell GROUP BY ALL ORDER BY zustaende DESC""")

show("Exakte Fahrten nach Anbieter und Art", """
    SELECT source, provider, kind, count(*) AS fahrten, round(median(duration_min_lower), 1) AS median_min,
           round(median(distance_m)) AS median_m
    FROM trip GROUP BY ALL ORDER BY fahrten DESC""")

show("Fahrten pro Stunde (lokal)", """
    SELECT hour(dep_local) AS stunde, count(*) AS fahrten,
           count(*) FILTER (provider LIKE 'nextbike%') AS nextbike
    FROM trip WHERE kind = 'ride' GROUP BY 1 ORDER BY 1""", 24)

show("Häufigste Verbindungen (Stationen, mit Namen)", """
    SELECT a.name AS von, b.name AS nach, count(*) AS fahrten, round(avg(duration_min_lower), 1) AS avg_min
    FROM trip t
    LEFT JOIN station_info a ON a.source = t.source AND a.provider = t.provider AND a.station_id = t.from_station
    LEFT JOIN station_info b ON b.source = t.source AND b.provider = t.provider AND b.station_id = t.to_station
    WHERE kind = 'ride' AND from_station IS NOT NULL AND to_station IS NOT NULL AND from_station <> to_station
    GROUP BY ALL ORDER BY fahrten DESC""")

show("Abfahrten pro Anbieter und Stunde (alle Anbieter, auch mit rotierenden IDs)", """
    SELECT source, provider, hour(gone_local) AS stunde, count(*) AS abfahrten
    FROM departure GROUP BY ALL ORDER BY abfahrten DESC""")

# nextbike ist doppelt vorhanden (direkt = source 'nextbike' mit festen Velonummern, und im Aggregator).
# Für Fahrten nextbike immer aus source = 'nextbike' nehmen.

# Wiederkehrend vs. überraschend: gleiche Verbindung (Station bzw. ≈250-m-Zelle) im gleichen
# Zeitfenster (Werktag/Wochenende × Stunde) in mehreren verschiedenen Wochen ⇒ wiederkehrend.
# Hinweis: Für eine saubere Analyse nur vergangene Wochen als Referenz verwenden (kein Blick in die Zukunft).
show("Wiederkehrend vs. überraschend (Beispiel-Definition)", """
    WITH t AS (
      SELECT *,
        coalesce(from_station, printf('%.4f,%.4f', round(from_lat * 400) / 400, round(from_lon * 400) / 400)) AS o,
        coalesce(to_station,   printf('%.4f,%.4f', round(to_lat * 400) / 400,   round(to_lon * 400) / 400))   AS d,
        CASE WHEN isodow(dep_local) >= 6 THEN 'Wochenende' ELSE 'Werktag' END AS tagtyp,
        hour(dep_local) AS h,
        date_trunc('week', dep_local) AS woche
      FROM trip WHERE kind = 'ride'
    ), muster AS (
      SELECT provider, o, d, tagtyp, h, count(DISTINCT woche) AS wochen, count(*) AS n FROM t GROUP BY ALL
    )
    SELECT CASE WHEN wochen >= 3 THEN 'wiederkehrend (≥ 3 Wochen)'
                WHEN n >= 2 THEN 'gelegentlich'
                ELSE 'einmalig / überraschend' END AS klasse,
           sum(n) AS fahrten
    FROM muster GROUP BY 1 ORDER BY 2 DESC""")

-- DuckDB-Sichten auf das Parquet-Archiv. {ARCHIVE} wird durch den Archiv-Pfad ersetzt (siehe load.py).
-- Zeiten sind Unix-Sekunden (UTC); to_timestamp(ts) AT TIME ZONE 'Europe/Zurich' für lokale Zeit.

-- Zustände: abgeschlossene (archiviert) + aktuell offene
CREATE OR REPLACE VIEW vehicle_spell AS
  SELECT * EXCLUDE (date) FROM read_parquet('{ARCHIVE}/vehicle_spell/*/*.parquet', hive_partitioning = true, union_by_name = true)
  UNION ALL BY NAME
  SELECT * FROM read_parquet('{ARCHIVE}/open/vehicle_spell.parquet');

CREATE OR REPLACE VIEW station_spell AS
  SELECT * EXCLUDE (date) FROM read_parquet('{ARCHIVE}/station_spell/*/*.parquet', hive_partitioning = true, union_by_name = true)
  UNION ALL BY NAME
  SELECT * FROM read_parquet('{ARCHIVE}/open/station_spell.parquet');

-- Exakte Fahrten (gleiche Fahrzeug-ID: nextbike direkt, Anbieter ohne ID-Rotation)
CREATE OR REPLACE VIEW trip AS
  SELECT * EXCLUDE (date),
         to_timestamp((dep_ts + dep_next_ts) / 2) AT TIME ZONE 'Europe/Zurich' AS dep_local,
         to_timestamp(arr_ts) AT TIME ZONE 'Europe/Zurich' AS arr_local,
         (arr_ts - dep_next_ts) / 60.0 AS duration_min_lower,      -- untere Schranke
         (coalesce(arr_prev_ts, arr_ts) - dep_ts) / 60.0 AS duration_min_upper
  FROM read_parquet('{ARCHIVE}/trip/*/*.parquet', hive_partitioning = true, union_by_name = true);

-- Abfahrten und Ankünfte – für ALLE Anbieter, auch mit rotierenden IDs
CREATE OR REPLACE VIEW departure AS
  SELECT source, provider, vehicle_type, lat, lon, station_id, id_mode,
         end_ts AS last_seen_ts, end_next_ts AS gone_ts,
         to_timestamp(end_next_ts) AT TIME ZONE 'Europe/Zurich' AS gone_local
  FROM vehicle_spell WHERE end_reason IN ('gone', 'moved');

CREATE OR REPLACE VIEW arrival AS
  SELECT source, provider, vehicle_type, lat, lon, station_id, id_mode,
         start_prev_ts AS prev_poll_ts, start_ts AS seen_ts,
         to_timestamp(start_ts) AT TIME ZONE 'Europe/Zurich' AS seen_local
  FROM vehicle_spell WHERE start_reason IN ('appeared', 'moved');

-- Stammdaten (aktueller Stand)
CREATE OR REPLACE VIEW station_info  AS SELECT * FROM read_parquet('{ARCHIVE}/dim/station_info.parquet');
CREATE OR REPLACE VIEW provider_info AS SELECT * FROM read_parquet('{ARCHIVE}/dim/provider_info.parquet');
CREATE OR REPLACE VIEW bike_info     AS SELECT * FROM read_parquet('{ARCHIVE}/dim/bike_info.parquet');
CREATE OR REPLACE VIEW provider_stat AS
  SELECT * EXCLUDE (date) FROM read_parquet('{ARCHIVE}/provider_stat/*/*.parquet', hive_partitioning = true, union_by_name = true);
CREATE OR REPLACE VIEW poll AS
  SELECT * EXCLUDE (date) FROM read_parquet('{ARCHIVE}/poll/*/*.parquet', hive_partitioning = true, union_by_name = true);

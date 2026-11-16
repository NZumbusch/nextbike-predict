-- Rohbeobachtungen der eigenen Stationen (1 Zeile pro Station und Cron-Lauf).
-- wslot = lokaler Wochen-Slot (Europe/Zurich): Wochentag(Mo=0..So=6)*96 + 15-Min-Slot (0..95).
-- Primärschlüssel (station_id, wslot, ts) WITHOUT ROWID:
--   * Prognose-Abfragen (Station × Slot) sind reine Range-Scans auf dem PK,
--   * kein Zusatzindex ⇒ jeder Insert kostet genau 1 "row written",
--   * INSERT OR IGNORE verhindert Doppelzeilen bei wiederholten Cron-Läufen.
CREATE TABLE IF NOT EXISTS obs (
  station_id INTEGER NOT NULL,
  wslot      INTEGER NOT NULL CHECK (wslot BETWEEN 0 AND 671),
  ts         INTEGER NOT NULL,              -- Unix-Sekunden UTC, auf 5 Min abgerundet
  bikes      INTEGER NOT NULL CHECK (bikes >= 0),   -- ausleihbare Velos total
  ebikes     INTEGER CHECK (ebikes >= 0),           -- davon E-Bikes (NULL = unbekannt)
  free_racks INTEGER,                               -- NULL bei virtuellen Stationen ohne Ständer
  PRIMARY KEY (station_id, wslot, ts)
) WITHOUT ROWID;

-- Verdichtete Daten (Rohdaten älter als RAW_RETENTION_DAYS), Station × Wochentag × 15-Min-Slot.
CREATE TABLE IF NOT EXISTS agg (
  station_id    INTEGER NOT NULL,
  wslot         INTEGER NOT NULL,
  n             INTEGER NOT NULL,   -- Anzahl Beobachtungen
  n_avail       INTEGER NOT NULL,   -- davon mit >= 1 Velo
  sum_bikes     INTEGER NOT NULL,
  n_ebike_avail INTEGER NOT NULL,   -- davon mit >= 1 E-Bike
  sum_ebikes    INTEGER NOT NULL,
  first_ts      INTEGER NOT NULL,
  last_ts       INTEGER NOT NULL,
  PRIMARY KEY (station_id, wslot)
) WITHOUT ROWID;

-- Kompakter Snapshot des ganzen Luzerner Netzes pro Cron-Lauf (für spätere Analysen:
-- Verteilung, Umverteilung). data = JSON [[uid, bikes, ebikes], ...] ≈ 2 KB.
CREATE TABLE IF NOT EXISTS snap (
  ts            INTEGER PRIMARY KEY,  -- Unix-Sekunden UTC, auf 5 Min abgerundet
  n_stations    INTEGER NOT NULL,
  total_bikes   INTEGER NOT NULL,
  total_ebikes  INTEGER NOT NULL,
  free_floating INTEGER NOT NULL,     -- Velos ausserhalb von Stationen
  booked        INTEGER NOT NULL,     -- reservierte Velos (Netz)
  data          TEXT NOT NULL
);

-- Stammdaten aller Stationen (1× täglich aktualisiert).
CREATE TABLE IF NOT EXISTS stations (
  uid        INTEGER PRIMARY KEY,
  number     INTEGER,
  name       TEXT NOT NULL,
  lat        REAL,
  lng        REAL,
  place_type TEXT,
  first_seen INTEGER NOT NULL,
  last_seen  INTEGER NOT NULL
);

-- Kleiner Key-Value-Speicher (z. B. letzte Wartung, letzter Fehler).
CREATE TABLE IF NOT EXISTS meta (
  k TEXT PRIMARY KEY,
  v TEXT NOT NULL
);

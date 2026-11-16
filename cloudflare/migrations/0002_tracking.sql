-- v2: Einzelvelo-Tracking und minutengenaues Änderungs-Log.
--
-- log: 1 Zeile pro Abruf, aber NUR wenn sich etwas geändert hat (sonst gar keine Zeile).
--   kind 'K' = Keyframe (vollständiger Zustand, alle KEYFRAME_HOURS Stunden)
--   kind 'D' = Delta seit prev_ts
--   st = JSON [[uid, verfügbar, davon_E, reserviert, aktiv], …]   ([uid] = Station verschwunden)
--   bk = JSON [[velonummer, von, nach], …]  Ort = Station-uid | [lat, lng] (frei abgestellt) | 0 (nicht sichtbar = ausgeliehen/Depot)
CREATE TABLE IF NOT EXISTS log (
  ts      INTEGER PRIMARY KEY,              -- Unix-s UTC, auf Minute abgerundet
  prev_ts INTEGER,                          -- vorheriger erfolgreicher Abruf (Lücken erkennbar)
  kind    TEXT NOT NULL CHECK (kind IN ('K', 'D')),
  n_st    INTEGER NOT NULL,
  n_bk    INTEGER NOT NULL,
  st      TEXT NOT NULL,
  bk      TEXT NOT NULL
);

-- Velo-Stammdaten (Typ aus dem täglichen Vollabruf), 1× täglich aktualisiert.
CREATE TABLE IF NOT EXISTS bikes (
  number     TEXT PRIMARY KEY,
  type       TEXT,
  first_seen INTEGER NOT NULL,
  last_seen  INTEGER NOT NULL
);

-- Aktueller Zustand (genau 1 Zeile): Basis für das nächste Delta und für /now.
CREATE TABLE IF NOT EXISTS state (
  k    INTEGER PRIMARY KEY CHECK (k = 1),
  data TEXT NOT NULL
);

-- Die Tabelle snap aus v1 wird nicht mehr beschrieben (Inhalt bleibt erhalten und exportierbar).

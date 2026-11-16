# nextbike-predict 🚲 → Shared-Mobility-Daten Schweiz

Zwei unabhängige Teile:

| | [`collector/`](collector/README.md) – **Datensammlung (Hauptprojekt)** | [`cloudflare/`](cloudflare/README.md) – persönliche Prognose |
|---|---|---|
| Zweck | möglichst vollständige Rohdaten für die Analyse: einzelne Fahrzeuge, Fahrten, Stationsbestände | „Steht um 07:45 an meiner Station ein Velo?“ fürs Morgenbriefing |
| Abdeckung | **ganze Schweiz**: 46 Anbieter über sharedmobility.ch (BFE) + nextbike direkt | nextbike Luzern (eigene Stationen + ganzes Netz) |
| Läuft auf | Raspberry Pi 4/5 (Docker), Upload zu Google Drive | Cloudflare Worker + D1 (Free Plan) |
| Takt | 1 Min | 1 Min, nachts 5 Min |
| Speicherung | Zustandsintervalle in SQLite → tägliches Parquet-Archiv + Rohdaten alle 6 h | Delta-Log mit Keyframes in D1 |
| Stack | Python 3.12, SQLite, pyarrow, Flask/waitress, rclone, pytest; Analyse mit DuckDB | TypeScript, wrangler, vitest |

## Wichtigste Erkenntnisse zu den Daten (am echten Feed geprüft)

- **sharedmobility.ch** (`https://sharedmobility.ch/gbfs.json`) bündelt ≈ 23 000 Fahrzeuge und
  ≈ 12 600 Stationen von 46 Anbietern. Lizenz: „Open use. Must provide the source.“
- **GBFS wechselt die Fahrzeug-ID nach jeder Fahrt** (Datenschutzregel des Standards); Bird wechselt sogar jede
  Minute alle IDs. Abfahrten und Ankünfte lassen sich für alle erfassen, Einzelfahrten nur dort, wo die ID stabil bleibt.
- **nextbike direkt** (`maps2.nextbike.net/maps/nextbike-live.json?domains=ch,ms`) liefert **feste
  Velonummern** ⇒ exakte Einzelfahrten für nextbike Schweiz und invia.

Quelle der Daten: sharedmobility.ch – Bundesamt für Energie (BFE), <https://github.com/SFOE/sharedmobility>; nextbike GmbH.

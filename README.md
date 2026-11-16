# nextbike-predict 🚲 Luzern

Velo-Verfügbarkeit für **nextbike Luzern** sammeln, vorhersagen und analysieren: Wie wahrscheinlich
steht vor der Vorlesung ein Velo an meiner Station? Wie verteilen sich die Velos übers Netz, wann wird
umverteilt, welche Strecken werden gefahren?

Zwei unabhängige Varianten mit derselben Datenquelle und derselben API (`/now`, `/predict`, `/plan`),
das Morgenbriefing kann also beide nutzen:

| | [`cloudflare/`](cloudflare/README.md) | [`raspberry-pi/`](raspberry-pi/README.md) |
|---|---|---|
| Läuft auf | Cloudflare Worker + D1 (Free Plan) | Raspberry Pi (oder jeder Rechner) mit Docker |
| Kosten | 0 | Strom (≈ 2–4 W) |
| Erreichbar | überall (HTTPS, Token) | im WLAN (optional Token) |
| Abruf-Takt | 1 Min, nachts (01–05 Uhr) 5 Min | 1 Min (konfigurierbar) |
| Gespeichert | Delta-Log aller Stationen **und** jedes einzelnen Velos (+ Keyframes), Prognose-Daten der eigenen Stationen | alles normalisiert: Bestände aller Stationen (5-Min-Raster und jede Änderung), jede Velobewegung, erkannte Fahrten |
| Prognose für | eigene Stationen | **jede** Station |
| Analyse | CSV-Export + Rekonstruktion (Fahrten, Bewegungen) per Script | direkt in der Web-GUI (Verläufe, Wochenmuster, Karte, Fahrten, Umverteilungen) + CSV/SQLite-Download |
| Grenzen | D1: 100 000 Writes/Tag, 500 MB pro DB → Delta-Speicherung, reicht ≈ 3 Jahre | SD-Karte/SSD |
| Backups | `wrangler d1 export` / Export-Script | täglich automatisch, Upload zu Google Drive (rclone) |
| Stack | TypeScript, wrangler, vitest | Python 3.12, SQLite, Flask/waitress, Docker Compose, rclone, pytest |

**Welche nehmen?** Wenn du keinen Pi hast oder die Prognose unterwegs brauchst: Cloudflare. Wenn du
möglichst viel analysieren willst: Pi. Beide parallel laufen zu lassen schadet nicht, dann ist die
Cloudflare-Version gleichzeitig ein unabhängiges Backup der Rohdaten.

## Datenquelle (am echten Feed geprüft)

- `https://maps2.nextbike.net/maps/nextbike-live.json?city=126` (öffentlich, kein Login).
  `api.nextbike.net` leitet per 301 dorthin um.
- city **126** = Luzern, inkl. Südpol/Kriens-Allmend. Mit `&bikes=false` ≈ 51 KB, mit Velo-Liste ≈ 115 KB.
- Pro Station `bike_numbers` (Velonummern) → einzelne Velos lassen sich verfolgen. Frei abgestellte Velos
  erscheinen als eigene „places“ mit Koordinaten.
- Velotypen (GBFS `vehicle_types`): `150`/`196` Velo, `183` E-Bike, `371` E-Cargobike.
- Stichprobe Montagnachmittag: 1–12 Velobewegungen pro Minute im ganzen Netz, ≈ 350 Velos, ≈ 107 Stationen.

## Konfigurierte Stationen

| Alias | uid | Name | Rolle |
|---|---|---|---|
| `home` | 3024956 | Neustadtstrasse 10 | Start ★ |
| `moosstrasse` | 39638 | Bundesplatz / Moosstrasse | Ausweich Start (157 m) |
| `zentralstrasse` | 34397 | Bundesplatz / Zentralstrasse 1 | Ausweich Start (200 m) |
| `suedpol` | 70900 | Südpol / Arsenalstrasse 28 | Ziel ★ |
| `messe` | 3024979 | Messe Allmend / Horwerstrasse | Ausweich Ziel (460 m) |

Ändern: Cloudflare in `cloudflare/src/config.ts`, Pi in `raspberry-pi/config.toml`.

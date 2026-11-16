/**
 * Konfiguration: Feed, Stationen, Schwellwerte.
 *
 * Stationen ändern: `id` ist die nextbike-`uid` einer Station aus
 *   https://maps2.nextbike.net/maps/nextbike-live.json?city=126&bikes=false
 * (Feld `countries[].cities[].places[].uid`). Siehe README → "Stationen ändern".
 */

/** nextbike city-ID von Luzern (Netz "vdl", Domain "ch"). Südpol/Kriens-Allmend gehört auch dazu. */
export const CITY_ID = 126;

/**
 * Gefilterter Feed. `api.nextbike.net` antwortet mit 301 → maps2.nextbike.net,
 * deshalb direkt maps2 (spart einen Roundtrip). `bikes=false` lässt die
 * Einzel-Velo-Liste weg (≈51 KB statt ≈115 KB) – `bike_types` bleibt erhalten.
 */
export const FEED_URL = `https://maps2.nextbike.net/maps/nextbike-live.json?city=${CITY_ID}&bikes=false`;

/** Vollabruf inkl. Velo-Liste (≈115 KB) – nur 1× täglich für die Velotypen. */
export const FEED_URL_FULL = `https://maps2.nextbike.net/maps/nextbike-live.json?city=${CITY_ID}`;

/**
 * Abruf-Takt. Der Cron feuert jede Minute; ob wirklich abgerufen wird, entscheidet der Code.
 * Nachts (NIGHT_FROM_H bis NIGHT_TO_H Zürcher Zeit) passiert wenig ⇒ seltener abrufen.
 */
export const DAY_INTERVAL_MIN = 1;
export const NIGHT_INTERVAL_MIN = 5;
export const NIGHT_FROM_H = 1;
export const NIGHT_TO_H = 5;

/** Alle wie viele Stunden ein vollständiger Keyframe ins Log geschrieben wird. */
export const KEYFRAME_HOURS = 6;

/**
 * Velo-Typen mit Elektroantrieb (aus GBFS vehicle_types, nextbike_ch):
 *   150 Fahrrad (human), 196 Fahrrad/SMARTbike (human),
 *   183 E-Bike (electric_assist), 371 E-Cargobike (electric_assist)
 */
export const EBIKE_TYPES: ReadonlySet<string> = new Set(["183", "371"]);

/** Plausibilitätsgrenze: weniger Stationen im Feed ⇒ Feed kaputt, nichts schreiben. */
export const MIN_PLACES = 20;

export type Group = "start" | "ziel";

export interface StationConfig {
  id: number;
  alias: string;
  name: string;
  group: Group;
  /** true = Stammstation der Gruppe, sonst Ausweichstation */
  primary: boolean;
  /** Fussweg-Distanz (Luftlinie) zur Stammstation in Metern, nur Info */
  distance_m?: number;
}

export const STATIONS: readonly StationConfig[] = [
  { id: 3024956, alias: "home", name: "Neustadtstrasse 10", group: "start", primary: true },
  { id: 39638, alias: "moosstrasse", name: "Bundesplatz / Moosstrasse", group: "start", primary: false, distance_m: 157 },
  { id: 34397, alias: "zentralstrasse", name: "Bundesplatz / Zentralstrasse 1", group: "start", primary: false, distance_m: 200 },
  { id: 70900, alias: "suedpol", name: "Südpol / Arsenalstrasse 28", group: "ziel", primary: true },
  { id: 3024979, alias: "messe", name: "Messe Allmend / Horwerstrasse", group: "ziel", primary: false, distance_m: 460 },
];

/** Prognose: mindestens so viele Beobachtungen (über ±1 Slot), sonst "zu wenig Daten". */
export const MIN_OBS = 5;

/** /plan: Stammstation gilt als "sicher genug" ab dieser Wahrscheinlichkeit. */
export const PLAN_THRESHOLD = 0.8;

/** Antwort-Cache (Sekunden). */
export const CACHE_TTL_S = 60;

/** Lokale Uhrzeit (Europe/Zurich), ab der die tägliche Wartung laufen darf. */
export const MAINTENANCE_HOUR_LOCAL = 3;

export function findStation(key: string): StationConfig | undefined {
  const k = key.trim().toLowerCase();
  return STATIONS.find((s) => s.alias === k || String(s.id) === k);
}

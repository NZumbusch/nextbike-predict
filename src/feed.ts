/**
 * nextbike-live.json parsen und validieren. Bewusst schlank: nur die wenigen Felder,
 * die wir brauchen; bei kaputtem Feed wird geworfen (⇒ es wird nichts geschrieben).
 *
 * Relevante Felder (am echten Feed geprüft, city=126):
 *   countries[].cities[] { uid, name, places[] }
 *   place { uid, number, name, lat, lng, spot, bike, bikes, bikes_available_to_rent,
 *           booked_bikes, bike_racks, free_racks, active_place, maintenance,
 *           bike_types: { "<typ-id>": anzahl }, place_type }
 *   Frei abgestellte Velos erscheinen als place mit spot=false, bike=true, name "BIKE 4xxxxx".
 */
import { EBIKE_TYPES } from "./config";

export class FeedError extends Error {}

export interface StationState {
  uid: number;
  number: number | null;
  name: string;
  lat: number | null;
  lng: number | null;
  place_type: string | null;
  bikes: number;
  ebikes: number;
  /** null = Station hat keine Ständer (virtuelle Station) */
  free_racks: number | null;
  booked: number;
  /** false bei Wartung / inaktiver Station */
  active: boolean;
}

export interface ParsedFeed {
  stations: StationState[];
  free_floating: number;
  booked: number;
}

const MAX_BIKES = 500;

function count(v: unknown): number | null {
  return typeof v === "number" && Number.isInteger(v) && v >= 0 && v <= MAX_BIKES ? v : null;
}

/** Für einen bereits geparsten Feed (JSON-Objekt). */
export function parseFeed(json: unknown, cityId: number, minPlaces: number): ParsedFeed {
  const countries = (json as { countries?: unknown })?.countries;
  if (!Array.isArray(countries)) throw new FeedError("Feld 'countries' fehlt");
  let city: { places?: unknown } | undefined;
  for (const c of countries) {
    const cities = (c as { cities?: unknown })?.cities;
    if (!Array.isArray(cities)) continue;
    city = cities.find((x) => (x as { uid?: unknown })?.uid === cityId);
    if (city) break;
  }
  if (!city) throw new FeedError(`city ${cityId} nicht im Feed`);
  const places = city.places;
  if (!Array.isArray(places)) throw new FeedError("Feld 'places' fehlt");

  const stations: StationState[] = [];
  let freeFloating = 0;
  let booked = 0;
  for (const p of places as Record<string, unknown>[]) {
    if (!p || typeof p !== "object") continue;
    if (p.spot !== true) {
      // frei abgestelltes Velo
      if (p.bike === true) freeFloating += count(p.bikes) ?? 1;
      continue;
    }
    const uid = p.uid;
    if (typeof uid !== "number" || !Number.isInteger(uid)) continue;
    const bikes = count(p.bikes_available_to_rent) ?? count(p.bikes);
    if (bikes === null) continue; // unplausibel ⇒ Station auslassen statt Müll speichern
    let e = 0;
    const types = p.bike_types;
    if (types && typeof types === "object") {
      for (const [k, v] of Object.entries(types as Record<string, unknown>)) {
        if (EBIKE_TYPES.has(k)) e += count(v) ?? 0;
      }
    }
    const racks = count(p.bike_racks) ?? 0;
    const b = count(p.booked_bikes) ?? 0;
    booked += b;
    stations.push({
      uid,
      number: typeof p.number === "number" ? p.number : null,
      name: typeof p.name === "string" ? p.name : String(uid),
      lat: typeof p.lat === "number" ? p.lat : null,
      lng: typeof p.lng === "number" ? p.lng : null,
      place_type: typeof p.place_type === "string" ? p.place_type : null,
      bikes,
      ebikes: Math.min(e, bikes),
      free_racks: racks > 0 ? count(p.free_racks) : null,
      booked: b,
      active: p.active_place !== 0 && p.maintenance !== true,
    });
  }
  if (stations.length < minPlaces) {
    throw new FeedError(`nur ${stations.length} Stationen im Feed (< ${minPlaces}) – vermutlich gestört`);
  }
  return { stations, free_floating: freeFloating, booked };
}

/** Feed abrufen (mit Timeout und einem Retry). Wirft FeedError. */
export async function fetchFeed(url: string, cityId: number, minPlaces: number, fetcher: typeof fetch = fetch): Promise<ParsedFeed> {
  let lastErr: unknown;
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      const res = await fetcher(url, {
        headers: { accept: "application/json", "user-agent": "nextbike-predict (private availability tracker)" },
        signal: AbortSignal.timeout(10_000),
      });
      if (!res.ok) throw new FeedError(`HTTP ${res.status}`);
      let json: unknown;
      try {
        json = await res.json();
      } catch {
        throw new FeedError("Antwort ist kein JSON");
      }
      return parseFeed(json, cityId, minPlaces);
    } catch (e) {
      lastErr = e;
      if (attempt === 0) await new Promise((r) => setTimeout(r, 1500));
    }
  }
  throw lastErr instanceof FeedError ? lastErr : new FeedError(String(lastErr));
}

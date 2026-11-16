import fixture from "./fixtures/feed-luzern.json";
import { obsStmts } from "../src/db";
import { parseFeed } from "../src/feed";
import { CITY_ID, STATIONS } from "../src/config";

export const ids = STATIONS.map((s) => s.id);
export const clone = () => JSON.parse(JSON.stringify(fixture));
export const places = (j: ReturnType<typeof clone>) => j.countries[0].cities[0].places as Record<string, any>[];
export const fetchOf = (j: unknown) => (async () => Response.json(j)) as unknown as typeof fetch;
export const okFetch = fetchOf(fixture);

/** obs-Zeilen direkt schreiben (für Prognose-Tests) */
export async function seedObs(db: D1Database, ts5: number, j: unknown = fixture) {
  await db.batch(obsStmts(db, ts5, parseFeed(j, CITY_ID, 20), ids));
}

/** Velo von einem Ort an einen anderen verschieben (im Feed-JSON) */
export function moveBike(j: ReturnType<typeof clone>, bike: string, toUid: number | null) {
  for (const p of places(j)) {
    const i = (p.bike_numbers as string[]).indexOf(bike);
    if (i >= 0) {
      p.bike_numbers.splice(i, 1);
      p.bikes--;
      p.bikes_available_to_rent--;
    }
  }
  if (toUid !== null) {
    const t = places(j).find((p) => p.uid === toUid)!;
    t.bike_numbers.push(bike);
    t.bikes++;
    t.bikes_available_to_rent++;
  }
  return j;
}

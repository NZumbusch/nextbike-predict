/**
 * Cron-Lauf (alle 5 Min): Feed holen → validieren → obs + Snapshot schreiben;
 * 1× täglich (ab MAINTENANCE_HOUR_LOCAL Zürcher Zeit, nachgeholt falls verpasst) Wartung.
 */
import { CITY_ID, FEED_URL, MAINTENANCE_HOUR_LOCAL, MIN_PLACES, STATIONS } from "./config";
import { getMeta, maintenance, setMetaStmt, writeRun, type Env } from "./db";
import { fetchFeed } from "./feed";
import { toZurich } from "./time";

export interface CronResult {
  ok: boolean;
  ts: number;
  obs?: number;
  maintenance?: boolean;
  error?: string;
}

const intVar = (v: string | undefined, def: number) => {
  const n = Number(v);
  return Number.isFinite(n) && n >= 0 ? Math.floor(n) : def;
};

/** Läuft die Wartung jetzt? Ab der Wartungsstunde, max. 1× pro lokalem Kalendertag. */
export function maintenanceDue(nowMs: number, lastMaintSec: number | null): boolean {
  const now = toZurich(nowMs);
  if (lastMaintSec === null) return true; // erster Lauf: Stammdaten sofort anlegen
  if (now.hour < MAINTENANCE_HOUR_LOCAL) return false;
  const last = toZurich(lastMaintSec * 1000);
  return last.year !== now.year || last.month !== now.month || last.day !== now.day;
}

export async function runCron(env: Env, nowMs: number, fetcher: typeof fetch = fetch): Promise<CronResult> {
  // Zeitstempel = tatsächlicher Abrufzeitpunkt (nicht geplanter), auf 5 Min abgerundet.
  // Verspätete Läufe landen so im richtigen Slot, Doppelläufe werden per PK ignoriert.
  const ts = Math.floor(nowMs / 300_000) * 300;
  let feed;
  try {
    feed = await fetchFeed(env.FEED_URL || FEED_URL, CITY_ID, MIN_PLACES, fetcher);
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    console.error(`[cron ${ts}] Feed-Fehler: ${msg} – nichts geschrieben`);
    await env.DB.batch([setMetaStmt(env.DB, "last_error", JSON.stringify({ ts, msg }))]).catch(() => {});
    return { ok: false, ts, error: msg };
  }

  let obs: number;
  try {
    obs = await writeRun(env.DB, ts, feed, STATIONS.map((s) => s.id));
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    console.error(`[cron ${ts}] D1-Schreibfehler: ${msg}`);
    return { ok: false, ts, error: msg };
  }

  let didMaint = false;
  try {
    const last = await getMeta(env.DB, "last_maint");
    if (maintenanceDue(nowMs, last === null ? null : Number(last))) {
      await maintenance(env.DB, ts, intVar(env.RAW_RETENTION_DAYS, 60), intVar(env.SNAPSHOT_RETENTION_DAYS, 365), feed.stations);
      didMaint = true;
      console.log(`[cron ${ts}] Wartung erledigt`);
    }
  } catch (e) {
    // Wartung wird beim nächsten Lauf erneut versucht (last_maint nicht gesetzt)
    console.error(`[cron ${ts}] Wartung fehlgeschlagen: ${e instanceof Error ? e.message : e}`);
  }
  return { ok: true, ts, obs, maintenance: didMaint };
}

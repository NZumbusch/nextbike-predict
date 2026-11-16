/**
 * Cron-Lauf. Der Trigger feuert jede Minute; abgerufen wird
 *   tagsüber jede Minute, nachts (01–05 Uhr Zürich) alle 5 Minuten.
 *
 * Pro Abruf (1 D1-Batch):
 *   - log: Delta seit dem letzten Abruf (nur wenn sich etwas geändert hat), alle 6 h ein Keyframe
 *   - obs: 1× pro 5-Min-Fenster je eigene Station (Prognose-Basis)
 *   - state: aktueller Zustand (Basis fürs nächste Delta und für /now)
 * 1× täglich ab 03:00 (nachgeholt falls verpasst): Wartung mit Vollabruf (Velotypen).
 */
import {
  CITY_ID,
  DAY_INTERVAL_MIN,
  FEED_URL,
  FEED_URL_FULL,
  KEYFRAME_HOURS,
  MAINTENANCE_HOUR_LOCAL,
  MIN_PLACES,
  NIGHT_FROM_H,
  NIGHT_INTERVAL_MIN,
  NIGHT_TO_H,
  STATIONS,
} from "./config";
import { logStmt, maintenance, obsStmts, readState, setMetaStmt, writeStateStmt, type Env } from "./db";
import { fetchFeed, type ParsedFeed } from "./feed";
import { toZurich } from "./time";
import { currentMaps, diff, isEmpty, keyframe, type TrackerState } from "./tracker";

export interface CronResult {
  ok: boolean;
  ts: number;
  skipped?: boolean;
  log?: "K" | "D" | null;
  moves?: number;
  obs?: number;
  maintenance?: boolean;
  error?: string;
}

const intVar = (v: string | undefined, def: number) => {
  const n = Number(v);
  return v !== undefined && v !== "" && Number.isFinite(n) && n >= 0 ? Math.floor(n) : def;
};

export function intervalMin(nowMs: number): number {
  const h = toZurich(nowMs).hour;
  return h >= NIGHT_FROM_H && h < NIGHT_TO_H ? NIGHT_INTERVAL_MIN : DAY_INTERVAL_MIN;
}

/**
 * Abruf fällig? Basis ist der letzte erfolgreiche Abruf (nicht die Uhr), damit verspätete
 * Cron-Läufe nachts keine 10-Min-Lücken erzeugen. 20 s Toleranz für Cron-Jitter.
 */
export function pollDue(nowMs: number, lastTs: number | null): boolean {
  if (lastTs === null) return true;
  return nowMs / 1000 - lastTs >= intervalMin(nowMs) * 60 - 20;
}

/** Läuft die Wartung jetzt? Ab der Wartungsstunde, max. 1× pro lokalem Kalendertag. */
export function maintenanceDue(nowMs: number, lastMaintSec: number | null): boolean {
  const now = toZurich(nowMs);
  if (!lastMaintSec) return true; // erster Lauf: Stammdaten sofort anlegen
  if (now.hour < MAINTENANCE_HOUR_LOCAL) return false;
  const last = toZurich(lastMaintSec * 1000);
  return last.year !== now.year || last.month !== now.month || last.day !== now.day;
}

const errMsg = (e: unknown) => (e instanceof Error ? e.message : String(e));

export async function runCron(env: Env, nowMs: number, fetcher: typeof fetch = fetch): Promise<CronResult> {
  const ts = Math.floor(nowMs / 60_000) * 60;
  const db = env.DB;

  let prev: TrackerState | null;
  try {
    prev = await readState(db);
  } catch (e) {
    console.error(`[cron ${ts}] Zustand nicht lesbar: ${errMsg(e)}`);
    return { ok: false, ts, error: errMsg(e) };
  }
  if (!pollDue(nowMs, prev?.ts ?? null)) return { ok: true, ts, skipped: true };

  const maintDue = maintenanceDue(nowMs, prev?.last_maint ?? null);
  let feed: ParsedFeed;
  try {
    feed = await fetchFeed(env.FEED_URL || (maintDue ? FEED_URL_FULL : FEED_URL), CITY_ID, MIN_PLACES, fetcher);
  } catch (e) {
    const msg = errMsg(e);
    console.error(`[cron ${ts}] Feed-Fehler: ${msg} – nichts geschrieben`);
    await db.batch([setMetaStmt(db, "last_error", JSON.stringify({ ts, msg }))]).catch(() => {});
    return { ok: false, ts, error: msg };
  }

  const cur = currentMaps(feed);
  const stmts: D1PreparedStatement[] = [];

  // 1) Änderungs-Log
  let kind: "K" | "D" | null = null;
  let moves = 0;
  let keyTs = prev?.key_ts ?? 0;
  if (!prev || ts - prev.key_ts >= KEYFRAME_HOURS * 3600) {
    kind = "K";
    keyTs = ts;
    stmts.push(logStmt(db, ts, prev?.ts ?? null, "K", keyframe(cur)));
  } else {
    const d = diff(prev, cur);
    moves = d.bk.length;
    if (!isEmpty(d)) {
      kind = "D";
      stmts.push(logStmt(db, ts, prev.ts, "D", d));
    }
  }

  // 2) obs für die Prognose: 1× pro 5-Min-Fenster
  const ts5 = Math.floor(ts / 300) * 300;
  let obsTs = prev?.obs_ts ?? 0;
  let nObs = 0;
  if (ts5 > obsTs) {
    const o = obsStmts(db, ts5, feed, STATIONS.map((s) => s.id));
    nObs = o.length;
    stmts.push(...o);
    obsTs = ts5;
  }

  // 3) Zustand
  const next: TrackerState = {
    v: 1,
    ts,
    obs_ts: obsTs,
    key_ts: keyTs,
    last_maint: prev?.last_maint ?? 0,
    db_bytes: prev?.db_bytes ?? 0,
    st: cur.st,
    bk: cur.bk,
  };
  stmts.push(writeStateStmt(db, next));

  try {
    await db.batch(stmts);
  } catch (e) {
    console.error(`[cron ${ts}] D1-Schreibfehler: ${errMsg(e)}`);
    return { ok: false, ts, error: errMsg(e) };
  }

  let didMaint = false;
  if (maintDue) {
    try {
      const mb = intVar(env.DB_SIZE_LIMIT_MB, 450);
      const size = await maintenance(
        db,
        ts,
        {
          rawDays: intVar(env.RAW_RETENTION_DAYS, 60),
          logDays: intVar(env.LOG_RETENTION_DAYS, 0),
          snapDays: intVar(env.SNAPSHOT_RETENTION_DAYS, 365),
          dbBytes: prev?.db_bytes ?? 0,
          dbLimitBytes: mb * 1_000_000,
        },
        feed,
      );
      didMaint = true;
      console.log(`[cron ${ts}] Wartung erledigt${size ? `, DB ${(size / 1e6).toFixed(1)} MB` : ""}`);
    } catch (e) {
      // last_maint bleibt alt ⇒ nächster Lauf versucht es erneut
      console.error(`[cron ${ts}] Wartung fehlgeschlagen: ${errMsg(e)}`);
    }
  }
  return { ok: true, ts, log: kind, moves, obs: nObs, maintenance: didMaint };
}

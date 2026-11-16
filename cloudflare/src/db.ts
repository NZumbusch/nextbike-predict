/**
 * Alle D1-Zugriffe. Ziel: wenige Statements, wenige gelesene/geschriebene Zeilen.
 */
import type { ParsedFeed, StationState } from "./feed";
import { addStat, type StatsByStation } from "./predict";
import type { Delta, TrackerState } from "./tracker";
import { weekSlotOf } from "./time";

export interface Env {
  DB: D1Database;
  API_TOKEN?: string;
  SNAPSHOT_RETENTION_DAYS?: string;
  RAW_RETENTION_DAYS?: string;
  LOG_RETENTION_DAYS?: string;
  DB_SIZE_LIMIT_MB?: string;
  /** optional: anderer Feed (nur für lokale Tests, z. B. Fixture-Server) */
  FEED_URL?: string;
}

// ---------------------------------------------------------------- Zustand

export async function readState(db: D1Database): Promise<TrackerState | null> {
  const r = await db.prepare("SELECT data FROM state WHERE k = 1").first<{ data: string }>();
  if (!r) return null;
  try {
    const s = JSON.parse(r.data) as TrackerState;
    return s && s.v === 1 ? s : null;
  } catch {
    return null; // kaputter Zustand ⇒ wie Neustart behandeln (nächster Lauf schreibt Keyframe)
  }
}

export function writeStateStmt(db: D1Database, s: TrackerState): D1PreparedStatement {
  return db
    .prepare("INSERT INTO state (k, data) VALUES (1, ?1) ON CONFLICT(k) DO UPDATE SET data = excluded.data")
    .bind(JSON.stringify(s));
}

/** Einzelnes Feld im Zustand setzen, ohne ihn neu zu schreiben (für Wartung). */
export function patchStateStmt(db: D1Database, field: "last_maint" | "db_bytes", value: number): D1PreparedStatement {
  return db.prepare(`UPDATE state SET data = json_set(data, '$.${field}', ?1) WHERE k = 1`).bind(value);
}

// ---------------------------------------------------------------- Schreiben pro Lauf

export function logStmt(db: D1Database, ts: number, prevTs: number | null, kind: "K" | "D", d: Delta): D1PreparedStatement {
  return db
    .prepare("INSERT OR IGNORE INTO log (ts, prev_ts, kind, n_st, n_bk, st, bk) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)")
    .bind(ts, prevTs, kind, d.st.length, d.bk.length, JSON.stringify(d.st), JSON.stringify(d.bk));
}

/** obs-Zeilen (Prognose-Basis) für die eigenen, aktiven Stationen. */
export function obsStmts(db: D1Database, ts5: number, feed: ParsedFeed, myIds: readonly number[]): D1PreparedStatement[] {
  const wslot = weekSlotOf(ts5 * 1000);
  const byId = new Map(feed.stations.map((s) => [s.uid, s]));
  const stmt = db.prepare(
    "INSERT OR IGNORE INTO obs (station_id, wslot, ts, bikes, ebikes, free_racks) VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
  );
  const out: D1PreparedStatement[] = [];
  for (const id of myIds) {
    const s = byId.get(id);
    if (!s) {
      console.warn(`Station ${id} fehlt im Feed – Lücke`);
      continue;
    }
    if (!s.active) {
      console.warn(`Station ${id} inaktiv/Wartung – keine Beobachtung`);
      continue;
    }
    out.push(stmt.bind(id, wslot, ts5, s.bikes, s.ebikes, s.free_racks));
  }
  return out;
}

// ---------------------------------------------------------------- Stammdaten (1× täglich)

/** Stammdaten aller Stationen per einzelnem Statement (json_each) upserten. */
export function upsertStationsStmt(db: D1Database, tsSec: number, stations: StationState[]): D1PreparedStatement {
  const rows = stations.map((s) => [s.uid, s.number, s.name, s.lat, s.lng, s.place_type]);
  return db
    .prepare(
      `INSERT INTO stations (uid, number, name, lat, lng, place_type, first_seen, last_seen)
       SELECT json_extract(value, '$[0]'), json_extract(value, '$[1]'), json_extract(value, '$[2]'),
              json_extract(value, '$[3]'), json_extract(value, '$[4]'), json_extract(value, '$[5]'), ?2, ?2
       FROM json_each(?1) WHERE true
       ON CONFLICT(uid) DO UPDATE SET number = excluded.number, name = excluded.name, lat = excluded.lat,
         lng = excluded.lng, place_type = excluded.place_type, last_seen = excluded.last_seen`,
    )
    .bind(JSON.stringify(rows), tsSec);
}

/** Velo-Stammdaten: alle sichtbaren Velos, Typ wenn bekannt (aus Vollabruf). */
export function upsertBikesStmt(db: D1Database, tsSec: number, feed: ParsedFeed): D1PreparedStatement {
  const rows = [...feed.bikes.keys()].map((n) => [n, feed.bikeTypes.get(n) ?? null]);
  return db
    .prepare(
      `INSERT INTO bikes (number, type, first_seen, last_seen)
       SELECT json_extract(value, '$[0]'), json_extract(value, '$[1]'), ?2, ?2
       FROM json_each(?1) WHERE true
       ON CONFLICT(number) DO UPDATE SET type = coalesce(excluded.type, type), last_seen = excluded.last_seen`,
    )
    .bind(JSON.stringify(rows), tsSec);
}

// ---------------------------------------------------------------- Wartung

export interface MaintenanceOpts {
  rawDays: number;
  /** 0 = für immer */
  logDays: number;
  /** 0 = für immer (alte v1-Snapshots) */
  snapDays: number;
  /** aktuelle DB-Grösse und Grenze in Bytes; darüber werden die ältesten 30 Tage Log gelöscht */
  dbBytes: number;
  dbLimitBytes: number;
}

/**
 * Tägliche Wartung (in einer Transaktion):
 *  1. obs älter als rawDays → in agg addieren (Station × Wochen-Slot), dann löschen
 *  2. Log/Snapshots nach Aufbewahrungsfrist bzw. Grössen-Schutz löschen
 *  3. Stammdaten (Stationen, Velos) aktualisieren, last_maint setzen
 * Gibt die DB-Grösse nach der Wartung zurück (falls D1 sie meldet).
 */
export async function maintenance(db: D1Database, tsSec: number, o: MaintenanceOpts, feed: ParsedFeed | null): Promise<number | null> {
  const rawCut = tsSec - o.rawDays * 86400;
  const stmts: D1PreparedStatement[] = [
    db
      .prepare(
        `INSERT INTO agg (station_id, wslot, n, n_avail, sum_bikes, n_ebike_avail, sum_ebikes, first_ts, last_ts)
         SELECT station_id, wslot, count(*), sum(bikes > 0), sum(bikes),
                coalesce(sum(ebikes > 0), 0), coalesce(sum(ebikes), 0), min(ts), max(ts)
         FROM obs WHERE ts < ?1 GROUP BY station_id, wslot
         ON CONFLICT(station_id, wslot) DO UPDATE SET
           n = n + excluded.n, n_avail = n_avail + excluded.n_avail, sum_bikes = sum_bikes + excluded.sum_bikes,
           n_ebike_avail = n_ebike_avail + excluded.n_ebike_avail, sum_ebikes = sum_ebikes + excluded.sum_ebikes,
           first_ts = min(first_ts, excluded.first_ts), last_ts = max(last_ts, excluded.last_ts)`,
      )
      .bind(rawCut),
    db.prepare("DELETE FROM obs WHERE ts < ?1").bind(rawCut),
  ];
  if (o.logDays > 0) stmts.push(db.prepare("DELETE FROM log WHERE ts < ?1").bind(tsSec - o.logDays * 86400));
  if (o.dbLimitBytes > 0 && o.dbBytes > o.dbLimitBytes) {
    console.warn(`DB ${Math.round(o.dbBytes / 1e6)} MB > Grenze – lösche älteste 30 Tage Log`);
    stmts.push(db.prepare("DELETE FROM log WHERE ts < (SELECT min(ts) FROM log) + 30 * 86400"));
    stmts.push(db.prepare("DELETE FROM snap WHERE ts < (SELECT min(ts) FROM snap) + 30 * 86400"));
  }
  if (o.snapDays > 0) stmts.push(db.prepare("DELETE FROM snap WHERE ts < ?1").bind(tsSec - o.snapDays * 86400));
  if (feed) {
    stmts.push(upsertStationsStmt(db, tsSec, feed.stations));
    stmts.push(upsertBikesStmt(db, tsSec, feed));
  }
  stmts.push(patchStateStmt(db, "last_maint", tsSec));
  const res = await db.batch(stmts);
  const size = (res[res.length - 1]?.meta as { size_after?: number } | undefined)?.size_after;
  if (typeof size === "number") {
    await db.batch([patchStateStmt(db, "db_bytes", size)]);
    return size;
  }
  return null;
}

export function setMetaStmt(db: D1Database, k: string, v: string): D1PreparedStatement {
  return db.prepare("INSERT INTO meta (k, v) VALUES (?1, ?2) ON CONFLICT(k) DO UPDATE SET v = excluded.v").bind(k, v);
}

export async function getMeta(db: D1Database, k: string): Promise<string | null> {
  const r = await db.prepare("SELECT v FROM meta WHERE k = ?1").bind(k).first<{ v: string }>();
  return r?.v ?? null;
}

// ---------------------------------------------------------------- Lesen

const intList = (xs: readonly number[]) => xs.map((x) => String(Math.trunc(x))).join(",");

/** Statistik für Stationen × Wochen-Slots aus obs (aggregiert) + agg, in einer Abfrage. */
export async function loadStats(db: D1Database, stationIds: readonly number[], wslots: readonly number[]): Promise<StatsByStation> {
  const stats: StatsByStation = new Map();
  if (stationIds.length === 0 || wslots.length === 0) return stats;
  // Werte sind validierte Integers ⇒ inline (spart das 100-Parameter-Limit von D1)
  const sIn = intList(stationIds);
  const wIn = intList([...new Set(wslots)]);
  const { results } = await db
    .prepare(
      `SELECT station_id, wslot, count(*) AS n, sum(bikes > 0) AS n_avail, sum(bikes) AS sum_bikes,
              coalesce(sum(ebikes > 0), 0) AS n_e_avail, coalesce(sum(ebikes), 0) AS sum_e
       FROM obs WHERE station_id IN (${sIn}) AND wslot IN (${wIn}) GROUP BY station_id, wslot
       UNION ALL
       SELECT station_id, wslot, n, n_avail, sum_bikes, n_ebike_avail, sum_ebikes
       FROM agg WHERE station_id IN (${sIn}) AND wslot IN (${wIn})`,
    )
    .all<{ station_id: number; wslot: number; n: number; n_avail: number; sum_bikes: number; n_e_avail: number; sum_e: number }>();
  for (const r of results) {
    addStat(stats, r.station_id, r.wslot, { n: r.n, n_avail: r.n_avail, sum_bikes: r.sum_bikes, n_e_avail: r.n_e_avail, sum_e: r.sum_e });
  }
  return stats;
}

/** Aktueller Stand (aus dem Zustand, 1 gelesene Zeile). */
export interface Current {
  ts: number;
  free_floating: number;
  total_bikes: number;
  visible_bikes: number;
  db_bytes: number;
  byId: Map<number, { bikes: number; ebikes: number; active: boolean }>;
}

export function currentFromState(s: TrackerState | null): Current | null {
  if (!s || !s.ts) return null;
  const byId = new Map<number, { bikes: number; ebikes: number; active: boolean }>();
  let total = 0;
  for (const uid in s.st) {
    const t = s.st[uid]!;
    byId.set(Number(uid), { bikes: t[0], ebikes: t[1], active: t[3] === 1 });
    total += t[0];
  }
  let ff = 0;
  for (const n in s.bk) if (Array.isArray(s.bk[n])) ff++;
  return { ts: s.ts, free_floating: ff, total_bikes: total, visible_bikes: Object.keys(s.bk).length, db_bytes: s.db_bytes, byId };
}

export async function readCurrent(db: D1Database): Promise<Current | null> {
  return currentFromState(await readState(db));
}

/**
 * Alle D1-Zugriffe. Ziel: wenige Statements, wenige gelesene/geschriebene Zeilen.
 */
import type { ParsedFeed, StationState } from "./feed";
import { addStat, type StatsByStation } from "./predict";
import { weekSlotOf } from "./time";

export interface Env {
  DB: D1Database;
  API_TOKEN?: string;
  SNAPSHOT_RETENTION_DAYS?: string;
  RAW_RETENTION_DAYS?: string;
}

/** Snapshot-Eintrag: [uid, bikes, ebikes] bzw. [uid, bikes, ebikes, 0] bei inaktiver Station */
export type SnapEntry = [number, number, number] | [number, number, number, 0];

export function snapEntries(stations: StationState[]): SnapEntry[] {
  return stations.map((s) => (s.active ? [s.uid, s.bikes, s.ebikes] : [s.uid, s.bikes, s.ebikes, 0]));
}

/**
 * Schreibt einen Cron-Lauf: je 1 obs-Zeile pro eigener (aktiver) Station + 1 Snapshot.
 * INSERT OR IGNORE ⇒ ein doppelter Lauf im selben 5-Min-Fenster schreibt nichts doppelt.
 * Gibt die Anzahl geschriebener obs-Zeilen zurück.
 */
export async function writeRun(db: D1Database, tsSec: number, feed: ParsedFeed, myIds: readonly number[]): Promise<number> {
  const wslot = weekSlotOf(tsSec * 1000);
  const byId = new Map(feed.stations.map((s) => [s.uid, s]));
  const stmts: D1PreparedStatement[] = [];
  const obsStmt = db.prepare(
    "INSERT OR IGNORE INTO obs (station_id, wslot, ts, bikes, ebikes, free_racks) VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
  );
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
    stmts.push(obsStmt.bind(id, wslot, tsSec, s.bikes, s.ebikes, s.free_racks));
  }
  const nObs = stmts.length;
  let totalBikes = 0, totalE = 0;
  for (const s of feed.stations) {
    totalBikes += s.bikes;
    totalE += s.ebikes;
  }
  stmts.push(
    db
      .prepare(
        "INSERT OR IGNORE INTO snap (ts, n_stations, total_bikes, total_ebikes, free_floating, booked, data) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
      )
      .bind(tsSec, feed.stations.length, totalBikes, totalE, feed.free_floating, feed.booked, JSON.stringify(snapEntries(feed.stations))),
  );
  await db.batch(stmts);
  return nObs;
}

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

/**
 * Tägliche Wartung (in einer Transaktion):
 *  1. obs älter als rawDays → in agg addieren (Station × Wochen-Slot), dann löschen
 *  2. Snapshots älter als snapDays löschen (0 = behalten)
 *  3. Stammdaten aktualisieren, last_maint setzen
 */
export async function maintenance(
  db: D1Database,
  tsSec: number,
  rawDays: number,
  snapDays: number,
  stations: StationState[] | null,
): Promise<void> {
  const rawCut = tsSec - rawDays * 86400;
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
  if (snapDays > 0) stmts.push(db.prepare("DELETE FROM snap WHERE ts < ?1").bind(tsSec - snapDays * 86400));
  if (stations) stmts.push(upsertStationsStmt(db, tsSec, stations));
  stmts.push(setMetaStmt(db, "last_maint", String(tsSec)));
  await db.batch(stmts);
}

export function setMetaStmt(db: D1Database, k: string, v: string): D1PreparedStatement {
  return db.prepare("INSERT INTO meta (k, v) VALUES (?1, ?2) ON CONFLICT(k) DO UPDATE SET v = excluded.v").bind(k, v);
}

export async function getMeta(db: D1Database, k: string): Promise<string | null> {
  const r = await db.prepare("SELECT v FROM meta WHERE k = ?1").bind(k).first<{ v: string }>();
  return r?.v ?? null;
}

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

export interface LatestSnap {
  ts: number;
  free_floating: number;
  total_bikes: number;
  byId: Map<number, { bikes: number; ebikes: number; active: boolean }>;
}

export async function latestSnap(db: D1Database): Promise<LatestSnap | null> {
  const r = await db
    .prepare("SELECT ts, free_floating, total_bikes, data FROM snap ORDER BY ts DESC LIMIT 1")
    .first<{ ts: number; free_floating: number; total_bikes: number; data: string }>();
  if (!r) return null;
  const byId = new Map<number, { bikes: number; ebikes: number; active: boolean }>();
  for (const e of JSON.parse(r.data) as SnapEntry[]) byId.set(e[0], { bikes: e[1], ebikes: e[2], active: e[3] !== 0 });
  return { ts: r.ts, free_floating: r.free_floating, total_bikes: r.total_bikes, byId };
}

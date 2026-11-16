/**
 * HTTP-Handler (werden erst NACH erfolgreicher Auth aufgerufen).
 */
import { findStation, MIN_OBS, PLAN_THRESHOLD, STATIONS, type Group, type StationConfig } from "./config";
import { getMeta, loadStats, readCurrent, type Current, type Env } from "./db";
import { predict, recommend, type Prediction } from "./predict";
import { formatZurich, neighborSlots, parseAt, type ParsedAt } from "./time";

export class HttpError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export const json = (data: unknown, status = 200) =>
  new Response(JSON.stringify(data, null, 2), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });

const stationInfo = (s: StationConfig) => ({
  id: s.id,
  alias: s.alias,
  name: s.name,
  group: s.group,
  primary: s.primary,
  ...(s.distance_m ? { distance_m: s.distance_m } : {}),
});

function nowView(snap: Current | null, nowMs: number) {
  if (!snap) return { as_of: null, age_s: null, stale: true, network: null, stations: STATIONS.map((s) => ({ ...stationInfo(s), bikes: null, ebikes: null })) };
  const age = Math.round(nowMs / 1000 - snap.ts);
  return {
    as_of: formatZurich(snap.ts * 1000),
    as_of_utc: new Date(snap.ts * 1000).toISOString(),
    age_s: age,
    stale: age > 15 * 60,
    network: {
      stations: snap.byId.size,
      bikes_at_stations: snap.total_bikes,
      free_floating: snap.free_floating,
      visible_bikes: snap.visible_bikes,
      db_mb: snap.db_bytes ? Math.round(snap.db_bytes / 1e5) / 10 : null,
    },
    stations: STATIONS.map((s) => {
      const v = snap.byId.get(s.id);
      return { ...stationInfo(s), bikes: v?.bikes ?? null, ebikes: v?.ebikes ?? null, ...(v && !v.active ? { inactive: true } : {}) };
    }),
  };
}

export async function handleNow(env: Env, nowMs: number): Promise<Response> {
  return json(nowView(await readCurrent(env.DB), nowMs));
}

function atView(at: ParsedAt) {
  return { local: formatZurich(at.utcMs), utc: new Date(at.utcMs).toISOString() };
}

function parseAtOr400(v: string | null, nowMs: number): ParsedAt {
  try {
    return parseAt(v, nowMs);
  } catch (e) {
    throw new HttpError(400, e instanceof Error ? e.message : "ungültiges 'at'");
  }
}

export async function handlePredict(env: Env, url: URL, nowMs: number): Promise<Response> {
  const key = url.searchParams.get("station");
  if (!key) throw new HttpError(400, "Parameter 'station' fehlt (ID oder Alias)");
  const st = findStation(key);
  if (!st) throw new HttpError(404, `Unbekannte Station '${key}'. Bekannt: ${STATIONS.map((s) => `${s.alias} (${s.id})`).join(", ")}`);
  const at = parseAtOr400(url.searchParams.get("at"), nowMs);
  const stats = await loadStats(env.DB, [st.id], neighborSlots(at.wslot));
  const p = predict(stats.get(st.id), at.wslot, MIN_OBS);
  return json({ station: stationInfo(st), at: atView(at), ...p });
}

export async function handlePlan(env: Env, url: URL, nowMs: number): Promise<Response> {
  const dirRaw = (url.searchParams.get("dir") ?? "start").toLowerCase();
  if (dirRaw !== "start" && dirRaw !== "ziel") throw new HttpError(400, "dir muss 'start' oder 'ziel' sein");
  const group = dirRaw as Group;
  const at = parseAtOr400(url.searchParams.get("at"), nowMs);
  const members = STATIONS.filter((s) => s.group === group).sort((a, b) => Number(b.primary) - Number(a.primary));
  const [stats, snap] = await Promise.all([loadStats(env.DB, members.map((s) => s.id), neighborSlots(at.wslot)), readCurrent(env.DB)]);
  const options = members.map((s) => ({ station: s, prediction: predict(stats.get(s.id), at.wslot, MIN_OBS) }));
  const now = nowView(snap, nowMs);
  return json({
    at: atView(at),
    group,
    threshold: PLAN_THRESHOLD,
    recommendation: recommend(options, PLAN_THRESHOLD),
    options: options.map((o) => {
      const cur = now.stations.find((x) => x.id === o.station.id);
      return { station: stationInfo(o.station), prediction: o.prediction, now: { bikes: cur?.bikes ?? null, ebikes: cur?.ebikes ?? null } };
    }),
    now_as_of: now.as_of,
  });
}

/** Daten für die HTML-Seite: aktueller Stand + Prognose für die nächsten Stunden. */
export interface StatusData {
  now: ReturnType<typeof nowView>;
  columns: { label: string; wslot: number }[];
  rows: { station: StationConfig; preds: Prediction[] }[];
  lastError: { ts: number; msg: string } | null;
}

export async function statusData(env: Env, nowMs: number, hours = 4, stepMin = 30): Promise<StatusData> {
  const columns: { label: string; wslot: number }[] = [];
  const startMs = Math.ceil(nowMs / (15 * 60_000)) * 15 * 60_000;
  for (let t = startMs; t <= startMs + hours * 3600_000; t += stepMin * 60_000) {
    const p = parseAt(new Date(t).toISOString(), nowMs);
    columns.push({ label: formatZurich(t).slice(11, 16), wslot: p.wslot });
  }
  const slots = columns.flatMap((c) => neighborSlots(c.wslot));
  const [stats, snap, errRaw] = await Promise.all([
    loadStats(env.DB, STATIONS.map((s) => s.id), slots),
    readCurrent(env.DB),
    getMeta(env.DB, "last_error"),
  ]);
  let lastError: StatusData["lastError"] = null;
  try {
    lastError = errRaw ? JSON.parse(errRaw) : null;
  } catch {
    /* ignorieren */
  }
  return {
    now: nowView(snap, nowMs),
    columns,
    rows: STATIONS.map((s) => ({ station: s, preds: columns.map((c) => predict(stats.get(s.id), c.wslot, MIN_OBS)) })),
    lastError,
  };
}

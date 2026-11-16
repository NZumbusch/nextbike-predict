/**
 * Änderungserkennung zwischen zwei Abrufen (rein funktional, ohne D1).
 *
 * Gespeichert wird nur, was sich geändert hat ("Delta"), plus alle paar Stunden ein
 * vollständiger "Keyframe". So lässt sich offline jeder Zustand rekonstruieren:
 * Stationsbestände minutengenau und jede einzelne Velobewegung (Ausleihe, Rückgabe,
 * Umverteilung).
 */
import type { Loc, ParsedFeed, StationState } from "./feed";

/** Stationszustand kompakt: [verfügbar, davon E, reserviert, aktiv 1/0] */
export type StTuple = [number, number, number, 0 | 1];
/** Ort im Log: Station-uid, [lat, lng] (frei abgestellt) oder 0 = nicht sichtbar (unterwegs/Depot) */
export type LogLoc = Loc | 0;

export interface TrackerState {
  v: 1;
  /** Zeitpunkt des letzten erfolgreichen Abrufs (Unix-s) */
  ts: number;
  /** letzter geschriebener 5-Min-Bucket der obs-Tabelle */
  obs_ts: number;
  /** letzter Keyframe */
  key_ts: number;
  /** letzte tägliche Wartung (0 = nie) */
  last_maint: number;
  /** DB-Grösse in Bytes (aus D1-Metadaten, 1 Lauf verzögert) */
  db_bytes: number;
  st: Record<string, StTuple>;
  bk: Record<string, Loc>;
}

export interface Delta {
  /** geänderte Stationen [uid, ...StTuple]; verschwundene Station: [uid] */
  st: (number | 0 | 1)[][];
  /** Velobewegungen [nummer, von, nach] */
  bk: [string, LogLoc, LogLoc][];
}

export function stTuple(s: StationState): StTuple {
  return [s.bikes, s.ebikes, s.booked, s.active ? 1 : 0];
}

export function currentMaps(feed: ParsedFeed): { st: Record<string, StTuple>; bk: Record<string, Loc> } {
  const st: Record<string, StTuple> = {};
  for (const s of feed.stations) st[s.uid] = stTuple(s);
  const bk: Record<string, Loc> = {};
  for (const [n, l] of feed.bikes) bk[n] = l;
  return { st, bk };
}

const sameLoc = (a: Loc | undefined, b: Loc | undefined) =>
  a === b || (Array.isArray(a) && Array.isArray(b) && a[0] === b[0] && a[1] === b[1]);

const sameSt = (a: StTuple | undefined, b: StTuple | undefined) =>
  !!a && !!b && a[0] === b[0] && a[1] === b[1] && a[2] === b[2] && a[3] === b[3];

export function diff(
  prev: { st: Record<string, StTuple>; bk: Record<string, Loc> },
  cur: { st: Record<string, StTuple>; bk: Record<string, Loc> },
): Delta {
  const st: Delta["st"] = [];
  for (const uid in cur.st) if (!sameSt(prev.st[uid], cur.st[uid])) st.push([Number(uid), ...cur.st[uid]!]);
  for (const uid in prev.st) if (!(uid in cur.st)) st.push([Number(uid)]);
  const bk: Delta["bk"] = [];
  for (const n in cur.bk) {
    const a = prev.bk[n];
    if (!sameLoc(a, cur.bk[n])) bk.push([n, a ?? 0, cur.bk[n]!]);
  }
  for (const n in prev.bk) if (!(n in cur.bk)) bk.push([n, prev.bk[n]!, 0]);
  return { st, bk };
}

export function isEmpty(d: Delta): boolean {
  return d.st.length === 0 && d.bk.length === 0;
}

/** Keyframe: vollständiger Zustand im selben Format wie ein Delta von "leer" aus. */
export function keyframe(cur: { st: Record<string, StTuple>; bk: Record<string, Loc> }): Delta {
  return diff({ st: {}, bk: {} }, cur);
}

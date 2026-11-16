/**
 * Rekonstruiert aus dem Änderungs-Log (Tabelle `log`) analysierbare Tabellen:
 *   station_changes  ts, uid, bikes, ebikes, booked, active        (jede Bestandsänderung)
 *   bike_events      ts, prev_ts, bike, from_*, to_*                 (jede Velobewegung)
 *   trips            Ausleihe → Rückgabe pro Velo (inkl. Umverteilungs-Hinweis)
 *
 * Ort im Log: Station-uid (Zahl), [lat, lng] (frei abgestellt) oder 0 (nicht sichtbar).
 * Keyframes ('K') werden gegen den rekonstruierten Zustand gediffed, damit Bewegungen in
 * der Keyframe-Minute nicht verloren gehen. Der Zustand ist serialisierbar (inkrementeller Export).
 */

/** @typedef {number | [number, number] | 0} Loc */

export function newState() {
  return { ts: null, st: {}, bk: {}, away: {} };
}

const sameLoc = (a, b) => a === b || (Array.isArray(a) && Array.isArray(b) && a[0] === b[0] && a[1] === b[1]);

function locCols(loc, coords) {
  if (loc === 0 || loc === undefined) return ["", "", ""];
  if (Array.isArray(loc)) return ["", loc[0], loc[1]];
  const c = coords.get(loc);
  return [loc, c ? c[0] : "", c ? c[1] : ""];
}

function haversine(a, b) {
  if (!a || !b || a[0] === "" || b[0] === "") return "";
  const R = 6371000, rad = Math.PI / 180;
  const dLat = (b[0] - a[0]) * rad, dLng = (b[1] - a[1]) * rad;
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(a[0] * rad) * Math.cos(b[0] * rad) * Math.sin(dLng / 2) ** 2;
  return Math.round(2 * R * Math.asin(Math.sqrt(h)));
}

/**
 * Wendet eine Log-Zeile an.
 * @param {ReturnType<typeof newState>} s Zustand (wird verändert)
 * @param {{ts:number, prev_ts:number|null, kind:'K'|'D', st:any[], bk:any[]}} row
 * @param {Map<number,[number,number]>} coords Station-uid → [lat, lng]
 * @param {Map<string,string>} types Velonummer → Typ
 * @returns {{stationChanges:any[][], events:any[][], trips:any[][]}}
 */
export function applyRow(s, row, coords, types = new Map()) {
  const out = { stationChanges: [], events: [], trips: [] };
  const first = s.ts === null;
  let st = row.st;
  let bk = row.bk;

  if (row.kind === "K") {
    // Keyframe → effektives Delta gegen den bisherigen Zustand
    const curSt = {};
    for (const e of st) curSt[e[0]] = e.slice(1);
    const curBk = {};
    for (const [n, , to] of bk) curBk[n] = to;
    st = [];
    for (const uid in curSt) {
      const a = s.st[uid], b = curSt[uid];
      if (!a || a.join() !== b.join()) st.push([Number(uid), ...b]);
    }
    for (const uid in s.st) if (!(uid in curSt)) st.push([Number(uid)]);
    bk = [];
    for (const n in curBk) if (!sameLoc(s.bk[n], curBk[n])) bk.push([n, s.bk[n] ?? 0, curBk[n]]);
    for (const n in s.bk) if (!(n in curBk)) bk.push([n, s.bk[n], 0]);
  }

  for (const e of st) {
    if (e.length === 1) {
      delete s.st[e[0]];
      out.stationChanges.push([row.ts, e[0], "", "", "", 0]);
    } else {
      s.st[e[0]] = e.slice(1);
      out.stationChanges.push([row.ts, ...e]);
    }
  }

  // Rückgaben pro Zielstation in dieser Zeile zählen (viele gleichzeitig ⇒ Umverteilung wahrscheinlich)
  const arrivalsAt = new Map();
  for (const [, from, to] of bk) if (to !== 0 && !Array.isArray(to)) arrivalsAt.set(to, (arrivalsAt.get(to) ?? 0) + 1);

  for (const [n, from, to] of bk) {
    if (to === 0) delete s.bk[n];
    else s.bk[n] = to;
    if (first) continue; // allererster Keyframe = Ausgangszustand, keine Ereignisse
    out.events.push([row.ts, row.prev_ts ?? "", n, ...locCols(from, coords), ...locCols(to, coords)]);

    const tripRow = (startTs, startLoc, direct) => {
      const a = locCols(startLoc, coords), b = locCols(to, coords);
      const dist = haversine([a[1], a[2]], [b[1], b[2]]);
      const batch = Array.isArray(to) ? 1 : arrivalsAt.get(to) ?? 1;
      return [n, types.get(n) ?? "", startTs, ...a, row.ts, ...b, Math.round((row.ts - startTs) / 60), dist, direct ? 1 : 0, batch];
    };
    if (from !== 0 && to === 0) {
      s.away[n] = { ts: row.ts, from };
    } else if (from === 0 && to !== 0) {
      const dep = s.away[n];
      if (dep) out.trips.push(tripRow(dep.ts, dep.from, false));
      delete s.away[n];
    } else if (from !== 0 && to !== 0) {
      // in einem Intervall von A nach B (kurze Fahrt oder Umverteilung)
      out.trips.push(tripRow(row.prev_ts ?? row.ts, from, true));
    }
  }
  s.ts = row.ts;
  return out;
}

export const STATION_CHANGES_HEADER = "ts,uid,bikes,ebikes,booked,active";
export const BIKE_EVENTS_HEADER = "ts,prev_ts,bike,from_uid,from_lat,from_lng,to_uid,to_lat,to_lng";
export const TRIPS_HEADER =
  "bike,type,start_ts,start_uid,start_lat,start_lng,end_ts,end_uid,end_lat,end_lng,duration_min,distance_m,direct,arrivals_same_minute";

/** Eine CSV-Zeile parsen (Felder mit "…" und "" als Escape). */
export function parseCsvLine(line) {
  const f = [];
  let i = 0;
  while (i <= line.length) {
    if (line[i] === '"') {
      let v = "";
      i++;
      while (i < line.length) {
        if (line[i] === '"' && line[i + 1] === '"') (v += '"'), (i += 2);
        else if (line[i] === '"') break;
        else v += line[i++];
      }
      f.push(v);
      i += 2; // schliessendes " und Komma
    } else {
      const j = line.indexOf(",", i);
      const end = j === -1 ? line.length : j;
      f.push(line.slice(i, end));
      i = end + 1;
    }
  }
  return f;
}


/**
 * Prognose = simple Statistik über historische Beobachtungen im passenden
 * Wochen-Slot (Wochentag × 15 Min, Europe/Zurich), geglättet über ±1 Slot.
 * Rohdaten (obs) und verdichtete Daten (agg) werden einfach addiert.
 */
import { neighborSlots, slotLabel } from "./time";
import type { StationConfig } from "./config";

export interface SlotStat {
  n: number;
  n_avail: number;
  sum_bikes: number;
  n_e_avail: number;
  sum_e: number;
}

/** station_id → wslot → Statistik */
export type StatsByStation = Map<number, Map<number, SlotStat>>;

export function addStat(stats: StatsByStation, stationId: number, wslot: number, s: SlotStat): void {
  let m = stats.get(stationId);
  if (!m) stats.set(stationId, (m = new Map()));
  const cur = m.get(wslot);
  if (!cur) m.set(wslot, { ...s });
  else {
    cur.n += s.n;
    cur.n_avail += s.n_avail;
    cur.sum_bikes += s.sum_bikes;
    cur.n_e_avail += s.n_e_avail;
    cur.sum_e += s.sum_e;
  }
}

export interface Prediction {
  status: "ok" | "zu_wenig_daten";
  slot: string;
  slots_used: string[];
  /** Anzahl Beobachtungen (über alle geglätteten Slots) */
  n: number;
  /** Anteil Beobachtungen mit ≥ 1 Velo (0..1), null bei zu wenig Daten */
  probability: number | null;
  avg_bikes: number | null;
  probability_ebike: number | null;
  avg_ebikes: number | null;
  message?: string;
}

const r3 = (x: number) => Math.round(x * 1000) / 1000;
const r2 = (x: number) => Math.round(x * 100) / 100;

export function predict(stats: Map<number, SlotStat> | undefined, wslot: number, minObs: number, smooth = 1): Prediction {
  const slots = neighborSlots(wslot, smooth);
  let n = 0, nA = 0, sB = 0, nE = 0, sE = 0;
  for (const ws of slots) {
    const s = stats?.get(ws);
    if (!s) continue;
    n += s.n;
    nA += s.n_avail;
    sB += s.sum_bikes;
    nE += s.n_e_avail;
    sE += s.sum_e;
  }
  const base = { slot: slotLabel(wslot), slots_used: slots.map(slotLabel), n };
  if (n < minObs) {
    return {
      ...base,
      status: "zu_wenig_daten",
      probability: null,
      avg_bikes: null,
      probability_ebike: null,
      avg_ebikes: null,
      message: `zu wenig Daten (${n} Beobachtungen, mindestens ${minObs} nötig)`,
    };
  }
  return {
    ...base,
    status: "ok",
    probability: r3(nA / n),
    avg_bikes: r2(sB / n),
    probability_ebike: r3(nE / n),
    avg_ebikes: r2(sE / n),
  };
}

export interface PlanOption {
  station: StationConfig;
  prediction: Prediction;
}

export interface Recommendation {
  station_id: number | null;
  alias: string | null;
  name: string | null;
  probability: number | null;
  text: string;
}

const pct = (p: number) => `${Math.round(p * 100)} %`;

/**
 * Empfehlung: Stammstation, wenn sicher genug (≥ threshold); sonst die
 * Ausweichstation mit der höchsten Wahrscheinlichkeit (falls besser als Stamm).
 */
export function recommend(options: PlanOption[], threshold: number): Recommendation {
  const ok = options.filter((o) => o.prediction.status === "ok");
  if (ok.length === 0) {
    return { station_id: null, alias: null, name: null, probability: null, text: "Zu wenig Daten für eine Empfehlung." };
  }
  const primary = options.find((o) => o.station.primary);
  const pick = (o: PlanOption, text: string): Recommendation => ({
    station_id: o.station.id,
    alias: o.station.alias,
    name: o.station.name,
    probability: o.prediction.probability,
    text,
  });
  const pp = primary?.prediction.status === "ok" ? primary.prediction.probability! : null;
  if (primary && pp !== null && pp >= threshold) {
    return pick(primary, `${primary.station.name}: ${pct(pp)} (Ø ${primary.prediction.avg_bikes} Velos) – Stammstation passt.`);
  }
  const best = [...ok].sort((a, b) => b.prediction.probability! - a.prediction.probability!)[0]!;
  if (primary && best.station.id === primary.station.id) {
    return pick(primary, `${primary.station.name}: nur ${pct(pp!)}, aber keine Ausweichstation ist besser.`);
  }
  const dist = best.station.distance_m ? ` (${best.station.distance_m} m)` : "";
  const primTxt = primary ? (pp === null ? `${primary.station.name}: zu wenig Daten` : `${primary.station.name} nur ${pct(pp)}`) : "";
  return pick(
    best,
    `${primTxt ? primTxt + " – " : ""}besser ${best.station.name}${dist}: ${pct(best.prediction.probability!)} (Ø ${best.prediction.avg_bikes} Velos).`,
  );
}

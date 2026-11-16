/**
 * Zeitzonen- und Slot-Logik für Europe/Zurich – ohne Intl (spart CPU im Worker).
 *
 * Regel (EU/CH seit 1996): Sommerzeit (UTC+2) vom letzten Sonntag im März 01:00 UTC
 * bis zum letzten Sonntag im Oktober 01:00 UTC, sonst UTC+1.
 */

export const SLOTS_PER_DAY = 96;
export const SLOTS_PER_WEEK = 7 * SLOTS_PER_DAY; // 672
const MIN = 60_000;
const HOUR = 60 * MIN;

export interface LocalParts {
  year: number;
  month: number; // 1..12
  day: number;
  hour: number;
  minute: number;
  /** Wochentag, Montag = 0 … Sonntag = 6 */
  dow: number;
}

/** UTC-ms des letzten Sonntags im Monat (month 1..12) um 01:00 UTC. */
function lastSunday0100Utc(year: number, month: number): number {
  const lastDay = new Date(Date.UTC(year, month, 0)); // Tag 0 des Folgemonats
  const back = lastDay.getUTCDay(); // 0 = Sonntag
  return Date.UTC(year, month - 1, lastDay.getUTCDate() - back, 1, 0);
}

/** Offset von Europe/Zurich zu UTC in Minuten (60 oder 120) für einen UTC-Zeitpunkt. */
export function zurichOffsetMinutes(utcMs: number): number {
  const year = new Date(utcMs).getUTCFullYear();
  const start = lastSunday0100Utc(year, 3);
  const end = lastSunday0100Utc(year, 10);
  return utcMs >= start && utcMs < end ? 120 : 60;
}

/** Lokale Kalenderteile (Europe/Zurich) für einen UTC-Zeitpunkt. */
export function toZurich(utcMs: number): LocalParts {
  const d = new Date(utcMs + zurichOffsetMinutes(utcMs) * MIN);
  return {
    year: d.getUTCFullYear(),
    month: d.getUTCMonth() + 1,
    day: d.getUTCDate(),
    hour: d.getUTCHours(),
    minute: d.getUTCMinutes(),
    dow: (d.getUTCDay() + 6) % 7,
  };
}

/**
 * Lokale Zürcher Wanduhrzeit → UTC-ms. Bei nicht existierenden Zeiten (Frühlings-Lücke
 * 02:00–03:00) ergibt sich die entsprechende Sommerzeit-Zeit, bei doppelten Zeiten
 * (Herbst 02:00–03:00) die erste (Sommerzeit-)Variante.
 */
export function zurichToUtc(year: number, month: number, day: number, hour: number, minute: number): number {
  const asUtc = Date.UTC(year, month - 1, day, hour, minute);
  const off1 = zurichOffsetMinutes(asUtc - 2 * HOUR);
  const guess = asUtc - off1 * MIN;
  const off2 = zurichOffsetMinutes(guess);
  return off2 === off1 ? guess : asUtc - off2 * MIN;
}

export function daySlot(hour: number, minute: number): number {
  return hour * 4 + Math.floor(minute / 15);
}

/** Wochen-Slot 0..671 = dow*96 + 15-Min-Slot. */
export function weekSlot(dow: number, hour: number, minute: number): number {
  return dow * SLOTS_PER_DAY + daySlot(hour, minute);
}

export function weekSlotOf(utcMs: number): number {
  const p = toZurich(utcMs);
  return weekSlot(p.dow, p.hour, p.minute);
}

/** ±k Nachbar-Slots mit Umbruch So 23:45 ↔ Mo 00:00. */
export function neighborSlots(ws: number, k = 1): number[] {
  const out: number[] = [];
  for (let i = -k; i <= k; i++) out.push((((ws + i) % SLOTS_PER_WEEK) + SLOTS_PER_WEEK) % SLOTS_PER_WEEK);
  return out;
}

const DOW_DE = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"];

export function slotLabel(ws: number): string {
  const dow = Math.floor(ws / SLOTS_PER_DAY);
  const s = ws % SLOTS_PER_DAY;
  const hh = String(Math.floor(s / 4)).padStart(2, "0");
  const mm = String((s % 4) * 15).padStart(2, "0");
  return `${DOW_DE[dow]} ${hh}:${mm}`;
}

const pad = (n: number) => String(n).padStart(2, "0");

/** "2026-11-16T14:05+01:00" */
export function formatZurich(utcMs: number): string {
  const p = toZurich(utcMs);
  const off = zurichOffsetMinutes(utcMs) / 60;
  return `${p.year}-${pad(p.month)}-${pad(p.day)}T${pad(p.hour)}:${pad(p.minute)}+${pad(off)}:00`;
}

export interface ParsedAt {
  utcMs: number;
  local: LocalParts;
  wslot: number;
}

export class BadInput extends Error {}

const RE_HHMM = /^(\d{1,2}):(\d{2})$/;
const RE_ISO = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$/;

/**
 * Parst `at`:
 *  - fehlt  → jetzt
 *  - "HH:MM" → heute (Zürcher Datum) um HH:MM Zürcher Zeit
 *  - ISO mit Z/Offset → absoluter Zeitpunkt
 *  - ISO ohne Offset → Zürcher Wanduhrzeit
 */
export function parseAt(at: string | null | undefined, nowMs: number): ParsedAt {
  if (at == null || at.trim() === "") return fromUtc(nowMs);
  const s = at.trim();
  let m = RE_HHMM.exec(s);
  if (m) {
    const h = Number(m[1]);
    const mi = Number(m[2]);
    checkHm(h, mi);
    const today = toZurich(nowMs);
    return fromUtc(zurichToUtc(today.year, today.month, today.day, h, mi), { h, mi });
  }
  m = RE_ISO.exec(s);
  if (m) {
    const [y, mo, d, h, mi] = [m[1], m[2], m[3], m[4], m[5]].map(Number) as [number, number, number, number, number];
    checkHm(h, mi);
    if (mo < 1 || mo > 12 || d < 1 || d > 31) throw new BadInput(`Ungültiges Datum: ${s}`);
    if (m[7]) {
      const ms = Date.parse(s.replace(" ", "T"));
      if (Number.isNaN(ms)) throw new BadInput(`Ungültiger Zeitpunkt: ${s}`);
      return fromUtc(ms);
    }
    return fromUtc(zurichToUtc(y, mo, d, h, mi), { h, mi });
  }
  throw new BadInput(`'at' muss HH:MM oder ISO-8601 sein, war: ${s}`);
}

function checkHm(h: number, mi: number) {
  if (h > 23 || mi > 59) throw new BadInput(`Ungültige Uhrzeit ${h}:${mi}`);
}

/** wanted: gewünschte Wanduhrzeit – bleibt auch in der DST-Lücke für den Slot massgebend. */
function fromUtc(utcMs: number, wanted?: { h: number; mi: number }): ParsedAt {
  const local = toZurich(utcMs);
  const h = wanted?.h ?? local.hour;
  const mi = wanted?.mi ?? local.minute;
  return { utcMs, local, wslot: weekSlot(local.dow, h, mi) };
}

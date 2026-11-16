import { describe, expect, it } from "vitest";
import {
  neighborSlots,
  parseAt,
  slotLabel,
  toZurich,
  weekSlot,
  weekSlotOf,
  zurichOffsetMinutes,
  zurichToUtc,
  BadInput,
  formatZurich,
} from "../src/time";

const intl = new Intl.DateTimeFormat("en-GB", {
  timeZone: "Europe/Zurich",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  weekday: "short",
  hourCycle: "h23",
});
const WD = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

function viaIntl(ms: number) {
  const p = Object.fromEntries(intl.formatToParts(new Date(ms)).map((x) => [x.type, x.value]));
  return {
    year: +p.year!, month: +p.month!, day: +p.day!, hour: +p.hour!, minute: +p.minute!,
    dow: WD.indexOf(p.weekday!),
  };
}

describe("Zeitzone Europe/Zurich", () => {
  it("stimmt mit Intl überein (Stichprobe über mehrere Jahre, inkl. DST-Wechsel)", () => {
    const start = Date.UTC(2024, 0, 1);
    const end = Date.UTC(2031, 0, 1);
    // alle 37 Minuten + 13 Stunden ≈ zufällig verteilte Stichproben
    for (let t = start; t < end; t += (13 * 60 + 37) * 60_000) {
      expect(toZurich(t)).toEqual(viaIntl(t));
    }
  });

  it("DST-Grenzen 2026 exakt", () => {
    // Sommerzeit beginnt 29.03.2026 01:00 UTC, endet 25.10.2026 01:00 UTC
    expect(zurichOffsetMinutes(Date.UTC(2026, 2, 29, 0, 59))).toBe(60);
    expect(zurichOffsetMinutes(Date.UTC(2026, 2, 29, 1, 0))).toBe(120);
    expect(zurichOffsetMinutes(Date.UTC(2026, 9, 25, 0, 59))).toBe(120);
    expect(zurichOffsetMinutes(Date.UTC(2026, 9, 25, 1, 0))).toBe(60);
  });

  it("zurichToUtc ist die Umkehrung (auch rund um DST)", () => {
    for (let t = Date.UTC(2026, 0, 1); t < Date.UTC(2027, 0, 1); t += 47 * 60_000 * 7) {
      const p = toZurich(t);
      const back = zurichToUtc(p.year, p.month, p.day, p.hour, p.minute);
      // Doppelte Stunde im Herbst: erste Variante
      if (back !== Math.floor(t / 60_000) * 60_000) {
        expect(toZurich(back)).toMatchObject({ hour: p.hour, minute: p.minute, day: p.day });
      }
    }
    expect(zurichToUtc(2026, 7, 1, 8, 0)).toBe(Date.UTC(2026, 6, 1, 6, 0));
    expect(zurichToUtc(2026, 12, 1, 8, 0)).toBe(Date.UTC(2026, 11, 1, 7, 0));
  });
});

describe("Slots", () => {
  it("weekSlot / Label", () => {
    expect(weekSlot(0, 0, 0)).toBe(0);
    expect(weekSlot(6, 23, 59)).toBe(671);
    expect(weekSlot(1, 7, 44)).toBe(96 + 30);
    expect(slotLabel(96 + 30)).toBe("Di 07:30");
  });

  it("weekSlotOf berücksichtigt Sommer-/Winterzeit", () => {
    // Montag 16.11.2026 07:50 lokal = 06:50 UTC (Winterzeit)
    expect(weekSlotOf(Date.UTC(2026, 10, 16, 6, 50))).toBe(weekSlot(0, 7, 50));
    // Montag 15.06.2026 07:50 lokal = 05:50 UTC (Sommerzeit)
    expect(weekSlotOf(Date.UTC(2026, 5, 15, 5, 50))).toBe(weekSlot(0, 7, 50));
  });

  it("Nachbar-Slots mit Wochen-Umbruch", () => {
    expect(neighborSlots(0)).toEqual([671, 0, 1]);
    expect(neighborSlots(671)).toEqual([670, 671, 0]);
    expect(neighborSlots(100, 2)).toEqual([98, 99, 100, 101, 102]);
  });
});

describe("parseAt", () => {
  const now = Date.UTC(2026, 10, 16, 13, 0); // Mo 16.11.2026 14:00 Zürich
  it("ohne at → jetzt", () => {
    expect(parseAt(null, now).wslot).toBe(weekSlot(0, 14, 0));
  });
  it("HH:MM → heute lokal", () => {
    const r = parseAt("7:45", now);
    expect(r.wslot).toBe(weekSlot(0, 7, 45));
    expect(r.utcMs).toBe(Date.UTC(2026, 10, 16, 6, 45));
  });
  it("ISO ohne Offset = Zürcher Zeit", () => {
    const r = parseAt("2026-06-17T08:10", now); // Mittwoch, Sommerzeit
    expect(r.wslot).toBe(weekSlot(2, 8, 10));
    expect(r.utcMs).toBe(Date.UTC(2026, 5, 17, 6, 10));
  });
  it("ISO mit Z/Offset = absolut", () => {
    expect(parseAt("2026-06-17T06:10Z", now).wslot).toBe(weekSlot(2, 8, 10));
    expect(parseAt("2026-06-17T08:10+02:00", now).wslot).toBe(weekSlot(2, 8, 10));
  });
  it("ungültige Eingaben", () => {
    expect(() => parseAt("25:00", now)).toThrow(BadInput);
    expect(() => parseAt("morgen", now)).toThrow(BadInput);
    expect(() => parseAt("2026-13-01T08:00", now)).toThrow(BadInput);
  });
  it("formatZurich", () => {
    expect(formatZurich(Date.UTC(2026, 5, 17, 6, 10))).toBe("2026-06-17T08:10+02:00");
  });
});

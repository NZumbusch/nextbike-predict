import { describe, expect, it } from "vitest";
import fixture from "./fixtures/feed-luzern.json";
import { parseFeed } from "../src/feed";
import { currentMaps, diff, isEmpty, keyframe } from "../src/tracker";
import { clone, moveBike, places } from "./helpers";

const cur = (j: unknown) => currentMaps(parseFeed(j, 126, 20));

describe("Velo-Tracking: Feed → Orte", () => {
  it("kennt jede Velonummer mit Ort (Station oder Koordinaten)", () => {
    const f = parseFeed(fixture, 126, 20);
    expect(f.bikes.get("401197")).toBe(3024956); // an Neustadtstrasse 10
    expect(f.bikes.get("401529")).toEqual([47.04904, 8.31191]); // frei abgestellt
    const total = places(clone()).reduce((a, p) => a + p.bike_numbers.length, 0);
    expect(f.bikes.size).toBe(total);
  });
  it("Velotypen nur aus Vollabruf (bike_list)", () => {
    expect(parseFeed(fixture, 126, 20).bikeTypes.size).toBe(0);
    const j = clone();
    places(j)[0]!.bike_list = [{ number: "999", bike_type: 183 }];
    expect(parseFeed(j, 126, 20).bikeTypes.get("999")).toBe("183");
  });
});

describe("diff", () => {
  const base = cur(fixture);

  it("keine Änderung ⇒ leeres Delta", () => {
    expect(isEmpty(diff(base, cur(fixture)))).toBe(true);
  });

  it("Ausleihe: Velo verschwindet ⇒ [nr, station, 0] + Stationsänderung", () => {
    const d = diff(base, cur(moveBike(clone(), "401197", null)));
    expect(d.bk).toEqual([["401197", 3024956, 0]]);
    expect(d.st).toEqual([[3024956, 0, 0, 0, 1]]); // [uid, verfügbar, E, reserviert, aktiv]
  });

  it("Rückgabe an anderer Station ⇒ [nr, 0, neue station]", () => {
    const away = cur(moveBike(clone(), "401197", null));
    const back = cur(moveBike(clone(), "401197", 70900));
    const d = diff(away, back);
    expect(d.bk).toEqual([["401197", 0, 70900]]);
    expect(d.st.map((x) => x[0])).toEqual([70900]);
  });

  it("direkte Verschiebung (z. B. Umverteilung) ⇒ [nr, A, B]", () => {
    const d = diff(base, cur(moveBike(clone(), "401197", 70900)));
    expect(d.bk).toEqual([["401197", 3024956, 70900]]);
  });

  it("frei abgestelltes Velo bewegt sich ⇒ Koordinaten im Delta", () => {
    const j = clone();
    const ff = places(j).find((p) => p.name === "BIKE 401529")!;
    ff.lat = 47.05;
    const d = diff(base, cur(j));
    expect(d.bk).toEqual([["401529", [47.04904, 8.31191], [47.05, 8.31191]]]);
  });

  it("Station verschwindet ⇒ [uid]", () => {
    const j = clone();
    j.countries[0].cities[0].places = places(j).filter((p) => p.uid !== 38568);
    expect(diff(base, cur(j)).st).toContainEqual([38568]);
  });

  it("Keyframe enthält alles", () => {
    const k = keyframe(base);
    expect(k.st.length).toBe(25);
    expect(k.bk.length).toBe(Object.keys(base.bk).length);
    expect(k.bk.every((b) => b[1] === 0)).toBe(true);
  });
});

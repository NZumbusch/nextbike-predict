"""Prognose: Anteil Beobachtungen mit ≥ 1 Velo im Wochen-Slot ±1 (simple Statistik)."""
from __future__ import annotations

from dataclasses import dataclass

from .config import Station
from .timeutil import neighbor_slots, slot_label


@dataclass
class SlotStat:
    n: int = 0
    n_avail: int = 0
    sum_bikes: int = 0
    n_e_avail: int = 0
    sum_e: int = 0


def predict(stats: dict[int, SlotStat] | None, wslot: int, min_obs: int, smooth: int = 1) -> dict:
    slots = neighbor_slots(wslot, smooth)
    agg = SlotStat()
    for ws in slots:
        s = (stats or {}).get(ws)
        if s:
            agg.n += s.n
            agg.n_avail += s.n_avail
            agg.sum_bikes += s.sum_bikes
            agg.n_e_avail += s.n_e_avail
            agg.sum_e += s.sum_e
    base = {"slot": slot_label(wslot), "slots_used": [slot_label(x) for x in slots], "n": agg.n}
    if agg.n < min_obs:
        return {
            **base,
            "status": "zu_wenig_daten",
            "probability": None,
            "avg_bikes": None,
            "probability_ebike": None,
            "avg_ebikes": None,
            "message": f"zu wenig Daten ({agg.n} Beobachtungen, mindestens {min_obs} nötig)",
        }
    return {
        **base,
        "status": "ok",
        "probability": round(agg.n_avail / agg.n, 3),
        "avg_bikes": round(agg.sum_bikes / agg.n, 2),
        "probability_ebike": round(agg.n_e_avail / agg.n, 3),
        "avg_ebikes": round(agg.sum_e / agg.n, 2),
    }


def _pct(p: float) -> str:
    return f"{round(p * 100)} %"


def recommend(options: list[tuple[Station, dict]], threshold: float) -> dict:
    ok = [(s, p) for s, p in options if p["status"] == "ok"]
    if not ok:
        return {"station_id": None, "alias": None, "name": None, "probability": None, "text": "Zu wenig Daten für eine Empfehlung."}

    def pick(s: Station, p: dict, text: str) -> dict:
        return {"station_id": s.id, "alias": s.alias, "name": s.name, "probability": p["probability"], "text": text}

    primary = next(((s, p) for s, p in options if s.primary), None)
    pp = primary[1]["probability"] if primary and primary[1]["status"] == "ok" else None
    if primary and pp is not None and pp >= threshold:
        return pick(*primary, f"{primary[0].name}: {_pct(pp)} (Ø {primary[1]['avg_bikes']} Velos) – Stammstation passt.")
    best = max(ok, key=lambda x: x[1]["probability"])
    if primary and best[0].id == primary[0].id:
        return pick(*primary, f"{primary[0].name}: nur {_pct(pp)}, aber keine Ausweichstation ist besser.")  # type: ignore[arg-type]
    dist = f" ({best[0].distance_m} m)" if best[0].distance_m else ""
    prim = ""
    if primary:
        prim = f"{primary[0].name}: zu wenig Daten" if pp is None else f"{primary[0].name} nur {_pct(pp)}"
    return pick(
        *best,
        f"{prim + ' – ' if prim else ''}besser {best[0].name}{dist}: {_pct(best[1]['probability'])} (Ø {best[1]['avg_bikes']} Velos).",
    )

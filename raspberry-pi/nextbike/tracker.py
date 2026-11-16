"""Änderungserkennung und Fahrten-Erkennung pro Abruf (rein funktional bis auf den Zustand).

Ort eines Velos: Station-uid (int), (lat, lng) bei frei abgestellt, None = nicht sichtbar
(ausgeliehen, im Depot, in Reparatur).

Fahrt: Velo verschwindet an Ort A (Ausleihe) und taucht später an Ort B auf (Rückgabe).
Wechselt ein Velo zwischen zwei Abrufen direkt von A nach B, wird es als "direkte" Fahrt
markiert (kurze Fahrt < 1 Abrufintervall oder Umverteilung). Kommen mehrere Velos im selben
Abruf an derselben Station an, ist das ein starker Hinweis auf Umverteilung (arrivals_same_poll).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .feed import Loc, ParsedFeed

StTuple = tuple[int, int, int, int]  # (verfügbar, E, reserviert, aktiv)


@dataclass
class Trip:
    bike: str
    start_ts: int
    start: Loc
    end_ts: int
    end: Loc
    direct: bool
    arrivals_same_poll: int


@dataclass
class Step:
    ts: int
    prev_ts: int | None
    station_changes: list[tuple[int, StTuple | None]] = field(default_factory=list)
    moves: list[tuple[str, Loc | None, Loc | None]] = field(default_factory=list)
    trips: list[Trip] = field(default_factory=list)
    initial: bool = False


def _norm(loc) -> Loc | None:
    if loc is None:
        return None
    if isinstance(loc, (list, tuple)):
        return (float(loc[0]), float(loc[1]))
    return int(loc)


class Tracker:
    """Hält den letzten Zustand. to_json/from_json für Persistenz zwischen Neustarts."""

    def __init__(self) -> None:
        self.ts: int | None = None
        self.st: dict[int, StTuple] = {}
        self.bk: dict[str, Loc] = {}
        self.away: dict[str, tuple[int, Loc]] = {}  # Velo → (seit, letzter Ort)

    # ------------------------------------------------------------ Persistenz
    def to_json(self) -> dict:
        return {
            "ts": self.ts,
            "st": {str(k): list(v) for k, v in self.st.items()},
            "bk": {k: (list(v) if isinstance(v, tuple) else v) for k, v in self.bk.items()},
            "away": {k: [t, list(l) if isinstance(l, tuple) else l] for k, (t, l) in self.away.items()},
        }

    @classmethod
    def from_json(cls, d: dict | None) -> "Tracker":
        t = cls()
        if not d:
            return t
        t.ts = d.get("ts")
        t.st = {int(k): tuple(v) for k, v in d.get("st", {}).items()}  # type: ignore[misc]
        t.bk = {k: _norm(v) for k, v in d.get("bk", {}).items()}  # type: ignore[misc]
        t.away = {k: (int(v[0]), _norm(v[1])) for k, v in d.get("away", {}).items()}  # type: ignore[misc]
        return t

    # ------------------------------------------------------------ Schritt
    def step(self, ts: int, feed: ParsedFeed) -> Step:
        cur_st: dict[int, StTuple] = {s.uid: (s.bikes, s.ebikes, s.booked, 1 if s.active else 0) for s in feed.stations}
        cur_bk: dict[str, Loc] = dict(feed.bikes)
        step = Step(ts=ts, prev_ts=self.ts, initial=self.ts is None)

        for uid, t in cur_st.items():
            if self.st.get(uid) != t:
                step.station_changes.append((uid, t))
        for uid in self.st:
            if uid not in cur_st:
                step.station_changes.append((uid, None))

        if not step.initial:
            for n, loc in cur_bk.items():
                a = self.bk.get(n)
                if a != loc:
                    step.moves.append((n, a, loc))
            for n, a in self.bk.items():
                if n not in cur_bk:
                    step.moves.append((n, a, None))

            arrivals: dict[int, int] = {}
            for _, _, b in step.moves:
                if isinstance(b, int):
                    arrivals[b] = arrivals.get(b, 0) + 1

            for n, a, b in step.moves:
                batch = arrivals.get(b, 1) if isinstance(b, int) else 1
                if a is not None and b is None:
                    self.away[n] = (ts, a)
                elif a is None and b is not None:
                    dep = self.away.pop(n, None)
                    if dep:
                        step.trips.append(Trip(n, dep[0], dep[1], ts, b, False, batch))
                elif a is not None and b is not None:
                    step.trips.append(Trip(n, self.ts or ts, a, ts, b, True, batch))  # type: ignore[arg-type]

        self.ts, self.st, self.bk = ts, cur_st, cur_bk
        return step

    def prune_away(self, now: int, max_days: int = 14) -> int:
        cut = now - max_days * 86400
        old = [k for k, (t, _) in self.away.items() if t < cut]
        for k in old:
            del self.away[k]
        return len(old)


def loc_coords(loc: Loc | None, coords: dict[int, tuple[float, float]]) -> tuple[int | None, float | None, float | None]:
    """→ (uid, lat, lng); frei abgestellt: (None, lat, lng); nicht sichtbar: (None, None, None)."""
    if loc is None:
        return None, None, None
    if isinstance(loc, tuple):
        return None, loc[0], loc[1]
    c = coords.get(loc)
    return loc, (c[0] if c else None), (c[1] if c else None)


def haversine_m(a: tuple[float | None, float | None], b: tuple[float | None, float | None]) -> int | None:
    if None in a or None in b:
        return None
    lat1, lng1, lat2, lng2 = map(math.radians, (a[0], a[1], b[0], b[1]))  # type: ignore[arg-type]
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    return round(2 * 6371000 * math.asin(math.sqrt(h)))

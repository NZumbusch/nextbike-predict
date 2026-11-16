"""Zustandsverfolgung pro Quelle: erzeugt aus aufeinanderfolgenden Snapshots
"Spells" (Zeitintervalle mit konstantem Zustand) und Fahrten.

Vehicle-Spell: Fahrzeug X stand von start_ts bis end_ts an Ort/Station Y (Zustand unverändert).
  start_prev_ts / end_next_ts = letzte Beobachtung davor / erste danach ⇒ exakte Unsicherheitsfenster.
Station-Spell: Station hatte von start_ts bis end_ts genau diesen Bestand.

Fahrt (trip), nur bei stabiler Fahrzeug-ID:
  ride         Fahrzeug verschwindet an A (ausgeliehen) und taucht an B wieder auf
  round_trip   … taucht nach ≥ round_trip_min_s wieder am selben Ort auf
  visible_move Fahrzeug wechselt zwischen zwei Abrufen direkt den Ort (kurze Fahrt, Umverteilung)

Identität: Anbieter, die die ID ständig wechseln (z. B. bei jedem Abruf), werden auf
Positions-Identität umgestellt ("@lat,lon,typ#n"): geparkte Fahrzeuge bleiben so ein Spell,
Bewegungen erscheinen als Abfahrt (Spell endet) und Ankunft (neuer Spell).
"""
from __future__ import annotations

import fnmatch
import logging
import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Iterator

from .model import Snapshot, StationObs, VehicleObs

log = logging.getLogger(__name__)

VKey = tuple[str, str]  # (Anbieter, Fahrzeug-Schlüssel)
SKey = tuple[str, str]  # (Anbieter, Station)


def haversine_m(lat1, lon1, lat2, lon2) -> float | None:
    if None in (lat1, lon1, lat2, lon2):
        return None
    p1, p2 = math.radians(lat1), math.radians(lat2)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * 6371000 * math.asin(min(1.0, math.sqrt(h)))


@dataclass(slots=True)
class OpenV:
    id: int
    obs: VehicleObs
    key: str
    start_ts: int
    last_ts: int


@dataclass(slots=True)
class ClosedRef:
    spell_id: int
    lat: float | None
    lon: float | None
    station_id: str | None
    vehicle_type: str | None
    end_ts: int
    end_next_ts: int


@dataclass(slots=True)
class OpenS:
    id: int
    obs: StationObs
    start_ts: int
    last_ts: int


@dataclass
class Changes:
    source: str
    obs_ts: int
    v_open: list[tuple] = field(default_factory=list)
    v_close: list[tuple] = field(default_factory=list)    # (end_ts, end_next_ts, reason, id)
    s_open: list[tuple] = field(default_factory=list)
    s_close: list[tuple] = field(default_factory=list)
    trips: list[tuple] = field(default_factory=list)
    provider_stats: list[tuple] = field(default_factory=list)
    mode_switches: list[tuple[str, str]] = field(default_factory=list)
    rekeyed: list[str] = field(default_factory=list)   # Anbieter mit einmaliger Komplett-Neuvergabe der IDs

    @property
    def n_vehicle_changes(self) -> int:
        return len(self.v_open) + len(self.v_close)


# Spaltenreihenfolge der Tupel (muss zu store.py passen)
V_COLS = ("id", "source", "provider", "vehicle_id", "raw_id", "id_mode", "vehicle_type", "lat", "lon", "station_id",
          "reserved", "disabled", "range_m", "start_ts", "start_prev_ts", "start_reason")
S_COLS = ("id", "source", "provider", "station_id", "bikes", "docks", "is_installed", "is_renting", "is_returning", "by_type",
          "start_ts", "start_prev_ts")
T_COLS = ("source", "provider", "vehicle_id", "vehicle_type", "kind", "dep_ts", "dep_next_ts", "arr_prev_ts", "arr_ts",
          "from_lat", "from_lon", "from_station", "to_lat", "to_lon", "to_station", "distance_m", "from_spell", "to_spell")
P_COLS = ("ts", "source", "provider", "id_mode", "n_vehicles", "n_new_ids", "n_gone_ids", "n_opened", "n_closed")


class Tracker:
    def __init__(self, source: str, *, move_tolerance_m: float = 25, trip_min_distance_m: float = 100,
                 round_trip_min_s: int = 300, trip_link_max_h: float = 72, id_strategy: str = "auto",
                 position_providers: list[str] | None = None, auto_churn_threshold: float = 0.5,
                 provider_absent_close_s: int = 6 * 3600, reshuffle_limit: int = 3,
                 next_vehicle_id: Callable[[], int] | None = None, next_station_id: Callable[[], int] | None = None):
        self.source = source
        self.tol = move_tolerance_m
        self.trip_min = trip_min_distance_m
        self.round_min = round_trip_min_s
        self.link_max_s = int(trip_link_max_h * 3600)
        self.strategy = id_strategy
        self.position_patterns = position_providers or []
        self.churn = auto_churn_threshold
        self._vid = next_vehicle_id or _counter()
        self._sid = next_station_id or _counter()
        self.open_v: dict[VKey, OpenV] = {}
        self.recent: dict[VKey, ClosedRef] = {}
        self.open_s: dict[SKey, OpenS] = {}
        self.last_v_ts: int | None = None
        self.last_s_ts: int | None = None
        self.prov_mode: dict[str, str] = {}          # durch auto-Erkennung auf "pos" umgestellt
        self.prev_raw_ids: dict[str, set[str]] = {}
        self._last_prune = 0
        # Teilausfall-Schutz: fehlt ein Anbieter komplett, bleiben seine Zustände offen
        self.absent_close_s = provider_absent_close_s
        self.absent_since: dict[tuple[str, str], int] = {}   # (v|s, Anbieter) → seit
        # Komplett-Neuvergaben der IDs je Anbieter (Zeitpunkte, 24 h) – ab reshuffle_limit ⇒ Positions-Identität
        self.reshuffle_limit = reshuffle_limit
        self.reshuffles: dict[str, list[int]] = {}

    def _absent(self, kind: str, present: set[str], open_counts: dict[str, int], ts: int, min_fleet: int = 10) -> set[str]:
        """Anbieter, die gerade komplett fehlen und (noch) nicht geschlossen werden sollen.
        Nur für Anbieter mit ≥ min_fleet offenen Zuständen – bei Kleinstflotten ist "alle weg" plausibel."""
        hold = set()
        for p in set(open_counts) - present:
            if open_counts[p] < min_fleet:
                continue
            since = self.absent_since.setdefault((kind, p), ts)
            if ts - since < self.absent_close_s:
                hold.add(p)
                if since == ts:
                    log.warning("[%s] Anbieter %s fehlt im Feed – Zustände bleiben offen (max. %d h)",
                                self.source, p, self.absent_close_s // 3600)
        for p in present:
            self.absent_since.pop((kind, p), None)
        return hold

    # ---------------------------------------------------------------- Identität
    def mode_for(self, provider: str) -> str:
        if self.strategy == "id":
            return "id"
        if self.strategy == "position" or any(fnmatch.fnmatch(provider, p) for p in self.position_patterns):
            return "pos"
        return self.prov_mode.get(provider, "id")

    def _keyed(self, vehicles: list[VehicleObs], ch: Changes) -> dict[VKey, tuple[VehicleObs, str]]:
        by_prov: dict[str, list[VehicleObs]] = defaultdict(list)
        for v in vehicles:
            by_prov[v.provider].append(v)
        cur: dict[VKey, tuple[VehicleObs, str]] = {}
        for prov, items in by_prov.items():
            raw = {v.vehicle_id for v in items}
            prev = self.prev_raw_ids.get(prov)
            new_ids = len(raw - prev) if prev is not None else 0
            gone_ids = len(prev - raw) if prev is not None else 0
            if (self.strategy == "auto" and self.mode_for(prov) == "id" and prev is not None and len(raw) >= 20
                    and new_ids / len(raw) > self.churn):
                hist = [t for t in self.reshuffles.get(prov, []) if t > ch.obs_ts - 86400] + [ch.obs_ts]
                self.reshuffles[prov] = hist
                if len(hist) >= self.reshuffle_limit:
                    self.prov_mode[prov] = "pos"
                    ch.mode_switches.append((prov, "pos"))
                    log.warning("[%s] %s vergibt IDs laufend neu (%d× in 24 h, zuletzt %d/%d) → Positions-Identität",
                                self.source, prov, len(hist), new_ids, len(raw))
                else:
                    ch.rekeyed.append(prov)
                    log.info("[%s] %s: Komplett-Neuvergabe der IDs (%d/%d) – Zustände neu verknüpft, keine Fahrten abgeleitet",
                             self.source, prov, new_ids, len(raw))
            self.prev_raw_ids[prov] = raw
            mode = self.mode_for(prov)
            if mode == "pos":
                cur.update(self._position_keys(prov, items))
            else:
                for v in items:
                    cur.setdefault((prov, v.vehicle_id), (v, "id"))
            ch.provider_stats.append([ch.obs_ts, self.source, prov, mode, len(items), new_ids, gone_ids, 0, 0])
        return cur

    def _position_keys(self, prov: str, items: list[VehicleObs]) -> dict[VKey, tuple[VehicleObs, str]]:
        """Positions-Identität mit Toleranz: Jedes Fahrzeug übernimmt den Schlüssel des nächsten offenen
        Zustands (gleicher Typ) innerhalb von move_tolerance_m. GPS-Rauschen von wenigen Metern erzeugt so
        keine Schein-Abfahrten/-Ankünfte. Nur wirklich neue Positionen erhalten einen neuen Schlüssel."""
        cell = max(self.tol, 1.0) / 111_000  # Rastergrösse in Grad (Breite)

        def cell_of(lat: float, lon: float) -> tuple[int, int]:
            return int(lat // cell), int(lon * math.cos(math.radians(lat)) // cell)

        grid: dict[tuple[int, int], list[tuple[VKey, OpenV]]] = defaultdict(list)
        for key, o in self.open_v.items():
            if key[0] == prov and key[1].startswith("@") and o.obs.lat is not None and o.obs.lon is not None:
                grid[cell_of(o.obs.lat, o.obs.lon)].append((key, o))
        claimed: set[VKey] = set()
        out: dict[VKey, tuple[VehicleObs, str]] = {}
        fresh: list[VehicleObs] = []
        for v in sorted(items, key=lambda x: (x.lat or 0, x.lon or 0, x.vehicle_type or "", x.vehicle_id)):
            if v.lat is None or v.lon is None:
                continue
            ci, cj = cell_of(v.lat, v.lon)
            best, best_d = None, self.tol
            for di in (-1, 0, 1):
                for dj in (-1, 0, 1):
                    for key, o in grid.get((ci + di, cj + dj), ()):
                        if key in claimed or o.obs.vehicle_type != v.vehicle_type:
                            continue
                        d = haversine_m(o.obs.lat, o.obs.lon, v.lat, v.lon)
                        if d is not None and d <= best_d:
                            best, best_d = key, d
            if best is not None:
                claimed.add(best)
                out[best] = (v, "pos")
            else:
                fresh.append(v)
        for v in fresh:  # neue Positionen: eindeutigen Schlüssel vergeben
            base = f"@{v.lat:.5f},{v.lon:.5f},{v.vehicle_type or ''}"
            k = 0
            while (prov, f"{base}#{k}") in self.open_v or (prov, f"{base}#{k}") in out:
                k += 1
            out[(prov, f"{base}#{k}")] = (v, "pos")
        return out

    # ---------------------------------------------------------------- Schritt
    def apply(self, snap: Snapshot) -> Changes | None:
        """None = Daten nicht neuer als der letzte Stand (Feed unverändert) ⇒ nichts zu tun."""
        ts = snap.obs_ts
        newest = max([t for t in (self.last_v_ts, self.last_s_ts) if t is not None], default=None)
        if newest is not None and ts <= newest:
            return None
        ch = Changes(self.source, ts)
        if snap.vehicles is not None:
            self._vehicles(snap.vehicles, ts, ch)
            self.last_v_ts = ts
        if snap.stations is not None:
            self._stations(snap.stations, ts, ch)
            self.last_s_ts = ts
        if ts - self._last_prune > 600:
            self._prune(ts)
        return ch

    def _open_v(self, ch: Changes, key: VKey, v: VehicleObs, mode: str, ts: int, prev_ts: int | None, reason: str) -> OpenV:
        o = OpenV(self._vid(), v, key[1], ts, ts)
        ch.v_open.append((o.id, self.source, key[0], key[1], v.vehicle_id if key[1] != v.vehicle_id else None, mode,
                          v.vehicle_type, v.lat, v.lon, v.station_id, int(v.reserved), int(v.disabled), v.range_m, ts, prev_ts,
                          reason))
        self.open_v[key] = o
        return o

    def _trip(self, ch: Changes, key: VKey, kind: str, dep_ts, dep_next, arr_prev, arr_ts, a, b: VehicleObs,
              from_spell: int, to_spell: int, dist: float | None) -> None:
        ch.trips.append((self.source, key[0], key[1], b.vehicle_type or a.vehicle_type, kind, dep_ts, dep_next, arr_prev, arr_ts,
                         a.lat, a.lon, a.station_id, b.lat, b.lon, b.station_id,
                         None if dist is None else round(dist), from_spell, to_spell))

    def _vehicles(self, vehicles: list[VehicleObs], ts: int, ch: Changes) -> None:
        prev_ts = self.last_v_ts
        cur = self._keyed(vehicles, ch)
        switched = {p for p, _ in ch.mode_switches} | set(ch.rekeyed)
        stats = {row[2]: row for row in ch.provider_stats}
        hold = self._absent("v", {k[0] for k in cur}, _count_providers(self.open_v), ts)
        for key in list(self.open_v):
            if key[0] in hold:
                continue
            o = self.open_v[key]
            hit = cur.get(key)
            if hit is None:
                reason = "rekey" if key[0] in switched else "gone"
                ch.v_close.append((o.last_ts, ts, reason, o.id))
                if self.mode_for(key[0]) == "id" and reason == "gone":
                    v = o.obs
                    self.recent[key] = ClosedRef(o.id, v.lat, v.lon, v.station_id, v.vehicle_type, o.last_ts, ts)
                del self.open_v[key]
                if key[0] in stats:
                    stats[key[0]][8] += 1
                continue
            v, mode = hit
            d = haversine_m(o.obs.lat, o.obs.lon, v.lat, v.lon)
            moved = d is not None and d > self.tol
            if moved or o.obs.state_key() != v.state_key():
                ch.v_close.append((o.last_ts, ts, "moved" if moved else "changed", o.id))
                n = self._open_v(ch, key, v, mode, ts, o.last_ts, "moved" if moved else "changed")
                stats[key[0]][7] += 1
                stats[key[0]][8] += 1
                if moved and mode == "id" and d >= self.trip_min:
                    self._trip(ch, key, "visible_move", o.last_ts, ts, o.last_ts, ts, o.obs, v, o.id, n.id, d)
            else:
                o.last_ts = ts
        for key, (v, mode) in cur.items():
            if key in self.open_v:
                continue
            first_after_rekey = key[0] in switched
            reason = "rekey" if first_after_rekey else ("initial" if prev_ts is None else "appeared")
            n = self._open_v(ch, key, v, mode, ts, None if first_after_rekey else prev_ts, reason)
            stats[key[0]][7] += 1
            ref = self.recent.pop(key, None) if mode == "id" else None
            if ref is None:
                continue
            d = haversine_m(ref.lat, ref.lon, v.lat, v.lon)
            gap = ts - ref.end_ts
            a = VehicleObs(key[0], key[1], ref.lat, ref.lon, ref.vehicle_type, ref.station_id)
            if d is not None and d >= self.trip_min:
                self._trip(ch, key, "ride", ref.end_ts, ref.end_next_ts, prev_ts, ts, a, v, ref.spell_id, n.id, d)
            elif gap >= self.round_min:
                self._trip(ch, key, "round_trip", ref.end_ts, ref.end_next_ts, prev_ts, ts, a, v, ref.spell_id, n.id, d)
        ch.provider_stats = [tuple(r) for r in ch.provider_stats]

    def _stations(self, stations: list[StationObs], ts: int, ch: Changes) -> None:
        prev_ts = self.last_s_ts
        cur = {(s.provider, s.station_id): s for s in stations}
        hold = self._absent("s", {k[0] for k in cur}, _count_providers(self.open_s), ts)
        for key in list(self.open_s):
            if key[0] in hold:
                continue
            o = self.open_s[key]
            s = cur.get(key)
            if s is not None and s.state_key() == o.obs.state_key():
                o.last_ts = ts
                continue
            ch.s_close.append((o.last_ts, ts, "gone" if s is None else "changed", o.id))
            del self.open_s[key]
            if s is not None:
                self._open_s(ch, key, s, ts, o.last_ts)
        for key, s in cur.items():
            if key not in self.open_s:
                self._open_s(ch, key, s, ts, prev_ts)

    def _open_s(self, ch: Changes, key: SKey, s: StationObs, ts: int, prev_ts: int | None) -> None:
        o = OpenS(self._sid(), s, ts, ts)
        ch.s_open.append((o.id, self.source, s.provider, s.station_id, s.bikes, s.docks, int(s.installed), int(s.renting),
                          int(s.returning), s.by_type, ts, prev_ts))
        self.open_s[key] = o

    def _prune(self, ts: int) -> None:
        cut = ts - self.link_max_s
        for k in [k for k, r in self.recent.items() if r.end_ts < cut]:
            del self.recent[k]
        self._last_prune = ts

    # ---------------------------------------------------------------- Wiederaufnahme nach Neustart
    def restore(self, open_v_rows: Iterator, open_s_rows: Iterator, recent_rows: Iterator,
                last_v_ts: int | None, last_s_ts: int | None, prov_modes: dict[str, str]) -> None:
        """Offene Spells aus der DB übernehmen. last_ts der offenen Spells = letzter erfolgreicher Abruf."""
        self.last_v_ts, self.last_s_ts = last_v_ts, last_s_ts
        self.prov_mode.update(prov_modes)
        for r in open_v_rows:
            v = VehicleObs(r["provider"], r["raw_id"] or r["vehicle_id"], r["lat"], r["lon"], r["vehicle_type"],
                           r["station_id"], bool(r["reserved"]), bool(r["disabled"]), r["range_m"])
            self.open_v[(r["provider"], r["vehicle_id"])] = OpenV(r["id"], v, r["vehicle_id"], r["start_ts"],
                                                                   max(r["start_ts"], last_v_ts or 0))
        for r in open_s_rows:
            s = StationObs(r["provider"], r["station_id"], r["bikes"], r["docks"], bool(r["is_installed"]),
                           bool(r["is_renting"]), bool(r["is_returning"]), r["by_type"])
            self.open_s[(r["provider"], r["station_id"])] = OpenS(r["id"], s, r["start_ts"], max(r["start_ts"], last_s_ts or 0))
        for r in recent_rows:
            key = (r["provider"], r["vehicle_id"])
            if key not in self.open_v:
                self.recent[key] = ClosedRef(r["id"], r["lat"], r["lon"], r["station_id"], r["vehicle_type"],
                                             r["end_ts"], r["end_next_ts"])


def _count_providers(d: dict) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for k in d:
        out[k[0]] += 1
    return out


def _counter() -> Callable[[], int]:
    n = [0]

    def nxt() -> int:
        n[0] += 1
        return n[0]

    return nxt

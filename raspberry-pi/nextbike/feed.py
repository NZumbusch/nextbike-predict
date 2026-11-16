"""nextbike-live.json abrufen, validieren, parsen (Felder am echten Feed geprüft)."""
from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import dataclass, field

from .config import EBIKE_TYPES

MAX_BIKES = 500
MIN_PLACES = 20

Loc = int | tuple[float, float]  # Station-uid oder (lat, lng) bei frei abgestelltem Velo


class FeedError(Exception):
    pass


@dataclass
class StationState:
    uid: int
    number: int | None
    name: str
    lat: float | None
    lng: float | None
    place_type: str | None
    bikes: int  # ausleihbar
    ebikes: int
    booked: int
    active: bool


@dataclass
class ParsedFeed:
    stations: list[StationState] = field(default_factory=list)
    bikes: dict[str, Loc] = field(default_factory=dict)
    bike_types: dict[str, str] = field(default_factory=dict)
    free_floating: int = 0

    def coords(self) -> dict[int, tuple[float, float]]:
        return {s.uid: (s.lat, s.lng) for s in self.stations if s.lat is not None and s.lng is not None}


def _count(v) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= MAX_BIKES else None


def _numbers(p: dict) -> list[str]:
    bn = p.get("bike_numbers")
    return [x for x in bn if isinstance(x, str) and 0 < len(x) < 20] if isinstance(bn, list) else []


def parse_feed(data, city_id: int, min_places: int = MIN_PLACES) -> ParsedFeed:
    if not isinstance(data, dict) or not isinstance(data.get("countries"), list):
        raise FeedError("Feld 'countries' fehlt")
    city = None
    for c in data["countries"]:
        for ci in (c or {}).get("cities") or []:
            if isinstance(ci, dict) and ci.get("uid") == city_id:
                city = ci
                break
        if city:
            break
    if city is None:
        raise FeedError(f"city {city_id} nicht im Feed")
    places = city.get("places")
    if not isinstance(places, list):
        raise FeedError("Feld 'places' fehlt")

    out = ParsedFeed()
    for p in places:
        if not isinstance(p, dict):
            continue
        for b in p.get("bike_list") or []:
            if isinstance(b, dict) and isinstance(b.get("number"), str) and b.get("bike_type") is not None:
                out.bike_types[b["number"]] = str(b["bike_type"])
        if p.get("spot") is not True:
            if p.get("bike") is True:
                out.free_floating += _count(p.get("bikes")) or 1
                if isinstance(p.get("lat"), (int, float)) and isinstance(p.get("lng"), (int, float)):
                    loc = (round(float(p["lat"]), 5), round(float(p["lng"]), 5))
                    for n in _numbers(p):
                        out.bikes[n] = loc
            continue
        uid = p.get("uid")
        if not isinstance(uid, int):
            continue
        bikes = _count(p.get("bikes_available_to_rent"))
        if bikes is None:
            bikes = _count(p.get("bikes"))
        if bikes is None:
            continue  # unplausibel ⇒ auslassen statt Müll speichern
        e = 0
        types = p.get("bike_types")
        if isinstance(types, dict):
            e = sum(_count(v) or 0 for k, v in types.items() if k in EBIKE_TYPES)
        for n in _numbers(p):
            out.bikes[n] = uid
        out.stations.append(
            StationState(
                uid=uid,
                number=p.get("number") if isinstance(p.get("number"), int) else None,
                name=p["name"] if isinstance(p.get("name"), str) else str(uid),
                lat=p.get("lat") if isinstance(p.get("lat"), (int, float)) else None,
                lng=p.get("lng") if isinstance(p.get("lng"), (int, float)) else None,
                place_type=p.get("place_type") if isinstance(p.get("place_type"), str) else None,
                bikes=bikes,
                ebikes=min(e, bikes),
                booked=_count(p.get("booked_bikes")) or 0,
                active=p.get("active_place") != 0 and p.get("maintenance") is not True,
            )
        )
    if len(out.stations) < min_places:
        raise FeedError(f"nur {len(out.stations)} Stationen im Feed (< {min_places}) – vermutlich gestört")
    return out


def fetch_feed(url: str, city_id: int, retries: int = 2, timeout: float = 15.0, opener=None) -> ParsedFeed:
    """Abruf mit Retry. urllib folgt Redirects und nutzt HTTPS_PROXY aus der Umgebung."""
    opener = opener or urllib.request.urlopen
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "nextbike-predict (private availability tracker)"})
            with opener(req, timeout=timeout) as r:
                if getattr(r, "status", 200) != 200:
                    raise FeedError(f"HTTP {r.status}")
                body = r.read()
            try:
                data = json.loads(body)
            except ValueError as e:
                raise FeedError("Antwort ist kein JSON") from e
            return parse_feed(data, city_id)
        except Exception as e:  # noqa: BLE001 – Netzwerkfehler aller Art
            last = e
            if attempt + 1 < retries:
                time.sleep(2)
    raise last if isinstance(last, FeedError) else FeedError(str(last))

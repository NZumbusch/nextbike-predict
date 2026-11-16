"""GBFS-Quelle (v2.x/v3-kompatibel lesend), z. B. der Schweizer Aggregator sharedmobility.ch
(alle Anbieter in einem Feed, Feld provider_id) oder der GBFS-Feed eines einzelnen Anbieters."""
from __future__ import annotations

import json
import logging
import time

from .. import http
from ..config import SourceConfig
from ..model import Snapshot, StationInfo, StationObs, VehicleObs

log = logging.getLogger(__name__)

VEHICLE_FEEDS = ("vehicle_status", "free_bike_status")  # v3 bzw. v2


def _b(v, default=False) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v.lower() in ("1", "true", "yes")
    return default


def _i(v) -> int | None:
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 else None


def _f(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _text(v) -> str | None:
    """GBFS v3 nutzt lokalisierte Texte [{text, language}], v2 einfache Strings."""
    if isinstance(v, str):
        return v
    if isinstance(v, list) and v and isinstance(v[0], dict):
        return v[0].get("text")
    return None


def strip_prefix(provider: str, ident: str) -> str:
    """sharedmobility.ch setzt "<provider>:" vor jede ID – spart Platz, ohne Information zu verlieren."""
    p = provider + ":"
    return ident[len(p):] if ident.startswith(p) else ident


def parse_vehicles(data: dict, default_provider: str) -> list[VehicleObs]:
    d = data.get("data") or {}
    items = d.get("vehicles") if isinstance(d.get("vehicles"), list) else d.get("bikes")
    out: list[VehicleObs] = []
    for b in items or []:
        if not isinstance(b, dict):
            continue
        vid = b.get("vehicle_id") or b.get("bike_id")
        if not isinstance(vid, str) or not vid:
            continue
        prov = b.get("provider_id") if isinstance(b.get("provider_id"), str) else default_provider
        lat, lon = _f(b.get("lat")), _f(b.get("lon"))
        if lat is not None and not (-90 <= lat <= 90 and -180 <= lon <= 180):  # type: ignore[operator]
            lat = lon = None
        st = b.get("station_id")
        vt = b.get("vehicle_type_id")
        out.append(
            VehicleObs(
                provider=prov,
                vehicle_id=strip_prefix(prov, vid),
                lat=round(lat, 6) if lat is not None else None,
                lon=round(lon, 6) if lon is not None else None,
                vehicle_type=strip_prefix(prov, vt) if isinstance(vt, str) else None,
                station_id=strip_prefix(prov, str(st)) if st not in (None, "") else None,
                reserved=_b(b.get("is_reserved")),
                disabled=_b(b.get("is_disabled")),
                range_m=_i(b.get("current_range_meters")),
            )
        )
    return out


def parse_station_status(data: dict, default_provider: str) -> list[StationObs]:
    out: list[StationObs] = []
    for s in (data.get("data") or {}).get("stations") or []:
        if not isinstance(s, dict) or s.get("station_id") in (None, ""):
            continue
        prov = s.get("provider_id") if isinstance(s.get("provider_id"), str) else default_provider
        bikes = _i(s.get("num_vehicles_available", s.get("num_bikes_available")))
        if bikes is None:
            continue
        by_type = None
        vta = s.get("vehicle_types_available")
        if isinstance(vta, list) and vta:
            m = {strip_prefix(prov, str(x.get("vehicle_type_id"))): _i(x.get("count")) for x in vta if isinstance(x, dict)}
            by_type = json.dumps(dict(sorted(m.items())), separators=(",", ":"))
        out.append(
            StationObs(
                provider=prov,
                station_id=strip_prefix(prov, str(s["station_id"])),
                bikes=bikes,
                docks=_i(s.get("num_docks_available")),
                installed=_b(s.get("is_installed"), True),
                renting=_b(s.get("is_renting"), True),
                returning=_b(s.get("is_returning"), True),
                by_type=by_type,
            )
        )
    return out


def parse_station_information(data: dict, default_provider: str) -> list[StationInfo]:
    out: list[StationInfo] = []
    for s in (data.get("data") or {}).get("stations") or []:
        if not isinstance(s, dict) or s.get("station_id") in (None, ""):
            continue
        prov = s.get("provider_id") if isinstance(s.get("provider_id"), str) else default_provider
        out.append(
            StationInfo(
                provider=prov,
                station_id=strip_prefix(prov, str(s["station_id"])),
                name=_text(s.get("name")),
                lat=_f(s.get("lat")),
                lon=_f(s.get("lon")),
                capacity=_i(s.get("capacity")),
                address=_text(s.get("address")),
            )
        )
    return out


class GbfsSource:
    """Liest gbfs.json (Feed-Verzeichnis) und fragt die Status-Feeds bei jedem Abruf ab,
    die Info-Feeds (Stationen, Anbieter, Fahrzeugtypen) seltener."""

    def __init__(self, cfg: SourceConfig, fetch=None):
        self.cfg = cfg
        self.fetch = fetch or http.fetch
        self.feeds: dict[str, str] = {}
        self.feeds_ts = 0.0
        self.info_ts = 0.0

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": self.cfg.auth} if self.cfg.auth else {}

    def _get(self, url: str):
        return self.fetch(url, headers=self.headers, timeout=self.cfg.timeout_s)

    def discover(self) -> None:
        r = self._get(self.cfg.url)
        d = r.json()
        feeds: dict[str, str] = {}
        data = d.get("data") or {}
        if isinstance(data.get("feeds"), list):  # v3
            lists = [data["feeds"]]
        else:  # v2: {lang: {feeds: [...]}}
            langs = [self.cfg.language] + [k for k in data if k != self.cfg.language]
            lists = [data[k]["feeds"] for k in langs if isinstance(data.get(k), dict) and isinstance(data[k].get("feeds"), list)][:1]
        for f in (lists[0] if lists else []):
            if isinstance(f, dict) and isinstance(f.get("name"), str) and isinstance(f.get("url"), str):
                feeds[f["name"]] = f["url"]
        if not feeds:
            raise http.FetchError(f"{self.cfg.url}: keine Feeds gefunden")
        self.feeds, self.feeds_ts = feeds, time.time()
        log.info("[%s] Feeds: %s", self.cfg.id, ", ".join(sorted(feeds)))

    def poll(self, now: float) -> Snapshot:
        if not self.feeds or now - self.feeds_ts > 6 * 3600:
            self.discover()
        snap = Snapshot(source=self.cfg.id, poll_ts=int(now), obs_ts=int(now))
        prov = self.cfg.provider or self.cfg.id
        lus: list[int] = []

        def load(name: str):
            r = self._get(self.feeds[name])
            snap.bytes_in += r.wire_bytes
            if name in self.cfg.raw_feeds:
                snap.raw[name] = r.body
            d = r.json()
            lu = _i(d.get("last_updated")) if not isinstance(d.get("last_updated"), str) else _iso(d["last_updated"])
            if lu:
                snap.feed_last_updated[name] = lu
            return d, lu

        vf = next((f for f in VEHICLE_FEEDS if f in self.feeds), None)
        if self.cfg.vehicles and vf:
            d, lu = load(vf)
            snap.vehicles = self._filter(parse_vehicles(d, prov))
            if lu:
                lus.append(lu)
        if self.cfg.stations and "station_status" in self.feeds:
            d, lu = load("station_status")
            snap.stations = self._filter(parse_station_status(d, prov))
            if lu:
                lus.append(lu)
        if now - self.info_ts >= self.cfg.info_interval_s:
            if "station_information" in self.feeds:
                d, _ = load("station_information")
                snap.station_info = self._filter(parse_station_information(d, prov))
            if "providers" in self.feeds:
                d, _ = load("providers")
                snap.providers = [p for p in (d.get("data") or {}).get("providers", []) if isinstance(p, dict)]
            if "vehicle_types" in self.feeds:
                d, _ = load("vehicle_types")
                snap.vehicle_types = [v for v in (d.get("data") or {}).get("vehicle_types", []) if isinstance(v, dict)]
            self.info_ts = now
        # Datenzeit = jüngstes last_updated, sofern plausibel (nicht in der Zukunft, nicht uralt)
        if lus and now - 3600 <= max(lus) <= now + 120:
            snap.obs_ts = max(lus)
        return snap

    def _filter(self, items):
        inc, exc = set(self.cfg.providers_include), set(self.cfg.providers_exclude)
        if not inc and not exc:
            return items
        return [x for x in items if (not inc or x.provider in inc) and x.provider not in exc]


def _iso(s: str) -> int | None:
    from datetime import datetime

    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None

"""Native nextbike-API (nextbike-live.json). Vorteil gegenüber GBFS: feste Velonummern
(bike_numbers) ⇒ jede Fahrt lässt sich exakt einem Velo zuordnen.

Anbieter-ID = "nextbike_<domain>" (gleich wie im sharedmobility.ch-Feed, z. B. nextbike_ch, nextbike_ms).
"""
from __future__ import annotations

import json
import logging

from .. import http
from ..config import SourceConfig
from ..model import Snapshot, StationInfo, StationObs, VehicleObs

log = logging.getLogger(__name__)
MIN_PLACES = 20


def _i(v) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 10_000 else None


def _f(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def parse_live(data: dict, types: dict[str, str]) -> tuple[list[VehicleObs], list[StationObs], list[StationInfo], dict[str, str]]:
    """→ (Fahrzeuge, Stationsstatus, Stationsinfos, Velotypen aus bike_list)."""
    if not isinstance(data, dict) or not isinstance(data.get("countries"), list):
        raise http.FetchError("nextbike: Feld 'countries' fehlt")
    vehicles: list[VehicleObs] = []
    stations: list[StationObs] = []
    infos: list[StationInfo] = []
    new_types: dict[str, str] = {}
    n_places = 0
    for c in data["countries"]:
        if not isinstance(c, dict):
            continue
        prov = f"nextbike_{c.get('domain') or 'xx'}"
        for ci in c.get("cities") or []:
            for p in (ci or {}).get("places") or []:
                if not isinstance(p, dict):
                    continue
                n_places += 1
                for b in p.get("bike_list") or []:
                    if isinstance(b, dict) and isinstance(b.get("number"), str) and b.get("bike_type") is not None:
                        new_types[b["number"]] = str(b["bike_type"])
                lat, lon = _f(p.get("lat")), _f(p.get("lng"))
                numbers = [n for n in p.get("bike_numbers") or [] if isinstance(n, str) and 0 < len(n) < 20]
                is_station = p.get("spot") is True
                sid = str(p["uid"]) if is_station and isinstance(p.get("uid"), int) else None
                for n in numbers:
                    vehicles.append(
                        VehicleObs(
                            provider=prov,
                            vehicle_id=n,
                            lat=round(lat, 6) if lat is not None else None,
                            lon=round(lon, 6) if lon is not None else None,
                            vehicle_type=types.get(n) or new_types.get(n),
                            station_id=sid,
                        )
                    )
                if not sid:
                    continue
                bikes = _i(p.get("bikes_available_to_rent"))
                if bikes is None:
                    bikes = _i(p.get("bikes"))
                if bikes is None:
                    continue
                racks = _i(p.get("bike_racks")) or 0
                bt = p.get("bike_types")
                by_type = json.dumps(dict(sorted(bt.items())), separators=(",", ":")) if isinstance(bt, dict) and bt else None
                stations.append(
                    StationObs(
                        provider=prov,
                        station_id=sid,
                        bikes=bikes,
                        docks=_i(p.get("free_racks")) if racks > 0 else None,
                        installed=p.get("active_place") != 0,
                        renting=p.get("maintenance") is not True,
                        returning=p.get("active_place") != 0,
                        by_type=by_type,
                    )
                )
                infos.append(
                    StationInfo(
                        provider=prov,
                        station_id=sid,
                        name=p.get("name") if isinstance(p.get("name"), str) else None,
                        lat=lat,
                        lon=lon,
                        capacity=racks or None,
                        address=p.get("address") if isinstance(p.get("address"), str) else None,
                    )
                )
    if len(stations) < MIN_PLACES:
        raise http.FetchError(f"nextbike: nur {len(stations)} Stationen (von {n_places} Orten) – Feed vermutlich gestört")
    return vehicles, stations, infos, new_types


class NextbikeSource:
    def __init__(self, cfg: SourceConfig, fetch=None):
        self.cfg = cfg
        self.fetch = fetch or http.fetch
        self.types: dict[str, str] = {}
        self.info_ts = 0.0

    def poll(self, now: float) -> Snapshot:
        full = bool(self.cfg.full_url) and now - self.info_ts >= self.cfg.info_interval_s
        r = self.fetch(self.cfg.full_url if full else self.cfg.url, timeout=self.cfg.timeout_s)
        vehicles, stations, infos, new_types = parse_live(r.json(), self.types)
        self.types.update(new_types)
        snap = Snapshot(source=self.cfg.id, poll_ts=int(now), obs_ts=int(now), bytes_in=r.wire_bytes)
        snap.vehicles = vehicles if self.cfg.vehicles else None
        snap.stations = stations if self.cfg.stations else None
        if full or not self.info_ts:
            snap.station_info = infos
            if full:
                snap.bike_types = {(v.provider, v.vehicle_id): v.vehicle_type for v in vehicles if v.vehicle_type}
            self.info_ts = now
        if "live" in self.cfg.raw_feeds:
            snap.raw["live"] = r.body
        return snap

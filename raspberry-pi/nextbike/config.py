"""Konfiguration aus config.toml + Umgebungsvariablen."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# Velo-Typen mit Elektroantrieb (GBFS vehicle_types nextbike_ch):
# 150/196 Velo, 183 E-Bike, 371 E-Cargobike
EBIKE_TYPES = frozenset({"183", "371"})
FEED_BASE = "https://maps2.nextbike.net/maps/nextbike-live.json"  # api.nextbike.net leitet hierhin um


@dataclass(frozen=True)
class Station:
    id: int
    alias: str
    name: str
    group: str = "start"
    primary: bool = False
    distance_m: int | None = None


@dataclass
class Config:
    city_id: int = 126
    poll_seconds: int = 60
    night_poll_seconds: int = 60
    night_from_hour: int = 1
    night_to_hour: int = 5
    full_feed_every_minutes: int = 60
    min_obs: int = 5
    plan_threshold: float = 0.8
    maintenance_hour: int = 3
    keep_local_backups: int = 7
    poll_log_days: int = 30
    stations: list[Station] = field(default_factory=list)
    data_dir: Path = Path("data")
    web_token: str = ""

    @property
    def db_path(self) -> Path:
        return self.data_dir / "nextbike.sqlite"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def feed_url(self) -> str:
        return f"{FEED_BASE}?city={self.city_id}&bikes=false"

    @property
    def feed_url_full(self) -> str:
        return f"{FEED_BASE}?city={self.city_id}"

    def find_station(self, key: str) -> Station | None:
        k = key.strip().lower()
        return next((s for s in self.stations if s.alias == k or str(s.id) == k), None)


def load_config(path: str | os.PathLike | None = None) -> Config:
    path = Path(path or os.environ.get("CONFIG_PATH", "config.toml"))
    raw = tomllib.loads(path.read_text("utf-8")) if path.exists() else {}
    feed = raw.get("feed", {})
    pred = raw.get("prediction", {})
    maint = raw.get("maintenance", {})
    cfg = Config(
        city_id=int(feed.get("city_id", 126)),
        poll_seconds=max(30, int(feed.get("poll_seconds", 60))),
        night_poll_seconds=max(30, int(feed.get("night_poll_seconds", 60))),
        night_from_hour=int(feed.get("night_from_hour", 1)),
        night_to_hour=int(feed.get("night_to_hour", 5)),
        full_feed_every_minutes=int(feed.get("full_feed_every_minutes", 60)),
        min_obs=int(pred.get("min_obs", 5)),
        plan_threshold=float(pred.get("plan_threshold", 0.8)),
        maintenance_hour=int(maint.get("hour", 3)),
        keep_local_backups=int(maint.get("keep_local_backups", 7)),
        poll_log_days=int(maint.get("poll_log_days", 30)),
        stations=[
            Station(
                id=int(s["id"]),
                alias=str(s["alias"]).lower(),
                name=str(s["name"]),
                group=str(s.get("group", "start")),
                primary=bool(s.get("primary", False)),
                distance_m=s.get("distance_m"),
            )
            for s in raw.get("stations", [])
        ],
        data_dir=Path(os.environ.get("DATA_DIR", "data")),
        web_token=os.environ.get("WEB_TOKEN", ""),
    )
    return cfg

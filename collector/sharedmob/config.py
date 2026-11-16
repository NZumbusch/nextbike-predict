"""Konfiguration: config.toml (Struktur) + Umgebungsvariablen (Pfade, Geheimnisse)."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SourceConfig:
    id: str
    kind: str                         # "gbfs" | "nextbike"
    url: str
    enabled: bool = True
    provider: str | None = None       # Standard-Anbieter-ID, falls der Feed keine provider_id liefert
    language: str = "en"
    auth: str | None = None           # Authorization-Header (sharedmobility.ch v2.3: E-Mail)
    vehicles: bool = True
    stations: bool = True
    info_interval_s: int = 6 * 3600
    timeout_s: float = 45.0
    providers_include: list[str] = field(default_factory=list)
    providers_exclude: list[str] = field(default_factory=list)
    raw_feeds: list[str] = field(default_factory=list)
    full_url: str | None = None       # nextbike: Abruf mit Velo-Liste (Velotypen)


@dataclass
class Config:
    interval_s: int = 60
    night_interval_s: int = 60
    night_from_hour: int = 1
    night_to_hour: int = 5
    # Tracking
    move_tolerance_m: float = 25.0
    trip_min_distance_m: float = 100.0
    round_trip_min_s: int = 300
    trip_link_max_h: float = 72.0
    id_strategy: str = "auto"         # auto | id | position
    position_providers: list[str] = field(default_factory=list)
    auto_churn_threshold: float = 0.5
    # Speicher
    hot_days: int = 21
    raw_every_min: int = 60
    raw_keep_local_days: int = 30
    archive_hour: int = 1
    sqlite_backups_keep: int = 3
    sources: list[SourceConfig] = field(default_factory=list)
    # Umgebung
    data_dir: Path = Path("data")
    web_token: str = ""

    @property
    def db_path(self) -> Path:
        return self.data_dir / "hot" / "sharedmob.sqlite"

    @property
    def archive_dir(self) -> Path:
        return self.data_dir / "archive"

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"


def load_config(path: str | os.PathLike | None = None, env: dict | None = None) -> Config:
    env = os.environ if env is None else env
    path = Path(path or env.get("CONFIG_PATH", "config.toml"))
    raw = tomllib.loads(path.read_text("utf-8")) if path.exists() else {}
    c, t, s = raw.get("collector", {}), raw.get("tracking", {}), raw.get("storage", {})
    auth_email = env.get("GBFS_AUTH_EMAIL", "").strip()
    sources = []
    for src in raw.get("sources", []):
        sc = SourceConfig(
            id=str(src["id"]),
            kind=str(src["kind"]),
            url=str(src["url"]),
            enabled=bool(src.get("enabled", True)),
            provider=src.get("provider"),
            language=str(src.get("language", "en")),
            vehicles=bool(src.get("vehicles", True)),
            stations=bool(src.get("stations", True)),
            info_interval_s=int(float(src.get("info_interval_min", 360)) * 60),
            timeout_s=float(src.get("timeout_s", 45)),
            providers_include=list(src.get("providers_include", [])),
            providers_exclude=list(src.get("providers_exclude", [])),
            raw_feeds=list(src.get("raw_feeds", [])),
            full_url=src.get("full_url"),
        )
        if src.get("auth_from_env") and auth_email:
            sc.auth = auth_email
        if sc.enabled:
            sources.append(sc)
    return Config(
        interval_s=max(30, int(c.get("interval_s", 60))),
        night_interval_s=max(30, int(c.get("night_interval_s", 60))),
        night_from_hour=int(c.get("night_from_hour", 1)),
        night_to_hour=int(c.get("night_to_hour", 5)),
        move_tolerance_m=float(t.get("move_tolerance_m", 25)),
        trip_min_distance_m=float(t.get("trip_min_distance_m", 100)),
        round_trip_min_s=int(t.get("round_trip_min_s", 300)),
        trip_link_max_h=float(t.get("trip_link_max_h", 72)),
        id_strategy=str(t.get("id_strategy", "auto")),
        position_providers=list(t.get("position_providers", [])),
        auto_churn_threshold=float(t.get("auto_churn_threshold", 0.5)),
        hot_days=int(s.get("hot_days", 21)),
        raw_every_min=int(s.get("raw_every_min", 60)),
        raw_keep_local_days=int(s.get("raw_keep_local_days", 30)),
        archive_hour=int(s.get("archive_hour", 1)),
        sqlite_backups_keep=int(s.get("sqlite_backups_keep", 3)),
        sources=sources,
        data_dir=Path(env.get("DATA_DIR", "data")),
        web_token=env.get("WEB_TOKEN", ""),
    )

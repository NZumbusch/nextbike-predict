"""Quellen-unabhängiges Datenmodell. Jede Quelle (GBFS, nextbike) liefert einen Snapshot."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class VehicleObs:
    provider: str
    vehicle_id: str
    lat: float | None
    lon: float | None
    vehicle_type: str | None = None
    station_id: str | None = None
    reserved: bool = False
    disabled: bool = False
    range_m: int | None = None  # Restreichweite (nur gespeichert, löst keinen neuen Zustand aus)

    def state_key(self) -> tuple:
        """Merkmale, deren Änderung einen neuen Zustand ("Spell") beginnt. Die Position wird separat
        mit einer Distanz-Toleranz verglichen (GPS-Rauschen soll keine Scheinbewegungen erzeugen)."""
        return (self.station_id, self.reserved, self.disabled, self.vehicle_type)


@dataclass(frozen=True, slots=True)
class StationObs:
    provider: str
    station_id: str
    bikes: int
    docks: int | None = None
    installed: bool = True
    renting: bool = True
    returning: bool = True
    by_type: str | None = None  # JSON {typ: anzahl}, falls die Quelle es liefert

    def state_key(self) -> tuple:
        return (self.bikes, self.docks, self.installed, self.renting, self.returning, self.by_type)


@dataclass(frozen=True, slots=True)
class StationInfo:
    provider: str
    station_id: str
    name: str | None
    lat: float | None
    lon: float | None
    capacity: int | None = None
    address: str | None = None


@dataclass
class Snapshot:
    """Ergebnis eines Abrufs einer Quelle."""
    source: str
    poll_ts: int                      # Zeitpunkt des Abrufs (Unix-s)
    obs_ts: int                       # Zeitpunkt der Daten (feed last_updated, falls plausibel)
    vehicles: list[VehicleObs] | None = None   # None = Feed nicht abgefragt / nicht vorhanden
    stations: list[StationObs] | None = None
    station_info: list[StationInfo] | None = None
    providers: list[dict] | None = None        # Rohdaten der Anbieter-Liste
    vehicle_types: list[dict] | None = None
    bike_types: dict[tuple[str, str], str] | None = None  # (Anbieter, Fahrzeug-ID) → Typ (nextbike)
    raw: dict[str, bytes] = field(default_factory=dict)   # Rohantworten (für Raw-Archiv)
    bytes_in: int = 0
    feed_last_updated: dict[str, int] = field(default_factory=dict)

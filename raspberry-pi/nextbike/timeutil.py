"""Zeitzone Europe/Zurich und Wochen-Slots (Wochentag × 15 Min, Mo = 0)."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Zurich")
SLOTS_PER_DAY = 96
SLOTS_PER_WEEK = 7 * SLOTS_PER_DAY
DOW_DE = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


def local(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, TZ)


def week_slot_of_dt(dt: datetime) -> int:
    return dt.weekday() * SLOTS_PER_DAY + dt.hour * 4 + dt.minute // 15


def week_slot(ts: float) -> int:
    return week_slot_of_dt(local(ts))


def neighbor_slots(ws: int, k: int = 1) -> list[int]:
    return [(ws + i) % SLOTS_PER_WEEK for i in range(-k, k + 1)]


def slot_label(ws: int) -> str:
    d, s = divmod(ws, SLOTS_PER_DAY)
    return f"{DOW_DE[d]} {s // 4:02d}:{(s % 4) * 15:02d}"


def local_midnight(ts: float) -> int:
    d = local(ts)
    return int(d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


class BadInput(ValueError):
    pass


_HHMM = re.compile(r"^(\d{1,2}):(\d{2})$")
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$")


def parse_at(at: str | None, now: float) -> tuple[datetime, int]:
    """'HH:MM' (heute, Zürich) | ISO ohne Offset (Zürich) | ISO mit Offset | leer = jetzt.
    Gibt (lokale Zeit, Wochen-Slot) zurück."""
    if not at or not at.strip():
        dt = local(now)
        return dt, week_slot_of_dt(dt)
    s = at.strip()
    if m := _HHMM.match(s):
        h, mi = int(m[1]), int(m[2])
        if h > 23 or mi > 59:
            raise BadInput(f"Ungültige Uhrzeit {s}")
        dt = local(now).replace(hour=h, minute=mi, second=0, microsecond=0)
        return dt, week_slot_of_dt(dt)
    if _ISO.match(s):
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError as e:
            raise BadInput(f"Ungültiger Zeitpunkt: {s}") from e
        dt = dt.replace(tzinfo=TZ) if dt.tzinfo is None else dt.astimezone(TZ)
        return dt, week_slot_of_dt(dt)
    raise BadInput(f"'at' muss HH:MM oder ISO-8601 sein, war: {s}")


def iso_local(ts: float) -> str:
    return local(ts).isoformat(timespec="minutes")


def iso_utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def fmt_hm(ts: float) -> str:
    return local(ts).strftime("%H:%M")


def fmt_dt(ts: float) -> str:
    return local(ts).strftime("%d.%m. %H:%M")


__all__ = [
    "TZ", "SLOTS_PER_WEEK", "local", "week_slot", "week_slot_of_dt", "neighbor_slots", "slot_label",
    "parse_at", "BadInput", "iso_local", "iso_utc", "fmt_hm", "fmt_dt", "local_midnight", "timedelta",
]

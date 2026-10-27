"""DST-safe local date and cycle boundaries."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo


def local_day_bounds(day: date, timezone: str) -> tuple[datetime, datetime]:
    """Return unambiguous UTC instants spanning one local calendar day."""
    zone = ZoneInfo(timezone)
    local_start = datetime.combine(day, time.min, zone)
    local_end = datetime.combine(day + timedelta(days=1), time.min, zone)
    return local_start.astimezone(UTC), local_end.astimezone(UTC)


def cycle_boundaries(day: date, timezone: str, cycle_hours: int = 6) -> list[datetime]:
    """Return UTC boundaries for wall-clock cycles on a local date."""
    if cycle_hours <= 0 or 24 % cycle_hours:
        raise ValueError("cycle_hours must divide 24")
    zone = ZoneInfo(timezone)
    boundaries = [
        datetime.combine(day, time(hour=hour), zone).astimezone(UTC)
        for hour in range(0, 24, cycle_hours)
    ]
    boundaries.append(datetime.combine(day + timedelta(days=1), time.min, zone).astimezone(UTC))
    return boundaries


def elapsed_seconds(start: datetime, end: datetime) -> float:
    """Return physical elapsed seconds, requiring aware datetimes."""
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return (end.astimezone(UTC) - start.astimezone(UTC)).total_seconds()

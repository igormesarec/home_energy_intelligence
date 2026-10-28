"""Local-calendar period presets, independent of browser timezone."""

from __future__ import annotations

import calendar
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from .timeutils import local_day_bounds


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def _season_start(today: date, month: int, day: int) -> date:
    candidate = date(today.year, month, day)
    return candidate if today >= candidate else date(today.year - 1, month, day)


def period_bounds(
    preset: str, today: date, *, season_month: int = 7, season_day: int = 1,
    older_month: str | None = None, custom_start: str | None = None,
    custom_end: str | None = None,
) -> tuple[date, date, str]:
    """Resolve every dashboard preset using Home Assistant's local date."""
    if preset == "today":
        return today, today, "Today"
    if preset == "yesterday":
        day = today - timedelta(days=1)
        return day, day, "Yesterday"
    if preset.startswith("day_"):
        offset = int(preset.removeprefix("day_"))
        if offset not in range(2, 8):
            raise ValueError("day offset must be between 2 and 7")
        day = today - timedelta(days=offset)
        return day, day, f"−{offset} days"
    monday = today - timedelta(days=today.weekday())
    if preset == "this_week":
        return monday, today, "This week"
    if preset == "previous_week":
        end = monday - timedelta(days=1)
        return end - timedelta(days=6), end, "Previous week"
    if preset == "last_7_completed":
        end = today - timedelta(days=1)
        return end - timedelta(days=6), end, "Last 7 completed days"
    if preset == "this_month":
        start, _ = _month_bounds(today.year, today.month)
        return start, today, "This month"
    if preset == "previous_month":
        end = date(today.year, today.month, 1) - timedelta(days=1)
        start, end = _month_bounds(end.year, end.month)
        return start, end, "Previous month"
    if preset == "older_month":
        if not older_month:
            raise ValueError("older_month is required")
        year, month = (int(value) for value in older_month.split("-", 1))
        start, end = _month_bounds(year, month)
        return start, end, start.strftime("%B %Y")
    if preset == "this_year":
        return date(today.year, 1, 1), today, "This calendar year"
    if preset == "previous_year":
        year = today.year - 1
        return date(year, 1, 1), date(year, 12, 31), "Previous calendar year"
    if preset in {"current_season", "previous_season"}:
        start = _season_start(today, season_month, season_day)
        if preset == "previous_season":
            start = date(start.year - 1, season_month, season_day)
        next_start = date(start.year + 1, season_month, season_day)
        end = min(today, next_start - timedelta(days=1)) if preset == "current_season" else next_start - timedelta(days=1)
        return start, end, f"Energy season {start.year}/{str(start.year + 1)[-2:]}"
    if preset == "custom":
        if not custom_start or not custom_end:
            raise ValueError("custom range requires start and end")
        start, end = date.fromisoformat(custom_start), date.fromisoformat(custom_end)
        if end < start:
            raise ValueError("custom range must be ordered")
        return start, end, f"{start.isoformat()}–{end.isoformat()}"
    raise ValueError(f"unsupported period preset: {preset}")


def previous_day_same_local_cutoff(
    reference_utc: datetime, timezone: str,
) -> tuple[datetime, datetime]:
    """Return previous local midnight through the same wall-clock cutoff."""
    zone = ZoneInfo(timezone)
    local = reference_utc.astimezone(zone)
    previous = local.date() - timedelta(days=1)
    start, day_end = local_day_bounds(previous, timezone)
    end = datetime(
        previous.year, previous.month, previous.day, local.hour,
        tzinfo=zone, fold=local.fold,
    ).astimezone(UTC)
    return start, min(day_end, max(start, end))

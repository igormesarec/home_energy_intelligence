"""Pure helpers for bounded historical temperature timelines."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .timeutils import local_day_bounds


def validate_timeline_dates(
    start_value: str, end_value: str, *, maximum_days: int,
) -> tuple[date, date]:
    """Validate an inclusive local-date timeline range."""
    try:
        start = date.fromisoformat(str(start_value))
        end = date.fromisoformat(str(end_value))
    except (TypeError, ValueError) as error:
        raise ValueError("start_date and end_date must use YYYY-MM-DD") from error
    if end < start or (end - start).days >= maximum_days:
        raise ValueError(
            f"timeline range must be ordered and at most {maximum_days} days"
        )
    return start, end


def timeline_quality(
    summary: dict | None, expected_seconds: float,
) -> tuple[str, str | None]:
    """Keep temporal coverage separate from filtering evidence."""
    if not isinstance(summary, dict) or summary.get("mean") is None:
        return "missing", "hourly_temperature_history_not_archived_or_backfilled"
    valid_seconds = float(summary.get("coverage_seconds", 0) or 0)
    rejections = summary.get("rejections") or []
    rejection_reasons = sorted({
        str(item.get("reason")) for item in rejections
        if isinstance(item, dict) and item.get("reason")
    })
    if valid_seconds + 1e-6 < expected_seconds:
        return "partial", "incomplete_validated_coverage"
    if summary.get("quality") == "filtered" or rejection_reasons:
        suffix = ",".join(rejection_reasons) if rejection_reasons else "filtered_samples"
        return "filtered", f"filtered_samples:{suffix}"
    return str(summary.get("quality") or "good"), None


def timeline_source_payload(
    summary: dict | None, expected_seconds: float, row_provenance: dict | None,
) -> dict:
    """Normalize one stored summary without creating a measurement."""
    quality, reason = timeline_quality(summary, expected_seconds)
    if not isinstance(summary, dict):
        return {
            "mean": None, "minimum": None, "maximum": None,
            "valid_seconds": 0.0, "expected_seconds": expected_seconds,
            "coverage_percent": 0.0, "quality": quality,
            "missing_reason": reason, "provenance": row_provenance or {},
            "rejected_count": 0, "rejected_reasons": [],
        }
    valid_seconds = max(0.0, min(
        expected_seconds, float(summary.get("coverage_seconds", 0) or 0)
    ))
    rejections = summary.get("rejections") or []
    rejection_reasons = sorted({
        str(item.get("reason")) for item in rejections
        if isinstance(item, dict) and item.get("reason")
    })
    return {
        "mean": summary.get("mean"), "minimum": summary.get("minimum"),
        "maximum": summary.get("maximum"), "latest_value": summary.get("latest_value"),
        "latest_timestamp": summary.get("latest_timestamp"),
        "valid_seconds": valid_seconds, "expected_seconds": expected_seconds,
        "coverage_percent": (
            min(100.0, 100.0 * valid_seconds / expected_seconds)
            if expected_seconds else 0.0
        ),
        "quality": quality, "missing_reason": reason,
        "provenance": summary.get("source_provenance") or row_provenance or {},
        "rejected_count": int(summary.get("rejected_count", len(rejections)) or 0),
        "rejected_reasons": rejection_reasons,
    }


def expected_timeline_intervals(
    resolution: str, start_date: date, end_date: date, timezone: str,
    range_end_utc: datetime,
) -> list[tuple[datetime, datetime, str]]:
    """Build elapsed UTC intervals while retaining HA-local calendar identity."""
    start_utc, _ = local_day_bounds(start_date, timezone)
    if resolution == "hourly":
        expected: list[tuple[datetime, datetime, str]] = []
        cursor = start_utc
        while cursor < range_end_utc:
            interval_end = min(cursor + timedelta(hours=1), range_end_utc)
            expected.append((cursor, interval_end, cursor.astimezone(
                ZoneInfo(timezone)
            ).date().isoformat()))
            cursor = interval_end
        return expected
    expected = []
    cursor_date = start_date
    while cursor_date <= end_date:
        interval_start, interval_end = local_day_bounds(cursor_date, timezone)
        if interval_start >= range_end_utc:
            break
        expected.append((interval_start, min(interval_end, range_end_utc),
                         cursor_date.isoformat()))
        cursor_date += timedelta(days=1)
    return expected


def build_timeline_intervals(
    *, resolution: str, start_date: date, end_date: date, timezone: str,
    range_end_utc: datetime, stored: list[dict], source_ids: list[str],
) -> tuple[list[dict], int]:
    """Merge persisted rows into a complete interval grid with explicit gaps."""
    by_start = {item["period_start_utc"]: item for item in stored}
    zone = ZoneInfo(timezone)
    intervals = []
    for interval_start, interval_end, local_date_value in expected_timeline_intervals(
        resolution, start_date, end_date, timezone, range_end_utc
    ):
        row = by_start.get(interval_start.isoformat())
        seconds = max(0.0, (interval_end - interval_start).total_seconds())
        temperatures = {
            source_id: timeline_source_payload(
                (row or {}).get("temperatures", {}).get(source_id), seconds,
                (row or {}).get("provenance"),
            )
            for source_id in source_ids
        }
        intervals.append({
            "start_utc": interval_start.isoformat(),
            "end_utc": interval_end.isoformat(),
            "local_date": local_date_value,
            "local_start": interval_start.astimezone(zone).isoformat(),
            "local_end": interval_end.astimezone(zone).isoformat(),
            "local_label": interval_start.astimezone(zone).strftime(
                "%Y-%m-%d %H:%M %z"
            ),
            "temperatures": temperatures,
        })
    available = sum(
        1 for item in intervals
        if any(value["mean"] is not None for value in item["temperatures"].values())
    )
    return intervals, available


def hourly_axis_ticks(start: datetime, end: datetime, timezone: str) -> list[dict]:
    """Return HA-local six-hour ticks, with an explicit actual range end."""
    zone = ZoneInfo(timezone)
    if end <= start:
        return []
    local_start = start.astimezone(zone)
    cursor = datetime.combine(local_start.date(), time.min, tzinfo=zone)
    ticks: list[dict] = []
    while cursor.astimezone(UTC) <= end:
        instant = cursor.astimezone(UTC)
        if start <= instant <= end:
            ticks.append({"timestamp_utc": instant.isoformat(),
                          "label": cursor.strftime("%H:%M")})
        cursor += timedelta(hours=6)
    end_local = end.astimezone(zone)
    end_label = (
        "24:00" if end_local.time() == time.min
        and end_local.date() > local_start.date()
        else end_local.strftime("%H:%M")
    )
    if ticks and ticks[-1]["timestamp_utc"] == end.isoformat():
        ticks[-1]["label"] = end_label
    if not ticks or ticks[-1]["timestamp_utc"] != end.isoformat():
        ticks.append({"timestamp_utc": end.isoformat(),
                      "label": end_label})
    return ticks

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from custom_components.home_energy_intelligence.analytics import combine_interval_records
from custom_components.home_energy_intelligence.temperature_timeline import (
    build_timeline_intervals, expected_timeline_intervals, hourly_axis_ticks,
    timeline_source_payload, validate_timeline_dates,
)
from custom_components.home_energy_intelligence.timeutils import local_day_bounds


ZONE = "Europe/Berlin"


def _summary(mean, minimum=None, maximum=None, *, quality="good", coverage=3600,
             rejections=None):
    return {
        "mean": mean, "minimum": mean if minimum is None else minimum,
        "maximum": mean if maximum is None else maximum,
        "coverage_seconds": coverage, "period_seconds": 3600,
        "coverage_percent": 100 * coverage / 3600,
        "quality": quality, "rejected_count": len(rejections or []),
        "rejections": rejections or [],
        "source_provenance": {"entity_id": "sensor.fixture", "role": "indoor"},
    }


def _hour_rows(day: date, source_values: dict[str, list[float]]) -> list[dict]:
    start, end = local_day_bounds(day, ZONE)
    rows = []
    cursor = start
    index = 0
    while cursor < end:
        interval_end = cursor + timedelta(hours=1)
        temperatures = {}
        for source_id, values in source_values.items():
            value = values[index]
            temperatures[source_id] = _summary(value)
        rows.append({
            "local_date": day.isoformat(), "timezone": ZONE,
            "period_start_utc": cursor.isoformat(),
            "period_end_utc": interval_end.isoformat(), "day_seconds": 3600,
            "household_kwh": None, "pv_kwh": None, "import_kwh": None,
            "export_kwh": None, "non_hvac_kwh": None, "allocations": {},
            "temperatures": temperatures, "temperature_only": True,
            "quality": "partial", "provenance": {"source": "test_fixture"},
        })
        cursor = interval_end
        index += 1
    return rows


def test_october_29_daily_values_and_multi_point_hourly_timeline_are_independent():
    day = date(2026, 10, 29)
    indoor = [22.8] * 12 + [23.2] * 12
    outdoor = [9.0, 17.0] + [11.3636363636364] * 22
    rows = _hour_rows(day, {"indoor": indoor, "outdoor": outdoor})
    rows[0]["temperatures"]["indoor"]["minimum"] = 22.5
    rows[-1]["temperatures"]["indoor"]["maximum"] = 23.2
    rows[0]["temperatures"]["outdoor"]["minimum"] = 9.0
    rows[1]["temperatures"]["outdoor"]["maximum"] = 17.0
    combined = combine_interval_records(rows)
    assert combined["temperatures"]["indoor"]["mean"] == pytest.approx(23.0)
    assert combined["temperatures"]["indoor"]["minimum"] == 22.5
    assert combined["temperatures"]["indoor"]["maximum"] == 23.2
    assert combined["temperatures"]["outdoor"]["mean"] == pytest.approx(11.5)
    assert combined["temperatures"]["outdoor"]["minimum"] == 9.0
    assert combined["temperatures"]["outdoor"]["maximum"] == 17.0
    start, end = local_day_bounds(day, ZONE)
    intervals, available = build_timeline_intervals(
        resolution="hourly", start_date=day, end_date=day, timezone=ZONE,
        range_end_utc=end, stored=rows, source_ids=["indoor", "outdoor"],
    )
    assert len(intervals) == available == 24
    assert len({item["temperatures"]["indoor"]["mean"] for item in intervals}) == 2
    assert hourly_axis_ticks(start, end, ZONE)[-1]["label"] == "24:00"


@pytest.mark.parametrize("day,expected", [
    (date(2026, 3, 29), 23), (date(2026, 10, 25), 25),
])
def test_hourly_timeline_uses_elapsed_dst_intervals(day, expected):
    start, end = local_day_bounds(day, ZONE)
    intervals = expected_timeline_intervals("hourly", day, day, ZONE, end)
    assert len(intervals) == expected
    labels = [item[0].astimezone(__import__("zoneinfo").ZoneInfo(ZONE)).strftime(
        "%H:%M %z"
    ) for item in intervals]
    if expected == 25:
        assert "02:00 +0200" in labels and "02:00 +0100" in labels


def test_missing_hours_are_explicit_gaps_and_never_forward_filled():
    day = date(2026, 10, 29)
    rows = _hour_rows(day, {"indoor": [23.0] * 24})
    del rows[8:10]
    _, end = local_day_bounds(day, ZONE)
    intervals, available = build_timeline_intervals(
        resolution="hourly", start_date=day, end_date=day, timezone=ZONE,
        range_end_utc=end, stored=rows, source_ids=["indoor"],
    )
    assert len(intervals) == 24 and available == 22
    missing = [item for item in intervals if item["temperatures"]["indoor"]["mean"] is None]
    assert len(missing) == 2
    assert {item["temperatures"]["indoor"]["missing_reason"] for item in missing} == {
        "hourly_temperature_history_not_archived_or_backfilled"
    }


def test_one_hour_is_one_truthful_point_not_a_complete_curve():
    day = date(2026, 10, 29)
    rows = _hour_rows(day, {"outdoor": [11.0] * 24})[:1]
    _, end = local_day_bounds(day, ZONE)
    intervals, available = build_timeline_intervals(
        resolution="hourly", start_date=day, end_date=day, timezone=ZONE,
        range_end_utc=end, stored=rows, source_ids=["outdoor"],
    )
    assert available == 1
    assert sum(item["temperatures"]["outdoor"]["mean"] is not None
               for item in intervals) == 1
    assert sum(item["temperatures"]["outdoor"]["valid_seconds"]
               for item in intervals) == 3600


def test_filtered_full_coverage_keeps_rejection_reason_separate_from_coverage():
    rejection = {"value": -55, "reason": "invalid_sentinel", "source": "sensor.indoor"}
    payload = timeline_source_payload(
        _summary(23, quality="filtered", rejections=[rejection]), 3600, None
    )
    assert payload["coverage_percent"] == 100
    assert payload["quality"] == "filtered"
    assert payload["missing_reason"] == "filtered_samples:invalid_sentinel"
    assert payload["rejected_reasons"] == ["invalid_sentinel"]


def test_daily_resolution_is_bounded_and_does_not_expand_to_hours():
    start, end = date(2026, 10, 1), date(2026, 10, 31)
    _, range_end = local_day_bounds(end, ZONE)
    intervals = expected_timeline_intervals("daily", start, end, ZONE, range_end)
    assert len(intervals) == 31
    with pytest.raises(ValueError, match="at most 7 days"):
        validate_timeline_dates(start.isoformat(), (start + timedelta(days=7)).isoformat(),
                                maximum_days=7)

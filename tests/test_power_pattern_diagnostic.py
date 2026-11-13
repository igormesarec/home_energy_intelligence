from datetime import UTC, date, datetime, timedelta

import pytest

from custom_components.home_energy_intelligence.engine import DeviceHistory
from custom_components.home_energy_intelligence.models import NumericSample, StateSample
from custom_components.home_energy_intelligence.power_pattern_diagnostic import analyze_power, power_segments, power_statistics
from custom_components.home_energy_intelligence.timeutils import local_day_bounds


def test_irregular_power_uses_duration_not_sample_count():
    t = datetime(2026, 10, 22, tzinfo=UTC)
    samples = [NumericSample(t, 100), NumericSample(t+timedelta(minutes=10), 1000),
               NumericSample(t+timedelta(minutes=11), 1000)]
    segments, gaps = power_segments(samples, t, t+timedelta(minutes=11))
    stats = power_statistics(segments, 660, gaps)
    assert stats["mean_w"] == pytest.approx((100*10+1000)/11)
    assert stats["median_w"] == 100 and stats["p90_w"] == 100
    assert stats["coverage_percent"] == 100


def test_gaps_and_invalid_observations_are_not_filled():
    t = datetime(2026, 10, 22, tzinfo=UTC)
    samples = [NumericSample(t, 10), NumericSample(t+timedelta(minutes=20), 100),
               NumericSample(t+timedelta(minutes=21), -10)]
    segments, gaps = power_segments(samples, t, t+timedelta(minutes=21))
    assert segments == []
    assert power_statistics(segments, 1260, gaps)["coverage_percent"] == 0


@pytest.mark.parametrize("day,hours", [(date(2026, 3, 29), 23), (date(2026, 10, 25), 25)])
def test_unknown_partition_and_dst(day, hours):
    start, end = local_day_bounds(day, "Europe/Ljubljana")
    assert (end-start).total_seconds() == hours*3600
    trace = [{"status": "accepted", "category": "unknown", "start": start.isoformat(),
              "end": (start+timedelta(minutes=10)).isoformat(), "delta_kwh": 1}]
    history = DeviceHistory(kind="heat_pump", power=[NumericSample(start, 300), NumericSample(start+timedelta(minutes=10), 350)])
    streams = {"primary": {"samples": []}}
    result = analyze_power(trace, history, streams, {}, timedelta(hours=1), {})
    assert result["candidate_totals"]["ambiguous_kwh"] == 1
    assert sum(result["candidate_totals"].values()) == 1
    assert result["validated_recovery_kwh"] == 0
    assert result["candidates"][0]["validation_status"] == "NOT VALIDATED"


def test_unverified_idle_reference_and_conflict_remain_diagnostic():
    start = datetime(2026, 10, 22, tzinfo=UTC)
    end = start+timedelta(minutes=10)
    trace = [{"status": "accepted", "category": "idle", "start": start.isoformat(),
              "end": end.isoformat(), "delta_kwh": 0.1, "state": {"source": "primary"}, "mode_transition": False},
             {"status": "accepted", "category": "unknown", "start": end.isoformat(),
              "end": (end+timedelta(minutes=10)).isoformat(), "delta_kwh": 1}]
    history = DeviceHistory(kind="heat_pump", power=[NumericSample(start, 10), NumericSample(end, 10), NumericSample(end+timedelta(minutes=10), 20)])
    streams = {"primary": {"samples": [StateSample(start, "idle")]}}
    continuity = {"candidates": [{"start_utc": end.isoformat(), "end_utc": (end+timedelta(minutes=10)).isoformat(), "possible_fallback_conflict": True}]}
    result = analyze_power(trace, history, streams, {"idle": "idle"}, timedelta(hours=1), continuity)
    assert result["references"]["idle"]["interval_count"] == 1
    assert result["references"]["idle"]["statistics"]["mean_w"] == 10
    assert result["candidates"][0]["potential_conflict"]
    assert result["candidate_totals"]["idle_compatible_kwh"] == 0


def test_conflicting_duplicate_power_observations_break_coverage():
    t = datetime(2026, 10, 22, tzinfo=UTC)
    samples = [NumericSample(t, 100), NumericSample(t+timedelta(minutes=5), 100),
               NumericSample(t+timedelta(minutes=5), 900), NumericSample(t+timedelta(minutes=10), 100)]
    segments, gaps = power_segments(samples, t, t+timedelta(minutes=10))
    assert segments == []
    assert power_statistics(segments, 600, gaps)["coverage_percent"] == 0

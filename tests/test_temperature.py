from datetime import UTC, datetime, timedelta

import pytest

from custom_components.home_energy_intelligence.models import NumericSample
from custom_components.home_energy_intelligence.temperature import time_weighted_temperature
from custom_components.home_energy_intelligence.timeutils import local_day_bounds
from datetime import date


def dt(hour=0, minute=0):
    return datetime(2026, 1, 1, hour, minute, tzinfo=UTC)


def test_true_time_weighted_mean_not_observation_average():
    result = time_weighted_temperature(
        [NumericSample(dt(), 5), NumericSample(dt(1), 6)],
        dt(), dt(1, 5), minimum_valid=-60, maximum_valid=70,
    )
    assert result.mean == pytest.approx(5.0769230769)
    assert result.minimum == 5
    assert result.maximum == 6
    assert result.coverage_percent == 100


def test_preceding_reading_is_used_and_155_spike_is_rejected():
    result = time_weighted_temperature(
        [
            NumericSample(dt() - timedelta(hours=1), 4),
            NumericSample(dt(0, 30), 155),
            NumericSample(dt(0, 45), 6),
        ],
        dt(), dt(1), minimum_valid=-60, maximum_valid=70,
    )
    assert result.mean == pytest.approx((4 * 1800 + 6 * 900) / 2700)
    assert result.coverage_seconds == 2700
    assert result.rejected_count == 1
    assert result.quality == "partial"


def test_unavailable_marker_stops_imputation_and_reports_gap():
    result = time_weighted_temperature(
        [NumericSample(dt(), 20), NumericSample(dt(0, 20), None, "unavailable"),
         NumericSample(dt(0, 50), 21)],
        dt(), dt(1), minimum_valid=-10, maximum_valid=60,
    )
    assert result.coverage_seconds == 30 * 60
    assert result.mean == pytest.approx((20 * 20 + 21 * 10) / 30)
    assert result.quality == "partial"


def test_freshness_limit_creates_missing_coverage_without_new_evidence():
    result = time_weighted_temperature(
        [NumericSample(dt(), 20, "point_sample")], dt(), dt(2), minimum_valid=-10, maximum_valid=60,
        max_carry=timedelta(minutes=30),
    )
    assert result.coverage_seconds == 1800
    assert result.coverage_percent == 25


def test_unchanged_recorder_state_is_not_stale_solely_from_last_change_time():
    result = time_weighted_temperature(
        [NumericSample(dt() - timedelta(days=2), 19.5)], dt(), dt(2),
        minimum_valid=-10, maximum_valid=60, max_carry=timedelta(minutes=30),
    )
    assert result.coverage_percent == 100
    assert result.mean == 19.5


def test_long_term_statistic_bounds_do_not_distort_weighted_mean():
    result = time_weighted_temperature(
        [NumericSample(dt(), 5.5, "statistics_bounds:4:7"),
         NumericSample(dt(1), None, "statistics_interval_end")],
        dt(), dt(1), minimum_valid=-60, maximum_valid=70,
    )
    assert result.mean == 5.5
    assert result.minimum == 4
    assert result.maximum == 7


@pytest.mark.parametrize("contaminated", [-55, 155])
def test_contaminated_long_term_statistics_have_no_measured_coverage(contaminated):
    result = time_weighted_temperature(
        [NumericSample(dt(), 5.5, f"statistics_bounds:4:{contaminated}"),
         NumericSample(dt(1), None, "statistics_interval_end")],
        dt(), dt(1), minimum_valid=-50, maximum_valid=60,
    )
    assert result.mean is None
    assert result.coverage_seconds == 0
    assert result.rejections[0].reason == "contaminated_statistics_bounds"


@pytest.mark.parametrize("bad", [-55.0, 155.0])
def test_known_invalid_sentinels_have_zero_measured_coverage_and_audit_detail(bad):
    result = time_weighted_temperature(
        [NumericSample(dt(), bad, source="sensor.outdoor")], dt(), dt(1),
        minimum_valid=-60, maximum_valid=200, source="outdoor",
    )
    assert result.coverage_seconds == 0
    assert result.rejections[0].value == bad
    assert result.rejections[0].duration_seconds == 3600
    assert result.rejections[0].reason == "invalid_sentinel"
    assert result.rejections[0].source == "sensor.outdoor"


def test_unchanged_winter_reading_is_valid_but_temporal_spike_is_not():
    result = time_weighted_temperature(
        [NumericSample(dt(), -12), NumericSample(dt(0, 20), -12),
         NumericSample(dt(0, 30), 30), NumericSample(dt(0, 40), -11)],
        dt(), dt(1), minimum_valid=-50, maximum_valid=60,
        maximum_change_per_minute=2,
    )
    assert result.minimum == -12
    assert result.coverage_seconds == 50 * 60
    assert any(item.reason == "temporal_spike" for item in result.rejections)


def test_outage_resets_temporal_comparison_and_never_counts_missing_interval():
    result = time_weighted_temperature(
        [NumericSample(dt(), 10), NumericSample(dt(0, 15), None, "unavailable"),
         NumericSample(dt(0, 45), -5)], dt(), dt(1), minimum_valid=-50, maximum_valid=60,
        maximum_change_per_minute=0.1,
    )
    assert result.coverage_seconds == 30 * 60
    assert result.mean == pytest.approx((10 * 15 + -5 * 15) / 30)


@pytest.mark.parametrize(("day", "hours"), [(date(2026, 3, 29), 23), (date(2026, 10, 25), 25)])
def test_temperature_weighting_uses_elapsed_time_on_dst_days(day, hours):
    start, end = local_day_bounds(day, "Europe/Ljubljana")
    result = time_weighted_temperature(
        [NumericSample(start, -2)], start, end, minimum_valid=-50, maximum_valid=60,
    )
    assert result.period_seconds == hours * 3600
    assert result.coverage_seconds == hours * 3600
    assert result.mean == -2

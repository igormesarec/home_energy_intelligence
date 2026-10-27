from datetime import UTC, datetime, timedelta

import pytest

from custom_components.home_energy_intelligence.models import NumericSample
from custom_components.home_energy_intelligence.temperature import time_weighted_temperature


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
        [NumericSample(dt(), 20)], dt(), dt(2), minimum_valid=-10, maximum_valid=60,
        max_carry=timedelta(minutes=30),
    )
    assert result.coverage_seconds == 1800
    assert result.coverage_percent == 25


def test_long_term_statistic_bounds_do_not_distort_weighted_mean():
    result = time_weighted_temperature(
        [NumericSample(dt(), 5.5, "statistics_bounds:4:7"),
         NumericSample(dt(1), None, "statistics_interval_end")],
        dt(), dt(1), minimum_valid=-60, maximum_valid=70,
    )
    assert result.mean == 5.5
    assert result.minimum == 4
    assert result.maximum == 7

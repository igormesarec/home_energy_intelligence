from datetime import UTC, date, datetime, timedelta

import pytest

from custom_components.home_energy_intelligence.allocation import allocate_historical_counter_increments
from custom_components.home_energy_intelligence.models import NumericSample, StateSample
from custom_components.home_energy_intelligence.timeutils import local_day_bounds


def preview(values, modes):
    start = datetime(2026, 9, 25, tzinfo=UTC)
    return allocate_historical_counter_increments(
        start=start, end=start + timedelta(days=1),
        counter_samples=[NumericSample(start + timedelta(minutes=t), value) for t, value in values],
        mode_samples=[StateSample(start + timedelta(minutes=t), mode) for t, mode in modes],
        classifier=lambda sample: sample.state,
    )


def test_unaligned_boundary_uses_only_supported_increments_without_power():
    result = preview([(5, 100), (35, 101), (65, 102)], [(0, "confirmed_heating")])
    assert result.meter_kwh == pytest.approx(2)
    assert result.allocations_kwh == {"confirmed_heating": pytest.approx(2)}
    assert result.covered_seconds == 3600
    assert result.quality == "partial"


def test_mixed_mode_increment_is_unknown_without_interpolation():
    result = preview([(0, 100), (60, 102)], [(0, "confirmed_heating"), (30, "dhw")])
    assert result.allocations_kwh == {"unknown": pytest.approx(2)}
    assert result.covered_seconds == 0


def test_false_reset_recovery_and_recording_gap_are_not_invented_energy():
    result = preview([(0, 100), (20, 1), (40, 101), (60, 102), (180, 110)], [(0, "idle")])
    assert result.meter_kwh == pytest.approx(1)
    assert result.rejected_count >= 3


def test_genuine_persistent_reset_preserves_later_observed_increment():
    result = preview([(0, 100), (30, 0), (60, 1)], [(0, "idle")])
    assert result.meter_kwh == pytest.approx(1)
    assert result.reset_count == 1


def test_unavailable_counter_state_breaks_observation_chain():
    result = preview([(0, 100), (20, None), (40, 102), (60, 103)], [(0, "idle")])
    assert result.meter_kwh == pytest.approx(1)
    assert result.rejected_count == 2


@pytest.mark.parametrize(("day", "hours"), [(date(2026, 3, 29), 23), (date(2026, 10, 25), 25)])
def test_dst_day_uses_elapsed_utc_hours(day, hours):
    start, end = local_day_bounds(day, "Europe/Ljubljana")
    result = allocate_historical_counter_increments(
        start=start, end=end,
        counter_samples=[NumericSample(start + timedelta(hours=i), float(i)) for i in range(hours + 1)],
        mode_samples=[StateSample(start, "idle")], classifier=lambda sample: sample.state)
    assert result.meter_kwh == hours
    assert result.period_seconds == hours * 3600

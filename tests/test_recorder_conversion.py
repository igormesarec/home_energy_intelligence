from datetime import UTC, datetime
from types import SimpleNamespace

from custom_components.home_energy_intelligence.recorder import (
    statistics_to_counter_samples,
    statistics_to_numeric_samples,
    states_to_numeric,
)


def test_long_term_energy_changes_become_boundary_counter_samples():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    rows = [
        {"start": start.timestamp(), "end": start.timestamp() + 3600, "change": 1.25},
        {"start": start.timestamp() + 3600, "end": start.timestamp() + 7200, "change": 0.75},
    ]
    samples = statistics_to_counter_samples(rows, start)
    assert [sample.value for sample in samples] == [0, 1.25, 2.0]
    assert samples[-1].timestamp.timestamp() == start.timestamp() + 7200


def test_long_term_temperature_means_are_retained():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    rows = [{"start": start.timestamp(), "end": start.timestamp() + 3600,
             "mean": 5.5, "min": 4.0, "max": 7.0}]
    samples = statistics_to_numeric_samples(rows)
    assert samples[0].value == 5.5
    assert samples[0].timestamp == start
    assert samples[1].value is None


def test_measured_temperature_attribute_not_setpoint_state_is_used():
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    row = SimpleNamespace(
        state="55", last_updated=stamp,
        attributes={"current_temperature": 48.5, "temperature": 55},
    )
    samples = states_to_numeric([row], attribute="current_temperature")
    assert samples[0].value == 48.5

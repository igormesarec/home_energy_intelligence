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
        attributes={"current_temperature": 48.5, "temperature": 55,
                    "unit_of_measurement": "°C"},
    )
    samples = states_to_numeric([row], attribute="current_temperature", dimension="temperature")
    assert samples[0].value == 48.5


def test_energy_power_and_temperature_units_are_normalized_with_provenance():
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    def row(value, unit):
        return SimpleNamespace(state=str(value), last_updated=stamp,
                               attributes={"unit_of_measurement": unit})

    assert states_to_numeric([row(1500, "Wh")], dimension="energy")[0].value == 1.5
    assert states_to_numeric([row(1.5, "MWh")], dimension="energy")[0].value == 1500
    assert states_to_numeric([row(1.2, "kW")], dimension="power")[0].value == 1200
    fahrenheit = states_to_numeric([row(14, "°F")], dimension="temperature")[0]
    assert fahrenheit.value == -10
    assert fahrenheit.source_unit == "°F" and fahrenheit.normalized_unit == "°C"


def test_incompatible_unit_is_unavailable_not_a_false_zero():
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    row = SimpleNamespace(state="12", last_updated=stamp,
                          attributes={"unit_of_measurement": "m³"})
    sample = states_to_numeric([row], dimension="energy")[0]
    assert sample.value is None
    assert sample.reason == "incompatible_energy_unit:m³"

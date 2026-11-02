from datetime import UTC, date, datetime, timedelta

import pytest

from custom_components.home_energy_intelligence.historical_modes import historical_heat_pump_modes, historical_selected_modes
from custom_components.home_energy_intelligence.models import StateSample
from custom_components.home_energy_intelligence.timeutils import local_day_bounds


def test_primary_dhw_overrides_selected_heat_and_stale_inputs_become_unknown():
    start = datetime(2026, 10, 9, tzinfo=UTC)
    result = historical_heat_pump_modes(
        start, start + timedelta(hours=3),
        primary=[StateSample(start + timedelta(minutes=30), "heating_water")],
        actual_dhw=[], fallback=[StateSample(start, "heat")],
        state_map={"heating_water": "dhw", "heating_zones": "confirmed_heating"},
    )
    assert [(item.timestamp-start, item.state, item.attributes["source"]) for item in result] == [
        (timedelta(0), "heating_assumed", "selected_mode"),
        (timedelta(minutes=30), "dhw", "primary"),
        (timedelta(minutes=90), "unknown", "unavailable"),
    ]


@pytest.mark.parametrize("day,hours", [(date(2026, 3, 29), 23), (date(2026, 10, 25), 25)])
def test_fallback_freshness_uses_elapsed_time_on_dst_days(day, hours):
    start, end = local_day_bounds(day, "Europe/Ljubljana")
    assert (end-start).total_seconds() == hours*3600
    result = historical_heat_pump_modes(start, end, primary=[], actual_dhw=[],
                                       fallback=[StateSample(start, "heat")], state_map={})
    assert [(item.timestamp, item.state) for item in result] == [
        (start, "heating_assumed"), (start+timedelta(hours=1), "unknown")]


def test_unknown_primary_without_fallback_never_becomes_heating():
    start = datetime(2026, 9, 25, tzinfo=UTC)
    result = historical_heat_pump_modes(start, start+timedelta(hours=2),
        primary=[StateSample(start, "unexpected")], actual_dhw=[], fallback=[], state_map={})
    assert [item.state for item in result] == ["unknown"]


def test_fresh_primary_idle_is_authoritative_over_fallback_and_dhw():
    start = datetime(2026, 10, 9, tzinfo=UTC)
    result = historical_heat_pump_modes(start, start+timedelta(minutes=30),
        primary=[StateSample(start, "idle")], actual_dhw=[StateSample(start, "heating_water")],
        fallback=[StateSample(start, "heat")], state_map={"idle": "idle"})
    assert [(item.state, item.attributes["source"]) for item in result] == [("idle", "primary")]


def test_ac_modes_preserve_selected_transitions_and_explicit_gaps():
    start = datetime(2026, 9, 26, tzinfo=UTC)
    result = historical_selected_modes(start, start+timedelta(hours=4), [
        StateSample(start, "heat"), StateSample(start+timedelta(minutes=30), "cool"),
        StateSample(start+timedelta(hours=1), "off"),
        StateSample(start+timedelta(hours=3), "unavailable"),
    ])
    assert [(item.timestamp-start, item.state) for item in result] == [
        (timedelta(), "heating_selected"), (timedelta(minutes=30), "cooling_selected"),
        (timedelta(hours=1), "standby"), (timedelta(hours=2), "unknown"),
    ]

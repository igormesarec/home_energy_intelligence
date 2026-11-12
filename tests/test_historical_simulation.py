from datetime import UTC, date, datetime, timedelta

import pytest

from custom_components.home_energy_intelligence.engine import DeviceHistory
from custom_components.home_energy_intelligence.historical_simulation import simulate_modes
from custom_components.home_energy_intelligence.models import NumericSample, StateSample
from custom_components.home_energy_intelligence.timeutils import local_day_bounds


@pytest.mark.parametrize("day,hours", [(date(2026, 3, 29), 23), (date(2026, 10, 25), 25)])
def test_scenarios_are_isolated_and_conserve_meter_on_dst(day, hours):
    start, end = local_day_bounds(day, "Europe/Ljubljana")
    history = DeviceHistory(kind="heat_pump", counter=[NumericSample(start+timedelta(hours=i), i) for i in range(4)])
    streams = {"primary": {"samples": []}, "actual_dhw": {"samples": []},
        "fallback": {"samples": [StateSample(start, "heat", {"hvac_action": "heating"})]}}
    reports = simulate_modes(start, end, history, streams, {}, 500, timedelta(hours=1))
    assert reports["baseline"]["unknown_kwh"] == 2
    assert reports["fallback_120m"]["unknown_kwh"] == 1
    assert reports["fallback_180m"]["unknown_kwh"] == 0
    assert reports["recorder_hvac_action"]["heating_assumed_kwh"] == 1
    assert all(r["meter_kwh"] == 3 and r["reconciles"] and r["period_seconds"] == hours*3600 for r in reports.values())
    assert all(r["confirmed_heating_kwh"] == 0 for r in reports.values())


def test_primary_precedence_and_idle_action_never_promotes_heat():
    start = datetime(2026, 10, 9, tzinfo=UTC)
    history = DeviceHistory(kind="heat_pump", counter=[NumericSample(start, 0), NumericSample(start+timedelta(hours=1), 1), NumericSample(start+timedelta(hours=2), 2)])
    streams = {"primary": {"samples": [StateSample(start, "dhw")]}, "actual_dhw": {"samples": []},
               "fallback": {"samples": [StateSample(start, "heat", {"hvac_action": "idle"})]}}
    reports = simulate_modes(start, start+timedelta(hours=2), history, streams, {"dhw": "dhw"}, 500, timedelta(hours=1))
    assert reports["fallback_120m"]["dhw_kwh"] == 1
    assert reports["fallback_120m"]["heating_assumed_kwh"] == 1
    assert reports["recorder_hvac_action"]["dhw_kwh"] == 1
    assert reports["recorder_hvac_action"]["unknown_kwh"] == 1


def test_mixed_mode_without_power_remains_unknown():
    start = datetime(2026, 10, 9, tzinfo=UTC)
    history = DeviceHistory(kind="heat_pump", counter=[NumericSample(start, 0), NumericSample(start+timedelta(hours=1), 1)])
    streams = {"primary": {"samples": [StateSample(start, "dhw"), StateSample(start+timedelta(minutes=30), "heat")]},
        "actual_dhw": {"samples": []}, "fallback": {"samples": []}}
    result = simulate_modes(start, start+timedelta(hours=1), history, streams, {"dhw": "dhw", "heat": "confirmed_heating"}, 500, timedelta(hours=1))
    assert all(r["unknown_kwh"] == 1 and r["unsupported_transition_kwh"] == 1 for r in result.values())

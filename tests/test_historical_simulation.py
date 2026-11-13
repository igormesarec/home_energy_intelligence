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


def test_nonstandard_freshness_preserves_primary_priority_in_every_scenario():
    start = datetime(2026, 10, 9, tzinfo=UTC)
    history = DeviceHistory(kind="heat_pump", counter=[
        NumericSample(start + timedelta(minutes=60), 10),
        NumericSample(start + timedelta(minutes=80), 11)])
    streams = {"primary": {"samples": [StateSample(start, "dhw")]},
               "actual_dhw": {"samples": []},
               "fallback": {"samples": [StateSample(start, "heat", {"hvac_action": "heating"})]}}
    reports = simulate_modes(start, start + timedelta(minutes=90), history, streams,
                             {"dhw": "dhw"}, 500, timedelta(minutes=90))
    assert all(report["dhw_kwh"] == 1 and report["heating_assumed_kwh"] == 0
               and report["primary_freshness_seconds"] == 5400 for report in reports.values())
    assert reports["recorder_hvac_action"]["fallback_freshness_seconds"] == 5400
    assert reports["fallback_120m"]["fallback_freshness_seconds"] == 7200
    assert reports["fallback_180m"]["fallback_freshness_seconds"] == 10800


@pytest.mark.parametrize("next_state,action,expected", [
    ("heat", "heating", "plausible_unverified"),
    ("dhw", "heating", "conflicting_or_insufficient"),
    ("idle", "heating", "conflicting_or_insufficient"),
    ("unavailable", "heating", "conflicting_or_insufficient"),
    ("heat", "idle", "conflicting_or_insufficient"),
])
def test_unknown_continuity_stays_unverified(next_state, action, expected):
    from custom_components.home_energy_intelligence.historical_simulation import continuity_candidates
    start = datetime(2026, 10, 25, tzinfo=UTC)
    trace = [{"start": start.isoformat(), "end": (start+timedelta(minutes=30)).isoformat(),
              "status": "accepted", "category": "unknown", "reason": "unknown_mode", "delta_kwh": 1.25}]
    streams = {"primary": {"samples": [StateSample(start-timedelta(hours=2), "heat"),
               StateSample(start+timedelta(hours=1), next_state)]},
               "actual_dhw": {"samples": []},
               "fallback": {"samples": [StateSample(start, "heat", {"hvac_action": action})]}}
    result = continuity_candidates(trace, DeviceHistory(kind="heat_pump"), streams,
                                   {"heat": "confirmed_heating", "dhw": "dhw"}, timedelta(hours=1))
    candidate = result["candidates"][0]
    assert candidate["confidence"] == expected
    assert candidate["power_coverage"] == "unavailable"
    assert candidate["evidence_type"] == "inferred"
    assert result["totals"]["supported_kwh"] == 0
    assert result["totals"][expected+"_kwh"] == pytest.approx(1.25)


def test_continuity_reports_missing_observations_and_unknown_meter():
    from custom_components.home_energy_intelligence.historical_simulation import continuity_candidates
    streams = {role: {"samples": []} for role in ("primary", "fallback", "actual_dhw")}
    result = continuity_candidates([], DeviceHistory(kind="heat_pump"), streams, {}, timedelta(hours=1))
    assert result["totals"]["supported_kwh"] is None
    assert result["candidates"] == []


def test_primary_interruption_cannot_be_bridged_even_if_endpoints_match():
    from custom_components.home_energy_intelligence.historical_simulation import continuity_candidates
    start = datetime(2026, 10, 22, tzinfo=UTC)
    trace = [{"start": start.isoformat(), "end": (start+timedelta(hours=1)).isoformat(),
              "status": "accepted", "category": "unknown", "reason": "unknown_mode", "delta_kwh": 1}]
    streams = {"primary": {"samples": [StateSample(start-timedelta(hours=2), "heat"),
                StateSample(start+timedelta(minutes=20), "dhw"), StateSample(start+timedelta(hours=1), "heat")]},
               "fallback": {"samples": []}, "actual_dhw": {"samples": []}}
    result = continuity_candidates(trace, DeviceHistory(kind="heat_pump"), streams,
                                   {"heat": "confirmed_heating"}, timedelta(hours=1))
    assert result["candidates"][0]["primary_interruption"]
    assert result["totals"]["conflicting_or_insufficient_kwh"] == 1


@pytest.mark.parametrize("primary,action,age,conflict", [
    ("heat", "idle", 0, True),
    ("idle", "heating", 0, True),
    ("dhw", "heating", 0, True),
    ("heat", "idle", 60, False),
    ("idle", "heating", 61, False),
    ("heat", "heating", 0, False),
    ("idle", "idle", 0, False),
])
def test_continuity_fresh_fallback_action_disagreements(primary, action, age, conflict):
    from custom_components.home_energy_intelligence.historical_simulation import continuity_candidates
    start = datetime(2026, 10, 22, tzinfo=UTC)
    trace = [{"start": start.isoformat(), "end": (start + timedelta(minutes=30)).isoformat(),
              "status": "accepted", "category": "unknown", "reason": "unknown_mode", "delta_kwh": 1}]
    streams = {"primary": {"samples": [StateSample(start-timedelta(hours=2), primary),
                StateSample(start+timedelta(hours=1), primary)]},
               "fallback": {"samples": [StateSample(start-timedelta(minutes=age), "heat", {"hvac_action": action})]},
               "actual_dhw": {"samples": []}}
    result = continuity_candidates(trace, DeviceHistory(kind="heat_pump"), streams,
        {"heat": "confirmed_heating", "idle": "idle", "dhw": "dhw"}, timedelta(hours=1))
    candidate = result["candidates"][0]
    assert candidate["possible_fallback_conflict"] is conflict
    assert candidate["confidence"] == ("conflicting_or_insufficient" if conflict else "plausible_unverified")
    assert result["totals"]["supported_kwh"] == 0
    if conflict and primary == "dhw":
        assert "may differ during DHW" in candidate["fallback_disagreements"][0]["interpretation"]

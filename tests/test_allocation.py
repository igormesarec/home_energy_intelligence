from datetime import UTC, datetime, timedelta

import pytest

from custom_components.home_energy_intelligence.allocation import ac_category, allocate_interval, configurable_classifier, counter_delta, ecodan_category
from custom_components.home_energy_intelligence.models import NumericSample, StateSample
from custom_components.home_energy_intelligence.allocation import allocate_historical_counter_increments
from custom_components.home_energy_intelligence.timeutils import local_day_bounds
from datetime import date


START = datetime(2026, 10, 25, tzinfo=UTC)
END = START + timedelta(hours=6)


@pytest.mark.parametrize('day,hours', [(date(2026, 3, 29), 23), (date(2026, 10, 25), 25)])
def test_historical_trace_preserves_whole_range_allocation_and_dst(day, hours):
    start, end = local_day_bounds(day, 'Europe/Ljubljana')
    counters = [NumericSample(start + timedelta(hours=i), float(i)) for i in range(hours+1)]
    modes = [StateSample(start, 'heating'), StateSample(start + timedelta(hours=2, minutes=30), 'dhw')]
    kwargs = dict(start=start, end=end, counter_samples=counters, mode_samples=modes,
                  classifier=lambda sample: sample.state)
    original = allocate_historical_counter_increments(**kwargs)
    trace = []
    traced = allocate_historical_counter_increments(**kwargs, trace=trace)
    assert traced == original and traced.period_seconds == hours * 3600
    assert sum(item['delta_kwh'] for item in trace if item['status'] == 'accepted') == pytest.approx(traced.meter_kwh)
    assert sum(traced.allocations_kwh.values()) == pytest.approx(traced.meter_kwh)
    assert any(item.get('reason') == 'unsupported_transition' and item.get('category') == 'unknown' for item in trace)


def test_historical_trace_preserves_reset_and_gap_rejections():
    start = START
    counters = [NumericSample(start, 100), NumericSample(start+timedelta(minutes=10), 0),
                NumericSample(start+timedelta(minutes=20), 100.1), NumericSample(start+timedelta(hours=3), 101)]
    kwargs = dict(start=start, end=start+timedelta(hours=4), counter_samples=counters,
                  mode_samples=[], classifier=lambda s:s.state)
    trace = []
    assert allocate_historical_counter_increments(**kwargs, trace=trace) == allocate_historical_counter_increments(**kwargs)
    assert [item['reason'] for item in trace] == ['transient_reset_sequence', 'transient_reset_sequence', 'counter_gap_or_nonpositive_duration']


def test_historical_trace_power_supported_mixed_mode_reconciles():
    start = START
    kwargs = dict(start=start, end=start+timedelta(hours=1),
                  counter_samples=[NumericSample(start, 10), NumericSample(start+timedelta(hours=1), 11)],
                  mode_samples=[StateSample(start, 'heating'), StateSample(start+timedelta(minutes=30), 'dhw')],
                  power_samples=[NumericSample(start, 1000), NumericSample(start+timedelta(minutes=30), 1000), NumericSample(start+timedelta(hours=1), 1000)],
                  classifier=lambda sample: sample.state)
    trace = []
    result = allocate_historical_counter_increments(**kwargs, trace=trace)
    assert result == allocate_historical_counter_increments(**kwargs)
    assert trace[0]['reason'] == 'power_supported_transition' and trace[0]['power_supported']
    assert trace[0]['split_allocations_kwh'] == pytest.approx(result.allocations_kwh)


def power_samples(watts=1000):
    return [NumericSample(START + timedelta(minutes=30 * i), watts) for i in range(13)]


def test_multiple_mode_changes_allocate_measured_power_and_conserve():
    modes = [
        StateSample(START - timedelta(hours=1), "idle"),
        StateSample(START + timedelta(hours=1), "heating_zones"),
        StateSample(START + timedelta(hours=3), "heating_water"),
        StateSample(START + timedelta(hours=4), "unavailable"),
        StateSample(START + timedelta(hours=5), "idle"),
    ]
    result = allocate_interval(
        start=START, end=END, mode_samples=modes, power_samples=power_samples(),
        counter_samples=[NumericSample(START, 100), NumericSample(END, 106)],
        classifier=ecodan_category,
    )
    assert result.allocations_kwh == pytest.approx({
        "idle": 2, "confirmed_heating": 2, "dhw": 1, "heating_assumed": 1,
    })
    assert sum(result.allocations_kwh.values()) + result.residual_kwh == pytest.approx(result.meter_kwh)
    assert result.quality == "uncertain"


def test_unknown_ecodan_is_assumed_heating_but_idle_is_not():
    assert ecodan_category(StateSample(START, "unknown")) == "heating_assumed"
    assert ecodan_category(StateSample(START, "idle")) == "idle"


def test_ac_action_selected_mode_and_explicit_seasonal_fallback_are_distinct():
    assert ac_category(StateSample(START, "heat", {"hvac_action": "heating"})) == "heating_selected"
    assert ac_category(StateSample(START, "heat", {})) == "heating_selected"
    assert ac_category(StateSample(START, "unknown", {}), seasonal_fallback=True) == "heating_estimated"


@pytest.mark.parametrize(
    ("selected", "action", "expected"),
    [
        ("heat", "idle", "heating_selected"),
        ("cool", "idle", "cooling_selected"),
        ("off", "heating", "standby"),
        ("dry", "cooling", "dry_fan"),
        ("fan_only", "heating", "dry_fan"),
        ("auto", "heating", "heating"),
        ("unavailable", "idle", "standby"),
        ("auto", "idle", "standby"),
        ("auto", "", "unknown"),
    ],
)
def test_ac_selected_mode_has_priority_over_hvac_action(selected, action, expected):
    assert ac_category(StateSample(START, selected, {"hvac_action": action})) == expected


def test_missing_power_is_unallocatable_residual_not_time_prorated():
    result = allocate_interval(
        start=START, end=END,
        mode_samples=[StateSample(START, "heating_zones")], power_samples=[],
        counter_samples=[NumericSample(START, 10), NumericSample(END, 12)],
        classifier=ecodan_category,
    )
    assert result.allocations_kwh == {}
    assert result.residual_kwh == 2
    assert result.quality == "poor"


def test_counter_reset_is_explicit_and_delta_continues():
    delta, resets, rejected = counter_delta([
        NumericSample(START, 99), NumericSample(START + timedelta(hours=2), 100),
        NumericSample(START + timedelta(hours=3), 0.5), NumericSample(END, 2.0),
    ], START, END)
    assert delta == pytest.approx(3.0)
    assert resets == 1
    assert rejected == 0


def test_counter_transient_drop_and_rebound_is_unavailable():
    delta, resets, rejected = counter_delta([
        NumericSample(START, 310), NumericSample(START + timedelta(hours=2), 0),
        NumericSample(END, 310.06),
    ], START, END)
    assert delta is None
    assert resets == 1 and rejected == 1


def test_counter_spike_is_rejected():
    delta, _resets, rejected = counter_delta([
        NumericSample(START, 0), NumericSample(END, 1000),
    ], START, END, maximum_delta_kwh=100)
    assert delta == 0
    assert rejected == 1


def test_non_melcloud_heat_pump_state_mapping_is_configurable():
    classifier = configurable_classifier(
        {"space_heat": "confirmed_heating", "hot_water": "dhw", "stopped": "idle"},
        kind="heat_pump",
    )
    assert classifier(StateSample(START, "space_heat")) == "confirmed_heating"
    assert classifier(StateSample(START, "hot_water")) == "dhw"
    assert classifier(StateSample(START, "vendor_new_state")) == "heating_assumed"

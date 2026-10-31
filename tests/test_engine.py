from datetime import date
import json
from pathlib import Path

import pytest

from custom_components.home_energy_intelligence.engine import DailyAggregator, DailyInput, DeviceHistory
from custom_components.home_energy_intelligence.models import NumericSample
from custom_components.home_energy_intelligence.timeutils import local_day_bounds


def test_october_25_whole_house_reconciliation_and_25_hour_day():
    fixture = json.loads((Path(__file__).parent / "fixtures" / "october_2026_stats.json").read_text())
    values = fixture["2026-10-25"]
    start, end = local_day_bounds(date(2026, 10, 25), fixture["timezone"])
    source = DailyInput(devices={}, site_counters={
        "pv": [NumericSample(start, 100), NumericSample(end, 100 + values["pv_kwh"])],
        "import": [NumericSample(start, 200), NumericSample(end, 200 + values["import_kwh"])],
        "export": [NumericSample(start, 50), NumericSample(end, 50 + values["export_kwh"])],
    }, temperatures={})
    result = DailyAggregator(fixture["timezone"]).aggregate(date(2026, 10, 25), source)
    assert result.household_kwh == pytest.approx(values["expected_household_kwh"])
    assert result.day_seconds == 25 * 3600


def test_required_recorder_fixture_values_are_preserved():
    fixture = json.loads((Path(__file__).parent / "fixtures" / "october_2026_stats.json").read_text())
    assert list(fixture["ecodan_daily_kwh"].values()) == [15.87707, 15.26269, 20.19328, 28.78444]


def test_persisted_checkpoints_survive_recorder_expiration_and_are_primary():
    day = date(2026, 10, 25)
    start, end = local_day_bounds(day, "Europe/Ljubljana")
    from custom_components.home_energy_intelligence.timeutils import cycle_boundaries
    boundaries = cycle_boundaries(day, "Europe/Ljubljana", 6)
    totals = [5.0, 5.0, 5.0, 5.19328]
    checkpoints = []
    for left, right, total in zip(boundaries, boundaries[1:], totals):
        checkpoints.append({
            "interval_start_utc": left.isoformat(), "interval_end_utc": right.isoformat(),
            "meter_kwh": total, "allocations_kwh": {"confirmed_heating": total},
            "residual_kwh": 0, "covered_seconds": (right - left).total_seconds(),
            "period_seconds": (right - left).total_seconds(), "quality": "good",
        })
    source = DailyInput(
        devices={"hp": DeviceHistory(kind="heat_pump", state_map={})},
        site_counters={}, temperatures={}, checkpoints={"hp": checkpoints},
    )
    result = DailyAggregator("Europe/Ljubljana").aggregate(day, source)
    hp = result.allocations["hp"]
    assert hp["meter_kwh"] == pytest.approx(20.19328)
    assert hp["categories"]["confirmed_heating"] == pytest.approx(20.19328)
    assert hp["checkpoint_intervals"] == 4
    assert hp["classification_source"] == "persisted_checkpoints"
    assert result.day_seconds == 25 * 3600


def test_historical_counter_correction_changes_residual_not_confirmed_classification():
    day = date(2026, 10, 25)
    start, end = local_day_bounds(day, "Europe/Ljubljana")
    from custom_components.home_energy_intelligence.timeutils import cycle_boundaries
    boundaries = cycle_boundaries(day, "Europe/Ljubljana", 6)
    checkpoints = [{
        "interval_start_utc": left.isoformat(), "interval_end_utc": right.isoformat(),
        "meter_kwh": 5, "allocations_kwh": {"confirmed_heating": 4.5},
        "residual_kwh": .5, "covered_seconds": (right - left).total_seconds(),
        "period_seconds": (right - left).total_seconds(), "quality": "partial",
    } for left, right in zip(boundaries, boundaries[1:])]
    history = DeviceHistory(
        counter=[NumericSample(start, 100), NumericSample(end, 121)],
        kind="heat_pump", state_map={},
    )
    result = DailyAggregator("Europe/Ljubljana").aggregate(
        day, DailyInput({"hp": history}, {}, {}, checkpoints={"hp": checkpoints})
    )
    hp = result.allocations["hp"]
    assert hp["meter_kwh"] == 21
    assert hp["categories"]["confirmed_heating"] == 18
    assert hp["residual_kwh"] == 3
    assert hp["authoritative_counter_source"] == "daily_recorder"


@pytest.mark.parametrize("day", [date(2026, 9, 18), date(2026, 9, 30)])
def test_unreliable_counter_cannot_restore_inflated_checkpoint(day):
    from custom_components.home_energy_intelligence.timeutils import cycle_boundaries
    boundaries = cycle_boundaries(day, "Europe/Ljubljana", 6)
    checkpoints = [{"interval_start_utc": left.isoformat(), "interval_end_utc": right.isoformat(),
                    "meter_kwh": 80, "allocations_kwh": {"cooling": .1},
                    "residual_kwh": 79.9, "covered_seconds": 3600, "quality": "partial"}
                   for left, right in zip(boundaries, boundaries[1:])]
    history = DeviceHistory(kind="air_conditioner")
    source = DailyInput({"ac": history}, {}, {}, checkpoints={"ac": checkpoints},
                        provenance={"counter_collection": {"equipment.ac.energy": "recorder_states_unreliable"}})
    allocation = DailyAggregator("Europe/Ljubljana").aggregate(day, source).allocations["ac"]
    assert allocation["meter_kwh"] is None
    assert allocation["categories"]["cooling"] == pytest.approx(.4)
    assert allocation["authoritative_counter_source"] == "unavailable"
    history.counter = [NumericSample(boundaries[0], 0), NumericSample(boundaries[-1], 2)]
    source.provenance["counter_collection"]["equipment.ac.energy"] = "recorder_statistics"
    allocation = DailyAggregator("Europe/Ljubljana").aggregate(day, source).allocations["ac"]
    assert allocation["meter_kwh"] == pytest.approx(2)
    assert allocation["residual_kwh"] == pytest.approx(1.6)
    assert allocation["authoritative_counter_source"] == "recorder_statistics"


def test_heat_pump_residual_is_assumed_only_for_reporting_and_remains_auditable():
    day = date(2026, 10, 25)
    start, end = local_day_bounds(day, "Europe/Ljubljana")
    history = DeviceHistory(
        counter=[NumericSample(start, 10), NumericSample(end, 20)],
        kind="heat_pump", state_map={},
    )
    result = DailyAggregator(
        "Europe/Ljubljana", assumed_heating_reporting="include_in_heating_total"
    ).aggregate(day, DailyInput({"hp": history}, {}, {}))
    hp = result.allocations["hp"]
    assert hp["categories"].get("confirmed_heating", 0) == 0
    assert hp["categories"].get("dhw", 0) == 0
    assert hp["residual_kwh"] == 10
    assert hp["uncertain_residual_kwh"] == 10
    assert hp["reported_assumed_heating_kwh"] == 10
    assert hp["reported_heating_kwh"] == 10
    assert hp["shelly_total_kwh"] == 10
    assert hp["heating_assumed_unknown_mode_kwh"] == 0
    assert hp["heating_assumed_residual_kwh"] == 10
    assert hp["raw_unassigned_before_fallback_kwh"] == 10
    assert hp["remaining_unassigned_after_fallback_kwh"] == 0


def test_missing_optional_sources_remain_unavailable_not_zero():
    result = DailyAggregator("Europe/Ljubljana").aggregate(
        date(2026, 10, 24), DailyInput(devices={}, site_counters={}, temperatures={})
    )
    assert result.household_kwh is None
    assert result.pv_kwh is None and result.import_kwh is None and result.export_kwh is None
    assert result.non_hvac_kwh is None and result.quality == "missing"


def test_no_pv_installation_uses_grid_balance_without_inventing_pv_measurement():
    day = date(2026, 10, 24)
    start, end = local_day_bounds(day, "Europe/Ljubljana")
    source = DailyInput(
        devices={}, temperatures={}, site_configured={"import", "export"},
        site_counters={
            "import": [NumericSample(start, 10), NumericSample(end, 20)],
            "export": [NumericSample(start, 2), NumericSample(end, 3)],
        },
    )
    result = DailyAggregator("Europe/Ljubljana").aggregate(day, source)
    assert result.pv_kwh is None
    assert result.household_kwh == 9


def test_temperature_rejection_payload_is_json_serializable_for_database():
    day = date(2026, 10, 24)
    start, _end = local_day_bounds(day, "Europe/Ljubljana")
    result = DailyAggregator("Europe/Ljubljana").aggregate(
        day, DailyInput(devices={}, site_counters={},
                        temperatures={"outdoor": [NumericSample(start, -55)]})
    )
    json.dumps(result.as_dict(), allow_nan=False)
    assert result.temperatures["outdoor"]["rejections"][0]["reason"] == "invalid_sentinel"

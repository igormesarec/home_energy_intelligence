from datetime import UTC, datetime, timedelta

from custom_components.home_energy_intelligence.analytics import (
    attach_canonical, canonical_energy, combine_interval_records,
)


EQUIPMENT = [
    {"equipment_id": "hp-a", "display_name": "Heat pump A", "kind": "heat_pump", "enabled": True},
    {"equipment_id": "ac-a", "display_name": "Bedroom AC", "kind": "air_conditioner", "enabled": True},
]


def test_canonical_contract_preserves_uncertain_categories_and_reconciles():
    record = {
        "household_kwh": 20.0, "pv_kwh": 8.0, "import_kwh": 15.0,
        "export_kwh": 3.0, "non_hvac_kwh": 10.0, "quality": "partial",
        "allocations": {
            "hp-a": {
                "meter_kwh": 7.0,
                "categories": {"confirmed_heating": 3.0, "dhw": 1.0, "idle": 0.5},
                "reported_assumed_heating_kwh": 2.5, "residual_kwh": 2.5,
                "quality": "partial",
            },
            "ac-a": {
                "meter_kwh": 3.0,
                "categories": {"heating": 1.0, "heating_selected": 1.0,
                               "standby": 0.5, "unknown": 0.5},
                "residual_kwh": 0.0, "quality": "uncertain",
            },
        },
    }
    energy = canonical_energy(record, EQUIPMENT)
    assert energy["confirmed_heat_pump_heating_kwh"] == 3
    assert energy["assumed_heat_pump_heating_kwh"] == 2.5
    assert energy["selected_ac_heating_kwh"] == 1
    assert energy["space_heating_kwh"] == 7.5
    assert energy["dhw_kwh"] == 1
    assert energy["idle_kwh"] == 1
    assert energy["unknown_kwh"] == .5
    assert energy["unallocated_discrepancy_kwh"] == 0
    assert energy["pv_consumed_on_site_kwh"] == 5
    assert energy["pv_self_consumption_percent"] == 62.5


def test_missing_equipment_never_becomes_false_zero():
    energy = canonical_energy({"allocations": {}, "quality": "missing"}, EQUIPMENT)
    assert energy["complete"] is False
    assert energy["unallocated_discrepancy_kwh"] is None
    assert all(item["available"] is False for item in energy["equipment"])


def _hour(start, household=1.0):
    end = start + timedelta(hours=1)
    return {
        "local_date": "2026-10-25", "timezone": "Europe/Ljubljana",
        "period_start_utc": start.isoformat(), "period_end_utc": end.isoformat(),
        "day_seconds": 3600, "household_kwh": household, "pv_kwh": None,
        "import_kwh": household, "export_kwh": 0.0, "non_hvac_kwh": 0.0,
        "quality": "good", "allocations": {"hp-a": {
            "meter_kwh": household, "categories": {"confirmed_heating": household},
            "residual_kwh": 0.0, "reported_assumed_heating_kwh": 0.0,
            "covered_seconds": 3600, "period_seconds": 3600, "quality": "good",
        }}, "temperatures": {},
    }


def test_combined_25_hour_day_keeps_all_elapsed_hours():
    start = datetime(2026, 10, 24, 22, tzinfo=UTC)
    combined = combine_interval_records([_hour(start + timedelta(hours=index)) for index in range(25)])
    assert combined["day_seconds"] == 90_000
    assert combined["household_kwh"] == 25
    result = attach_canonical(combined, EQUIPMENT[:1])
    assert result["energy"]["space_heating_kwh"] == 25


def test_month_temperature_mean_is_weighted_by_valid_elapsed_duration():
    first = _hour(datetime(2026, 10, 1, tzinfo=UTC))
    first["temperatures"] = {"indoor": {"mean": 10, "minimum": 9, "maximum": 11,
                                          "coverage_seconds": 3600, "period_seconds": 3600,
                                          "quality": "good", "latest_value": 10,
                                          "latest_timestamp": first["period_end_utc"]}}
    second = _hour(datetime(2026, 10, 2, tzinfo=UTC))
    second["temperatures"] = {"indoor": {"mean": 20, "minimum": 19, "maximum": 21,
                                           "coverage_seconds": 3 * 3600,
                                           "period_seconds": 4 * 3600, "quality": "partial",
                                           "latest_value": 20,
                                           "latest_timestamp": second["period_end_utc"]}}
    combined = combine_interval_records([first, second])
    temperature = combined["temperatures"]["indoor"]
    assert temperature["mean"] == 17.5
    assert temperature["minimum"] == 9
    assert temperature["maximum"] == 21
    assert temperature["coverage_percent"] == 80
    assert temperature["quality"] == "partial"

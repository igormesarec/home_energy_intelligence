from datetime import UTC, datetime, timedelta

from custom_components.home_energy_intelligence.analytics import (
    attach_canonical, canonical_energy, combine_daily_records, combine_interval_records,
    daily_coverage, interval_coverage, replace_with_partial_day,
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
    assert energy["space_heating_kwh"] is None
    assert energy["dhw_kwh"] is None
    assert energy["cooling_kwh"] is None
    assert energy["known_space_heating_kwh"] == 0
    assert all(item["available"] is False for item in energy["equipment"])


def test_missing_ac_preserves_known_hp_subtotal_but_total_is_unavailable():
    record = {"allocations": {"hp-a": {"meter_kwh": 2, "categories": {
        "confirmed_heating": 2}, "residual_kwh": 0}}, "quality": "partial"}
    energy = canonical_energy(record, EQUIPMENT)
    assert energy["known_space_heating_kwh"] == 2
    assert energy["space_heating_kwh"] is None
    assert energy["confirmed_heat_pump_heating_kwh"] == 2
    assert energy["confirmed_ac_heating_kwh"] is None


def test_missing_device_meter_does_not_render_false_device_zeroes():
    record = {"allocations": {
        "hp-a": {"meter_kwh": None, "categories": {}, "quality": "missing_counter"},
    }}
    energy = canonical_energy(record, EQUIPMENT[:1])
    device = energy["equipment"][0]
    assert device["available"] is False
    assert device["confirmed_heating_kwh"] is None
    assert device["cooling_kwh"] is None
    assert device["known_confirmed_heating_kwh"] == 0


def test_device_level_selected_and_estimated_cooling_are_explicit():
    record = {"allocations": {
        "hp-a": {"meter_kwh": 0, "categories": {}, "residual_kwh": 0},
        "ac-a": {"meter_kwh": 3, "categories": {
            "cooling": 1, "cooling_selected": .75, "cooling_estimated": .25,
            "standby": 1}, "residual_kwh": 0},
    }}
    energy = canonical_energy(record, EQUIPMENT)
    ac = next(item for item in energy["equipment"] if item["equipment_id"] == "ac-a")
    assert ac["confirmed_cooling_kwh"] == 1
    assert ac["selected_cooling_kwh"] == .75
    assert ac["estimated_cooling_kwh"] == .25
    assert ac["cooling_kwh"] == energy["cooling_kwh"] == 2


def test_negative_discrepancy_is_preserved_with_its_sign():
    record = {"allocations": {
        "hp-a": {"meter_kwh": 1, "categories": {"confirmed_heating": 2},
                 "residual_kwh": -1},
        "ac-a": {"meter_kwh": 0, "categories": {}, "residual_kwh": 0},
    }}
    assert canonical_energy(record, EQUIPMENT)["unallocated_discrepancy_kwh"] == -1


def test_other_hvac_fan_dry_reconciles_without_discrepancy():
    record = {"household_kwh": 2, "allocations": {
        "hp-a": {"meter_kwh": 0, "categories": {}, "quality": "good"},
        "ac-a": {"meter_kwh": 2, "categories": {"dry_fan": 2}, "quality": "good"},
    }}
    energy = canonical_energy(record, EQUIPMENT)
    assert energy["other_hvac_kwh"] == 2
    assert energy["known_other_hvac_kwh"] == 2
    assert energy["unknown_kwh"] == 0
    assert energy["unallocated_discrepancy_kwh"] == 0


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


def test_internal_missing_hour_is_detected_even_when_latest_hour_exists():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    records = [_hour(start), _hour(start + timedelta(hours=2))]
    coverage = interval_coverage(records, start, start + timedelta(hours=3))
    assert coverage["present_intervals"] == 2
    assert coverage["coverage_percent"] == 200 / 3
    assert coverage["missing_intervals"] == [{
        "start": (start + timedelta(hours=1)).isoformat(),
        "end": (start + timedelta(hours=2)).isoformat(),
    }]
    combined = combine_interval_records(
        records, expected_start=start, expected_end=start + timedelta(hours=3)
    )
    assert combined["household_kwh"] is None
    assert combined["known_household_kwh"] == 2
    assert attach_canonical(combined, EQUIPMENT[:1])["energy"]["space_heating_kwh"] is None


def test_23_and_25_hour_expected_interval_counts():
    spring = datetime(2026, 3, 28, 23, tzinfo=UTC)
    fall = datetime(2026, 10, 24, 22, tzinfo=UTC)
    assert interval_coverage([], spring, spring + timedelta(hours=23))["expected_intervals"] == 23
    assert interval_coverage([], fall, fall + timedelta(hours=25))["expected_intervals"] == 25


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


def test_missing_temperature_day_reduces_period_coverage_instead_of_denominator():
    first = _hour(datetime(2026, 10, 1, tzinfo=UTC))
    first["temperatures"] = {"indoor": {
        "mean": 10, "minimum": 10, "maximum": 10,
        "coverage_seconds": 3600, "period_seconds": 3600, "quality": "good",
    }}
    second = _hour(datetime(2026, 10, 2, tzinfo=UTC))
    combined = combine_interval_records([first, second])
    assert combined["temperatures"]["indoor"]["coverage_percent"] == 50
    assert combined["temperatures"]["indoor"]["quality"] == "partial"


def test_full_period_summary_is_independent_of_page_and_marks_missing_day():
    first = _hour(datetime(2026, 1, 1, tzinfo=UTC), 2)
    first["local_date"] = "2026-01-01"
    third = _hour(datetime(2026, 1, 3, tzinfo=UTC), 4)
    third["local_date"] = "2026-01-03"
    combined = combine_daily_records([first, third], "2026-01-01", "2026-01-03")
    assert combined["known_household_kwh"] == 6
    assert combined["period_coverage"]["missing_dates"] == ["2026-01-02"]
    assert canonical_energy(combined, EQUIPMENT[:1])["space_heating_kwh"] is None


def test_period_quality_layers_do_not_invalidate_unrelated_meter_totals():
    record = _hour(datetime(2026, 1, 1, tzinfo=UTC), 2)
    record.update({"local_date": "2026-01-01", "quality": "partial", "day_seconds": 86400})
    coverage = daily_coverage([record], "2026-01-01", "2026-01-01")
    assert coverage["complete"] is False
    assert coverage["date_coverage"]["complete"] is True
    assert coverage["meter_coverage"]["complete"] is True
    assert coverage["allocation_coverage"]["complete"] is True
    record["period_coverage"] = coverage
    energy = canonical_energy(record, EQUIPMENT[:1])
    assert energy["household_kwh"] == 2
    assert energy["space_heating_kwh"] == 2


def test_absent_daily_record_counts_in_temperature_period_denominator():
    record = _hour(datetime(2026, 1, 1, tzinfo=UTC), 2)
    record.update({"local_date": "2026-01-01", "day_seconds": 86400})
    record["temperatures"] = {"outdoor-selected": {
        "mean": 5, "minimum": 3, "maximum": 7,
        "coverage_seconds": 86400, "period_seconds": 86400, "quality": "good",
    }}
    combined = combine_daily_records(
        [record], "2026-01-01", "2026-01-02",
        temperature_source_ids=["outdoor-selected"],
    )
    temperature = combined["temperatures"]["outdoor-selected"]
    assert temperature["mean"] == 5
    assert temperature["minimum"] == 3
    assert temperature["maximum"] == 7
    assert temperature["coverage_percent"] == 50
    assert temperature["quality"] == "partial"


def test_today_partial_replaces_finalized_row_in_memory_only():
    finalized = {"local_date": "2026-10-29", "household_kwh": 99, "finalized": True}
    earlier = {"local_date": "2026-10-28", "household_kwh": 10}
    partial = {"local_date": "2026-10-29", "household_kwh": 4}
    result = replace_with_partial_day([finalized, earlier], partial)
    assert [item["local_date"] for item in result] == ["2026-10-28", "2026-10-29"]
    assert result[-1]["household_kwh"] == 4
    assert result[-1]["partial"] is True
    assert result[-1]["finalized"] is False
    assert finalized["household_kwh"] == 99
    assert replace_with_partial_day(
        [finalized, earlier], None, local_date="2026-10-29"
    ) == [earlier]

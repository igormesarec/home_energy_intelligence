from datetime import UTC, datetime, timedelta

import pytest

from custom_components.home_energy_intelligence.analytics import (
    attach_canonical, canonical_energy, combine_daily_records, combine_interval_records,
    daily_coverage, heating_response_profile, interval_coverage, replace_with_partial_day,
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
                "coverage_percent": 80, "quality": "partial",
            },
            "ac-a": {
                "meter_kwh": 3.0,
                "categories": {"heating": 1.0, "heating_selected": 1.0,
                               "standby": 0.5, "unknown": 0.5},
                "residual_kwh": 0.0, "coverage_percent": 100,
                "quality": "uncertain",
            },
        },
    }
    energy = canonical_energy(record, EQUIPMENT)
    assert energy["confirmed_heat_pump_heating_kwh"] is None
    assert energy["known_confirmed_heat_pump_heating_kwh"] == 3
    assert energy["known_assumed_heat_pump_heating_kwh"] == 2.5
    assert energy["selected_ac_heating_kwh"] == 1
    assert energy["space_heating_kwh"] is None
    assert energy["known_space_heating_kwh"] == 7.5
    assert energy["dhw_kwh"] is None
    assert energy["known_idle_kwh"] == 1
    assert energy["unknown_kwh"] == .5
    assert energy["unallocated_discrepancy_kwh"] is None
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


def test_legacy_allocation_without_coverage_keeps_meter_and_known_categories_unknown():
    record = {"allocations": {"hp-a": {
        "meter_kwh": 4, "categories": {"confirmed_heating": 3},
        "residual_kwh": 1, "quality": "good",
    }}}
    energy = canonical_energy(record, EQUIPMENT[:1])
    device = energy["equipment"][0]
    assert device["meter_kwh"] == 4
    assert device["coverage_percent"] is None
    assert device["coverage_known"] is False
    assert device["classification_complete"] is False
    assert device["confirmed_heating_kwh"] is None
    assert device["known_confirmed_heating_kwh"] == 3
    assert energy["space_heating_kwh"] is None


def test_legacy_percentage_is_reconstructed_only_when_explicitly_present():
    record = {"allocations": {"hp-a": {
        "meter_kwh": 4, "categories": {"confirmed_heating": 4},
        "coverage_percent": 100, "quality": "good",
    }}}
    energy = canonical_energy(record, EQUIPMENT[:1])
    assert energy["equipment"][0]["classification_complete"] is True
    assert energy["space_heating_kwh"] == 4


def test_missing_ac_preserves_known_hp_subtotal_but_total_is_unavailable():
    record = {"allocations": {"hp-a": {"meter_kwh": 2, "categories": {
        "confirmed_heating": 2}, "residual_kwh": 0}}, "quality": "partial"}
    energy = canonical_energy(record, EQUIPMENT)
    assert energy["known_space_heating_kwh"] == 2
    assert energy["space_heating_kwh"] is None
    assert energy["confirmed_heat_pump_heating_kwh"] is None
    assert energy["known_confirmed_heat_pump_heating_kwh"] == 2
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
        "hp-a": {"meter_kwh": 0, "categories": {}, "residual_kwh": 0,
                 "coverage_percent": 100},
        "ac-a": {"meter_kwh": 3, "categories": {
            "cooling": 1, "cooling_selected": .75, "cooling_estimated": .25,
            "standby": 1}, "residual_kwh": 0, "coverage_percent": 100},
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
                 "residual_kwh": -1, "coverage_percent": 100},
        "ac-a": {"meter_kwh": 0, "categories": {}, "residual_kwh": 0,
                 "coverage_percent": 100},
    }}
    assert canonical_energy(record, EQUIPMENT)["unallocated_discrepancy_kwh"] == -1


def test_other_hvac_fan_dry_reconciles_without_discrepancy():
    record = {"household_kwh": 2, "allocations": {
        "hp-a": {"meter_kwh": 0, "categories": {}, "coverage_percent": 100,
                 "quality": "good"},
        "ac-a": {"meter_kwh": 2, "categories": {"dry_fan": 2},
                 "coverage_percent": 100, "quality": "good"},
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
    record["allocations"]["hp-a"].update({
        "covered_seconds": 86400, "period_seconds": 86400,
    })
    coverage = daily_coverage([record], "2026-01-01", "2026-01-01")
    assert coverage["complete"] is True
    assert coverage["quality"] == "good"
    assert coverage["stored_quality_provenance"] == {"2026-01-01": "partial"}
    assert coverage["date_coverage"]["complete"] is True
    assert coverage["meter_coverage"]["complete"] is True
    assert coverage["allocation_coverage"]["complete"] is True
    record["period_coverage"] = coverage
    energy = canonical_energy(record, EQUIPMENT[:1])
    assert energy["household_kwh"] == 2
    assert energy["space_heating_kwh"] == 2
    assert energy["quality"] == "good"


def test_repaired_representative_temperature_recomputes_overall_quality():
    record = _hour(datetime(2026, 1, 1, tzinfo=UTC), 2)
    record.update({"local_date": "2026-01-01", "quality": "partial", "day_seconds": 86400})
    record["allocations"]["hp-a"].update({
        "covered_seconds": 86400, "period_seconds": 86400,
    })
    missing = daily_coverage(
        [record], "2026-01-01", "2026-01-01", equipment_ids=["hp-a"],
        temperature_source_ids=["outdoor"], timezone="UTC",
    )
    assert missing["energy_complete"] is True
    assert missing["temperature_complete"] is False
    assert missing["temperature_coverage"]["outdoor"]["quality"] == "missing"
    assert missing["quality"] == "partial"

    record["temperatures"] = {"outdoor": {
        "mean": 5.0, "minimum": 2.0, "maximum": 7.0,
        "coverage_seconds": 86400, "period_seconds": 86400,
        "coverage_percent": 100, "quality": "good",
    }}
    repaired = daily_coverage(
        [record], "2026-01-01", "2026-01-01", equipment_ids=["hp-a"],
        temperature_source_ids=["outdoor"], timezone="UTC",
    )
    assert repaired["quality"] == "good"
    assert repaired["complete"] is True
    assert repaired["stored_quality_provenance"]["2026-01-01"] == "partial"


def test_legacy_unknown_classification_is_unknown_not_good_or_missing():
    record = _hour(datetime(2026, 1, 1, tzinfo=UTC), 2)
    record.update({"local_date": "2026-01-01", "day_seconds": 86400})
    for key in ("covered_seconds", "period_seconds", "coverage_percent"):
        record["allocations"]["hp-a"].pop(key, None)
    coverage = daily_coverage(
        [record], "2026-01-01", "2026-01-01",
        equipment_ids=["hp-a"], timezone="UTC",
    )
    assert coverage["quality"] == "unknown"
    assert coverage["allocation_coverage"]["coverage_known"] is False
    assert coverage["complete"] is False


def test_optional_temperature_source_does_not_downgrade_required_quality():
    record = _hour(datetime(2026, 1, 1, tzinfo=UTC), 2)
    record.update({"local_date": "2026-01-01", "day_seconds": 86400})
    record["allocations"]["hp-a"].update({
        "covered_seconds": 86400, "period_seconds": 86400,
    })
    record["temperatures"] = {"outdoor": {
        "mean": 5.0, "minimum": 2.0, "maximum": 7.0,
        "coverage_seconds": 86400, "period_seconds": 86400,
        "coverage_percent": 100, "quality": "good",
    }}
    combined = combine_daily_records(
        [record], "2026-01-01", "2026-01-01",
        temperature_source_ids=["outdoor", "optional-room"],
        required_temperature_source_ids=["outdoor"],
        equipment_ids=["hp-a"], timezone="UTC",
    )
    assert combined["period_coverage"]["quality"] == "good"
    assert combined["period_coverage"]["temperature_coverage"]["outdoor"][
        "complete"
    ] is True
    assert combined["period_coverage"]["optional_temperature_coverage"][
        "optional-room"
    ]["quality"] == "missing"


def test_uncertain_but_fully_classified_allocation_is_complete_with_uncertainty():
    record = _hour(datetime(2026, 1, 1, tzinfo=UTC), 2)
    record.update({
        "local_date": "2026-01-01", "day_seconds": 86400,
        "quality": "uncertain",
    })
    record["allocations"]["hp-a"].update({
        "covered_seconds": 86400, "period_seconds": 86400,
        "coverage_percent": 100, "quality": "uncertain",
    })
    coverage = daily_coverage(
        [record], "2026-01-01", "2026-01-01",
        equipment_ids=["hp-a"], timezone="UTC",
    )
    assert coverage["allocation_coverage"]["complete"] is True
    assert coverage["allocation_coverage"]["quality"] == "uncertain"
    assert coverage["allocation_coverage"]["uncertain_dates"] == ["2026-01-01"]
    assert coverage["complete"] is True
    assert coverage["quality"] == "uncertain"
    assert coverage["uncertain_quality_dates"] == ["2026-01-01"]


def test_allocation_coverage_is_elapsed_time_not_good_day_count():
    record = _hour(datetime(2026, 1, 1, tzinfo=UTC), 2)
    record.update({"local_date": "2026-01-01", "day_seconds": 86400})
    record["allocations"]["hp-a"].update({
        "covered_seconds": 18 * 3600, "period_seconds": 24 * 3600,
        "quality": "partial",
    })
    coverage = daily_coverage(
        [record], "2026-01-01", "2026-01-01",
        equipment_ids=["hp-a"], timezone="UTC",
    )
    assert coverage["allocation_coverage"]["coverage_percent"] == 75
    assert coverage["allocation_coverage"]["classified_seconds"] == 18 * 3600
    assert coverage["allocation_coverage"]["complete"] is False


def test_no_configured_equipment_is_unavailable_not_false_full_coverage():
    coverage = daily_coverage([], "2026-01-01", "2026-01-01", equipment_ids=[])
    assert coverage["allocation_coverage"]["coverage_percent"] is None
    assert coverage["allocation_coverage"]["quality"] == "unavailable"
    assert coverage["allocation_coverage"]["complete"] is False


def test_unknown_legacy_coverage_makes_period_percentage_unknown_not_zero_or_full():
    record = _hour(datetime(2026, 1, 1, tzinfo=UTC), 2)
    record.update({"local_date": "2026-01-01", "day_seconds": 86400})
    allocation = record["allocations"]["hp-a"]
    allocation.pop("covered_seconds")
    allocation.pop("period_seconds")
    allocation.pop("coverage_percent", None)
    coverage = daily_coverage(
        [record], "2026-01-01", "2026-01-01",
        equipment_ids=["hp-a"], timezone="UTC",
    )
    layer = coverage["allocation_coverage"]
    assert layer["coverage_percent"] is None
    assert layer["coverage_known"] is False
    assert layer["complete"] is False
    assert layer["unknown_coverage_equipment"] == [{
        "date": "2026-01-01", "equipment_id": "hp-a",
    }]


def test_date_coverage_is_dst_duration_weighted_and_partial_today_is_not_full_day():
    spring = _hour(datetime(2026, 3, 28, 23, tzinfo=UTC), 2)
    spring.update({"local_date": "2026-03-29", "day_seconds": 23 * 3600})
    autumn = _hour(datetime(2026, 10, 24, 22, tzinfo=UTC), 2)
    autumn.update({"local_date": "2026-10-25", "day_seconds": 25 * 3600})
    spring_coverage = daily_coverage(
        [spring], "2026-03-29", "2026-03-29", timezone="Europe/Ljubljana",
    )
    autumn_coverage = daily_coverage(
        [autumn], "2026-10-25", "2026-10-25", timezone="Europe/Ljubljana",
    )
    assert spring_coverage["date_coverage"]["expected_seconds"] == 23 * 3600
    assert autumn_coverage["date_coverage"]["expected_seconds"] == 25 * 3600

    partial = _hour(datetime(2026, 10, 29, tzinfo=UTC), 2)
    partial.update({
        "local_date": "2026-10-29", "day_seconds": 6 * 3600,
        "partial": True, "finalized": False,
    })
    current = daily_coverage(
        [partial], "2026-10-29", "2026-10-29", timezone="Europe/Ljubljana",
    )
    assert current["date_coverage"]["coverage_percent"] == 25
    assert current["date_coverage"]["complete"] is False
    assert current["meter_coverage"]["coverage_percent"] == 100


def test_site_baseline_equations_preserve_physical_balance_and_rounding_evidence():
    record = {
        "household_kwh": 45.0, "pv_kwh": 28.5, "import_kwh": 28.9,
        "export_kwh": 12.3, "allocations": {}, "quality": "partial",
    }
    energy = canonical_energy(record, [])
    assert energy["pv_consumed_on_site_kwh"] == 16.2
    assert energy["calculated_household_kwh"] == pytest.approx(45.1)
    assert abs(energy["household_balance_discrepancy_kwh"]) <= 0.11


@pytest.mark.parametrize(
    ("record", "consumed", "self_consumption", "calculated"),
    [
        ({"household_kwh": 5, "pv_kwh": 0, "import_kwh": 5, "export_kwh": 0},
         0, None, 5),
        ({"household_kwh": 5, "pv_kwh": None, "import_kwh": 5, "export_kwh": 0},
         None, None, None),
        ({"household_kwh": 5, "pv_kwh": 2, "import_kwh": None, "export_kwh": 1},
         1, 50, None),
        ({"household_kwh": 5, "pv_kwh": 2, "import_kwh": 5, "export_kwh": 3},
         None, None, None),
        ({"household_kwh": 5, "pv_kwh": -1, "import_kwh": 5, "export_kwh": 0},
         None, None, None),
    ],
)
def test_pv_balance_distinguishes_zero_missing_and_physically_invalid(
    record, consumed, self_consumption, calculated,
):
    energy = canonical_energy({**record, "allocations": {}}, [])
    assert energy["pv_consumed_on_site_kwh"] == consumed
    assert energy["pv_self_consumption_percent"] == self_consumption
    assert energy["calculated_household_kwh"] == calculated


def test_supplied_equipment_baseline_preserves_partial_ac_as_evidence_not_complete_heat():
    equipment = [
        {"equipment_id": "hp", "display_name": "Configured heat pump",
         "kind": "heat_pump", "enabled": True},
        {"equipment_id": "ac", "display_name": "Configured AC",
         "kind": "air_conditioner", "enabled": True},
    ]
    record = {"allocations": {
        "hp": {"meter_kwh": 21.0, "categories": {
            "confirmed_heating": 17.6, "dhw": 3.4,
        }, "coverage_percent": 100, "quality": "good"},
        "ac": {"meter_kwh": 2.8, "categories": {"unknown": 2.8},
               "coverage_percent": 44, "quality": "partial",
               "classification_source": "configured-state-and-power-history"},
    }}
    energy = canonical_energy(record, equipment)
    assert energy["confirmed_heat_pump_heating_kwh"] == 17.6
    assert energy["dhw_kwh"] == 3.4
    assert energy["space_heating_kwh"] is None
    assert energy["known_space_heating_kwh"] == 17.6
    ac = next(item for item in energy["equipment"] if item["equipment_id"] == "ac")
    assert ac["meter_kwh"] == 2.8
    assert ac["confirmed_heating_kwh"] is None
    assert ac["unknown_kwh"] == 2.8
    assert ac["coverage_percent"] == 44
    assert ac["classification_source"] == "configured-state-and-power-history"


def test_october_29_partial_fixture_keeps_3_6_kwh_as_known_not_confirmed_total():
    equipment = [
        {"equipment_id": "hp", "display_name": "Configured heat pump",
         "kind": "heat_pump", "enabled": True},
        {"equipment_id": "aurora", "display_name": "Aurora",
         "kind": "air_conditioner", "enabled": True},
    ]
    record = {"local_date": "2026-10-29", "household_kwh": 6.7,
              "partial": True, "finalized": False, "allocations": {
        "hp": {"meter_kwh": 2.8, "categories": {"confirmed_heating": 2.8},
               "coverage_percent": 100, "quality": "good"},
        "aurora": {"meter_kwh": 0.8, "categories": {"heating_selected": 0.8},
                   "coverage_percent": 50, "quality": "partial",
                   "classification_source": "configured-state-and-power-history"},
    }}
    energy = canonical_energy(record, equipment)
    assert energy["space_heating_kwh"] is None
    assert energy["known_space_heating_kwh"] == pytest.approx(3.6)
    aurora = next(item for item in energy["equipment"]
                  if item["equipment_id"] == "aurora")
    assert aurora["meter_kwh"] == 0.8
    assert aurora["selected_heating_kwh"] is None
    assert aurora["known_selected_heating_kwh"] == 0.8
    assert aurora["classification_complete"] is False


def test_heating_response_retains_validated_and_excluded_daily_evidence():
    valid = _hour(datetime(2026, 1, 1, tzinfo=UTC), 4)
    valid.update({"local_date": "2026-01-01", "finalized": True})
    valid["temperatures"] = {"outdoor": {
        "mean": 5, "minimum": 3, "maximum": 7, "coverage_percent": 98,
        "coverage_seconds": 84672, "period_seconds": 86400, "quality": "good",
    }}
    low_coverage = _hour(datetime(2026, 1, 2, tzinfo=UTC), 4)
    low_coverage.update({"local_date": "2026-01-02", "finalized": True})
    low_coverage["temperatures"] = {"outdoor": {
        "mean": 6, "minimum": 4, "maximum": 8, "coverage_percent": 90,
        "quality": "partial",
    }}
    profile = heating_response_profile(
        [valid, low_coverage], EQUIPMENT[:1], outdoor_source_id="outdoor",
        floor_area_m2=180, minimum_temperature_coverage=95,
    )
    assert profile["minimum_regression_points"] == 10
    assert profile["minimum_outdoor_span_c"] == 8
    assert profile["points"][0]["status"] == "validated"
    assert profile["points"][0]["normalized_heating_kwh_m2_day"] == pytest.approx(4 / 180)
    assert profile["points"][0]["confirmed_heating_kwh"] == 4
    assert profile["points"][1]["status"] == "excluded"
    assert "insufficient_outdoor_coverage" in profile["points"][1]["exclusion_reasons"]


def test_heating_response_excludes_missing_source_invalid_temperature_and_incomplete_ac():
    record = _hour(datetime(2026, 1, 1, tzinfo=UTC), 4)
    record.update({"local_date": "2026-01-01", "finalized": True})
    record["temperatures"] = {"outdoor": {
        "mean": -55, "coverage_percent": 100, "quality": "good",
    }}
    invalid = heating_response_profile(
        [record], EQUIPMENT, outdoor_source_id="outdoor", floor_area_m2=180,
    )["points"][0]
    assert "invalid_outdoor_temperature" in invalid["exclusion_reasons"]
    assert "incomplete_heating_classification" in invalid["exclusion_reasons"]
    missing_source = heating_response_profile(
        [record], EQUIPMENT[:1], outdoor_source_id=None, floor_area_m2=180,
    )["points"][0]
    assert "representative_outdoor_not_configured" in missing_source["exclusion_reasons"]


@pytest.mark.parametrize(
    ("coverage", "expected_status"),
    [(95.0, "validated"), (94.99, "excluded"), (30.0, "excluded")],
)
def test_heating_response_uses_one_exact_coverage_boundary(coverage, expected_status):
    record = _hour(datetime(2026, 1, 1, tzinfo=UTC), 4)
    record.update({"local_date": "2026-01-01", "finalized": True})
    record["temperatures"] = {"outdoor": {
        "mean": 5, "minimum": 3, "maximum": 7,
        "coverage_percent": coverage,
        "coverage_seconds": 86400 * coverage / 100,
        "period_seconds": 86400, "quality": "good" if coverage >= 100 else "partial",
    }}
    point = heating_response_profile(
        [record], EQUIPMENT[:1], outdoor_source_id="outdoor",
        outdoor_source_is_measured_air=True, floor_area_m2=180,
        minimum_temperature_coverage=95,
    )["points"][0]
    assert point["status"] == expected_status
    assert point["outdoor_response_coverage_qualified"] is (coverage >= 95)
    assert point["outdoor_full_temporal_coverage"] is (coverage >= 100)
    assert point["outdoor_validated_seconds"] == pytest.approx(86400 * coverage / 100)
    assert point["reconciliation_discrepancy_kwh"] == 0
    assert point["equipment_heating"][0]["equipment_id"] == "hp-a"


def test_heating_response_rejects_non_outdoor_role_partial_day_and_non_finite_values():
    record = _hour(datetime(2026, 1, 1, tzinfo=UTC), 4)
    record.update({"local_date": "2026-01-01", "partial": True, "finalized": False})
    record["temperatures"] = {"outdoor": {
        "mean": 5, "coverage_percent": 100, "coverage_seconds": 86400,
        "period_seconds": 86400, "quality": "good",
    }}
    point = heating_response_profile(
        [record], EQUIPMENT[:1], outdoor_source_id="outdoor",
        outdoor_source_is_measured_air=False, floor_area_m2=180,
    )["points"][0]
    assert "partial_day" in point["exclusion_reasons"]
    assert "representative_source_not_measured_outdoor_air" in point["exclusion_reasons"]

    record.update({"partial": False, "finalized": True})
    record["temperatures"]["outdoor"]["mean"] = float("inf")
    point = heating_response_profile(
        [record], EQUIPMENT[:1], outdoor_source_id="outdoor",
        outdoor_source_is_measured_air=True, floor_area_m2=180,
    )["points"][0]
    assert "invalid_outdoor_temperature" in point["exclusion_reasons"]


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

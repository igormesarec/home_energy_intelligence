from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

import pytest

from custom_components.home_energy_intelligence.database import EnergyDatabase, SCHEMA_VERSION
from custom_components.home_energy_intelligence.thermal import (
    cumulative_intervals, match_classified_electricity, normalize_manual_reading,
    ratio_of_sums, thermal_performance_points,
)


def manual(stamp="2026-10-25T00:00:00", **values):
    day, wall = stamp.split("T")
    return normalize_manual_reading({
        "ecodan_equipment_id": "hp-main", "meter_identity": "ecodan-generation",
        "local_date": day, "local_time": wall, "source_label": "operator log",
        "unit": "kWh", "heating_thermal_kwh": 100,
        **values,
    }, "Europe/Ljubljana")


def test_manual_counter_normalization_dst_and_invalid_values():
    result = manual(unit="MWh", heating_thermal_kwh=1.25, dhw_thermal_kwh=0.5)
    assert result["heating_thermal_kwh"] == 1250
    assert result["dhw_thermal_kwh"] == 500
    with pytest.raises(ValueError, match="ambiguous"):
        manual("2026-10-25T02:30:00")
    second = manual("2026-10-25T02:30:00", fold=1)
    assert second["fold"] == 1
    with pytest.raises(ValueError, match="does not exist"):
        manual("2026-03-29T02:30:00")
    with pytest.raises(ValueError, match="at least zero"):
        manual(heating_thermal_kwh=-1)


def test_cumulative_intervals_preserve_baseline_reset_and_elapsed_time():
    readings = []
    for index, (stamp, heat, dhw) in enumerate((
        ("2026-10-25T00:00:00", 100, 50),
        ("2026-10-26T00:00:00", 108, 53),
        ("2026-10-28T00:00:00", 5, 60),
    ), start=1):
        item = manual(stamp, heating_thermal_kwh=heat, dhw_thermal_kwh=dhw)
        item["id"] = index
        readings.append(item)
    intervals = cumulative_intervals(readings)
    assert len(intervals) == 2  # first observation is only a baseline
    assert intervals[0]["heating_thermal_delta_kwh"] == 8
    assert intervals[0]["dhw_thermal_delta_kwh"] == 3
    assert intervals[0]["elapsed_seconds"] == 25 * 3600  # autumn DST day
    assert intervals[1]["heating_thermal_delta_kwh"] is None
    assert "heating_thermal_kwh:counter_reset_or_correction" in intervals[1]["issues"]
    assert intervals[1]["dhw_thermal_delta_kwh"] == 7


def hourly(start, *, heat, dhw, assumed=0):
    return {
        "period_start_utc": start.isoformat(),
        "period_end_utc": (start + timedelta(hours=1)).isoformat(),
        "household_kwh": heat + dhw + assumed,
        "allocations": {"hp-main": {
            "meter_kwh": heat + dhw + assumed,
            "categories": {"confirmed_heating": heat, "heating_assumed": assumed,
                           "dhw": dhw},
            "covered_seconds": 3600, "expected_seconds": 3600, "quality": "good",
        }},
    }


def test_exact_interval_ecodan_cop_includes_assumed_as_provisional_and_excludes_ac():
    start = datetime(2026, 10, 25, tzinfo=UTC)
    interval = {
        "ecodan_equipment_id": "hp-main", "meter_identity": "meter",
        "start_utc": start.isoformat(), "end_utc": (start + timedelta(hours=2)).isoformat(),
        "heating_thermal_delta_kwh": 8.0, "dhw_thermal_delta_kwh": 6.0,
    }
    equipment = [
        {"equipment_id": "hp-main", "display_name": "Ecodan", "kind": "heat_pump", "enabled": True},
        {"equipment_id": "ac-1", "display_name": "Aurora", "kind": "air_conditioner", "enabled": True},
    ]
    records = [hourly(start, heat=1, dhw=1.5), hourly(start + timedelta(hours=1), heat=1, dhw=1.5)]
    # Missing AC allocation makes whole-record totals partial, but does not invent AC heat.
    match = match_classified_electricity(interval, records, equipment[:1])
    assert match["exact_match"] is True
    assert match["heating_electricity_kwh"] == 2
    assert match["dhw_electricity_kwh"] == 3
    points = thermal_performance_points([interval], records, equipment[:1], {
        (interval["start_utc"], interval["end_utc"]): 5.0,
    })
    assert points[0]["heating_cop"] == 4
    assert points[0]["dhw_cop"] == 2
    assumed = match_classified_electricity(
        interval, [hourly(start, heat=1, dhw=1.5, assumed=.5), records[1]], equipment[:1]
    )
    assert assumed["exact_match"] is True
    assert assumed["heating_electricity_kwh"] == pytest.approx(2.5)
    assert assumed["confirmed_heating_electricity_kwh"] == pytest.approx(2)
    assert assumed["assumed_heating_electricity_kwh"] == pytest.approx(.5)
    assert assumed["heating_match_quality"] == "provisional_assumed_heating"
    assert ratio_of_sums([8, 12], [2, 3]) == 4


def test_thermal_ledger_migration_crud_audit_and_unrelated_data_preserved(tmp_path):
    database = EnergyDatabase(tmp_path / "energy.sqlite3")
    database.initialize()
    with database.connect() as connection:
        connection.execute("UPDATE schema_version SET version=5")
        connection.execute("DROP TABLE thermal_meter_reading_audit")
        connection.execute("DROP TABLE thermal_meter_readings")
    database.initialize()
    first = database.save_thermal_reading(manual())
    changed = dict(manual(heating_thermal_kwh=101)); changed["note"] = "corrected"
    second = database.save_thermal_reading(changed, first["id"])
    assert second["revision"] == 2
    assert database.delete_thermal_reading(first["id"])
    audit = database.thermal_reading_audit(first["id"])
    assert [item["action"] for item in audit] == ["create", "update", "delete"]
    assert database.thermal_readings() == []
    with database.connect() as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == SCHEMA_VERSION


def test_thermal_configuration_translations_are_synchronized():
    root = Path(__file__).parents[1] / "custom_components/home_energy_intelligence"
    documents = [json.loads((root / path).read_text(encoding="utf-8")) for path in (
        "strings.json", "translations/en.json", "translations/sl.json",
    )]
    required = {
        "thermal_source_mode", "ecodan_thermal_equipment_id",
        "heating_thermal_energy_entity", "dhw_thermal_energy_entity",
        "vendor_heating_electricity_entity", "vendor_dhw_electricity_entity",
    }
    for document in documents:
        for flow in ("config", "options"):
            fields = document[flow]["step"]["data_sources"]["data"]
            assert required.issubset(fields)
            assert "thermal_source_incomplete" in document[flow]["error"]

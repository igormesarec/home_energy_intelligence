from datetime import UTC, datetime

import pytest

from custom_components.home_energy_intelligence.database import SCHEMA_VERSION, EnergyDatabase
from custom_components.home_energy_intelligence.monthly import (
    build_rolling_month_report,
    monthly_metric_catalog,
    normalize_monthly_snapshot,
    rolling_months,
)


def snapshot(**overrides):
    value = {
        "ecodan_equipment_id": "hp_main", "meter_identity": "monthly_report",
        "report_month": "2026-10", "cutoff_local_date": "2026-10-31",
        "status": "partial", "heating_thermal_kwh": 30,
        "dhw_thermal_kwh": 20, "vendor_heating_electricity_kwh": 9,
        "vendor_dhw_electricity_kwh": 4, "source_label": "Ecodan report",
        "unit": "kWh",
    }
    value.update(overrides)
    return value


def daily_record(
    *, local_date="2026-10-31", period_start="2026-10-30T23:00:00+00:00",
    period_end="2026-10-31T23:00:00+00:00",
):
    return {
        "local_date": local_date, "timezone": "Europe/Ljubljana",
        "period_start_utc": period_start,
        "period_end_utc": period_end, "day_seconds": 86400,
        "household_kwh": 20, "pv_kwh": 5, "import_kwh": 15, "export_kwh": 0,
        "non_hvac_kwh": 4, "quality": "good", "temperatures": {},
        "allocations": {"hp_main": {
            "meter_kwh": 16, "categories": {
                "confirmed_heating": 8, "dhw": 5, "idle": 1,
                "heating_assumed": 1,
            },
            "reported_assumed_heating_kwh": 2,
            "heating_assumed_unknown_mode_kwh": 1,
            "heating_assumed_residual_kwh": 1,
            "raw_unassigned_before_fallback_kwh": 2,
            "remaining_unassigned_after_fallback_kwh": 0,
            "signed_overallocation_or_reconciliation_error_kwh": 0,
            "uncertain_residual_kwh": 1, "residual_kwh": 1,
            "covered_seconds": 86400, "period_seconds": 86400,
            "coverage_percent": 100, "quality": "uncertain",
        }},
    }


def test_monthly_snapshot_is_distinct_nullable_and_unit_normalized():
    result = normalize_monthly_snapshot(snapshot(
        heating_thermal_kwh="30000", dhw_thermal_kwh="",
        vendor_heating_electricity_kwh=None, vendor_dhw_electricity_kwh=None,
        unit="Wh",
    ), "Europe/Ljubljana")
    assert result["heating_thermal_kwh"] == 30
    assert result["dhw_thermal_kwh"] is None
    assert result["cutoff_certainty"] == "day_only"
    assert result["cutoff_utc"] == "2026-10-31T23:00:00+00:00"


def test_monthly_snapshot_rejects_wrong_month_and_dst_gap():
    with pytest.raises(ValueError, match="within"):
        normalize_monthly_snapshot(
            snapshot(cutoff_local_date="2026-11-01"), "Europe/Ljubljana"
        )
    with pytest.raises(ValueError, match="does not exist"):
        normalize_monthly_snapshot(snapshot(
            report_month="2026-03", cutoff_local_date="2026-03-29",
            cutoff_local_time="02:30",
        ), "Europe/Ljubljana")


def test_rolling_report_has_all_13_calendar_positions_and_shelly_primary_cop():
    normalized = normalize_monthly_snapshot(snapshot(
        report_month="2026-01", cutoff_local_date="2026-01-02",
        cutoff_local_time="00:00", cutoff_certainty="exact",
    ), "Europe/Ljubljana")
    normalized["id"] = 1
    result = build_rolling_month_report(
        daily_records=[daily_record(
            local_date="2026-01-01", period_start="2025-12-31T23:00:00+00:00",
            period_end="2026-01-01T23:00:00+00:00",
        )], snapshots=[normalized],
        equipment=[{
            "equipment_id": "hp_main", "display_name": "Heat pump",
            "kind": "heat_pump", "enabled": True,
        }],
        timezone="Europe/Ljubljana", end_month="2026-01", count=13,
    )
    assert result["months"] == rolling_months("2026-01", 13)
    assert len(result["rows"]) == 13
    january = result["rows"][-1]
    assert january["ecodan_heating_electricity_kwh"] == pytest.approx(10)
    assert january["ecodan_dhw_electricity_kwh"] == pytest.approx(5)
    assert january["raw_unassigned_before_fallback_kwh"] == pytest.approx(2)
    assert january["primary_heating_cop"] == pytest.approx(3)
    assert january["primary_dhw_cop"] == pytest.approx(4)
    assert january["primary_cop_quality"] == "provisional_assumed_heating"
    assert january["vendor_heating_cop"] == pytest.approx(30 / 9)
    assert january["shelly_minus_vendor_kwh"] == pytest.approx(3)
    assert january["shelly_minus_vendor_percent"] == pytest.approx(100 * 3 / 13)
    assert january["cutoff_match"] is True
    assert result["rows"][0]["quality"] == "missing"


def test_final_month_requires_exact_month_end_boundary():
    with pytest.raises(ValueError, match="final month"):
        normalize_monthly_snapshot(snapshot(status="final"), "Europe/Ljubljana")
    result = normalize_monthly_snapshot(snapshot(
        status="final", cutoff_local_date="2026-11-01",
        cutoff_local_time="00:00", cutoff_certainty="exact",
    ), "Europe/Ljubljana")
    assert result["status"] == "final"
    assert result["cutoff_utc"] == "2026-10-31T23:00:00+00:00"


def test_mismatched_monthly_cutoff_never_claims_primary_cop():
    normalized = normalize_monthly_snapshot(
        snapshot(cutoff_local_date="2026-10-30"), "Europe/Ljubljana"
    )
    normalized["id"] = 1
    report = build_rolling_month_report(
        daily_records=[daily_record()], snapshots=[normalized],
        equipment=[{"equipment_id": "hp_main", "kind": "heat_pump", "enabled": True}],
        timezone="Europe/Ljubljana", end_month="2026-10", count=1,
    )["rows"][0]
    assert report["primary_heating_cop"] is None
    assert report["primary_cop_quality"] == "unavailable"
    assert report["vendor_heating_cop"] == pytest.approx(30 / 9)
    assert report["shelly_minus_vendor_kwh"] is None
    assert report["shelly_minus_vendor_percent"] is None


def test_latest_same_month_snapshot_replaces_mtd_value_without_summing():
    older = normalize_monthly_snapshot(snapshot(
        report_month="2026-01", cutoff_local_date="2026-01-01",
        cutoff_local_time="12:00", cutoff_certainty="exact",
        heating_thermal_kwh=12,
    ), "Europe/Ljubljana")
    newer = normalize_monthly_snapshot(snapshot(
        report_month="2026-01", cutoff_local_date="2026-01-02",
        cutoff_local_time="00:00", cutoff_certainty="exact",
        heating_thermal_kwh=30,
    ), "Europe/Ljubljana")
    older["id"], newer["id"] = 1, 2
    report = build_rolling_month_report(
        daily_records=[daily_record(
            local_date="2026-01-01", period_start="2025-12-31T23:00:00+00:00",
            period_end="2026-01-01T23:00:00+00:00",
        )], snapshots=[older, newer],
        equipment=[{"equipment_id": "hp_main", "kind": "heat_pump", "enabled": True}],
        timezone="Europe/Ljubljana", end_month="2026-01", count=1,
    )["rows"][0]
    assert report["snapshot_count"] == 1
    assert report["heating_thermal_kwh"] == 30


def test_missing_monthly_heat_is_unavailable_not_zero_cop():
    august = normalize_monthly_snapshot(snapshot(
        report_month="2026-08", cutoff_local_date="2026-08-20",
        heating_thermal_kwh=None, dhw_thermal_kwh=None,
        vendor_heating_electricity_kwh=9, vendor_dhw_electricity_kwh=63,
    ), "Europe/Ljubljana")
    august["id"] = 1
    row = build_rolling_month_report(
        daily_records=[], snapshots=[august],
        equipment=[{"equipment_id": "hp_main", "kind": "heat_pump", "enabled": True}],
        timezone="Europe/Ljubljana", end_month="2026-08", count=1,
    )["rows"][0]
    assert row["heating_thermal_kwh"] is None
    assert row["vendor_heating_cop"] is None


def test_monthly_catalog_contains_site_ecodan_and_dynamic_ac_metrics():
    catalog = monthly_metric_catalog([{
        "equipment_id": "living_room", "display_name": "Living room",
        "kind": "air_conditioner", "enabled": True,
    }])
    ids = {item["id"] for item in catalog}
    assert {
        "household_electricity_kwh", "pv_production_kwh", "grid_import_kwh",
        "grid_export_kwh", "ecodan_total_measured_electricity_kwh",
        "ecodan_confirmed_heating_electricity_kwh",
        "ecodan_assumed_heating_kwh", "ecodan_dhw_electricity_kwh",
        "ac_heating_electricity_kwh", "ac_cooling_electricity_kwh",
        "whole_house_heating_electricity_kwh", "whole_house_heating_kwh_m2",
        "ecodan_total_generated_thermal_kwh", "primary_combined_cop",
        "shelly_minus_vendor_kwh", "shelly_minus_vendor_percent",
        "equipment.living_room.heating_kwh", "equipment.living_room.cooling_kwh",
    } <= ids


def test_monthly_primary_cop_requires_complete_month_start_and_all_heat_pumps():
    normalized = normalize_monthly_snapshot(snapshot(), "Europe/Ljubljana")
    normalized["id"] = 1
    report = build_rolling_month_report(
        daily_records=[daily_record()], snapshots=[normalized],
        equipment=[
            {"equipment_id": "hp_main", "kind": "heat_pump", "enabled": True},
            {"equipment_id": "hp_second", "kind": "heat_pump", "enabled": True},
        ],
        timezone="Europe/Ljubljana", end_month="2026-10", count=1,
    )["rows"][0]
    assert report["snapshot_equipment_complete"] is False
    assert report["cutoff_match"] is False
    assert report["primary_heating_cop"] is None


def test_v7_monthly_ledger_audit_and_delete_preserve_existing_database(tmp_path):
    database = EnergyDatabase(tmp_path / "energy.sqlite3")
    database.initialize()
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO model_parameters(key,value_json,updated_at) VALUES (?,?,?)",
            ("preserved", '{"value":42}', datetime.now(UTC).isoformat()),
        )
        connection.execute("DROP TABLE monthly_ecodan_snapshot_audit")
        connection.execute("DROP TABLE monthly_ecodan_snapshots")
        connection.execute("UPDATE schema_version SET version=6")
    database.initialize()
    with database.connect() as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == SCHEMA_VERSION
        assert connection.execute(
            "SELECT value_json FROM model_parameters WHERE key='preserved'"
        ).fetchone()[0] == '{"value":42}'
    payload = normalize_monthly_snapshot(snapshot(), "Europe/Ljubljana")
    created = database.save_monthly_ecodan_snapshot(payload)
    assert created["revision"] == 1
    payload["note"] = "corrected from source report"
    updated = database.save_monthly_ecodan_snapshot(payload, created["id"])
    assert updated["revision"] == 2
    assert database.delete_monthly_ecodan_snapshot(created["id"])
    audit = database.monthly_ecodan_snapshot_audit(created["id"])
    assert [item["action"] for item in audit] == ["create", "update", "delete"]
    assert database.monthly_ecodan_snapshots() == []

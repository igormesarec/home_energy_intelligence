from copy import deepcopy
from datetime import date
import json

import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from custom_components.home_energy_intelligence.database import EnergyDatabase, SCHEMA_VERSION
from custom_components.home_energy_intelligence.timeutils import local_day_bounds
from custom_components.home_energy_intelligence.electricity_recovery import prepare_recovery_day, summarize_provisional_evidence


def evidence(day, *, value=1.0):
    start, end = local_day_bounds(date.fromisoformat(day), "Europe/Ljubljana")
    return {"local_date": day, "source_key": "equipment.hp", "source_entity_id": "sensor.hp",
            "timezone": "Europe/Ljubljana", "interval_start_utc": start.isoformat(),
            "interval_end_utc": end.isoformat(), "verification_status": "provisional",
            "meter_kwh": value, "categories": {"unknown": value}, "residual_kwh": 0,
            "source": "recorder", "calculation_version": "historical-preview-v1"}


@pytest.mark.parametrize(("day", "seconds"), [("2026-03-29", 82800), ("2026-10-25", 90000)])
def test_recovered_daily_heating_requires_confirmed_dates_identity_and_temperature(day, seconds):
    from custom_components.home_energy_intelligence.configuration import EquipmentMapping, TemperatureMapping
    from custom_components.home_energy_intelligence.electricity_recovery import recovered_heating_points
    start, end = local_day_bounds(date.fromisoformat(day), "Europe/Ljubljana")
    record = {**evidence(day), "categories": {"heating_assumed": .7, "unknown": .3}, "meter_coverage_seconds": seconds}
    outdoor = TemperatureMapping(source_id="out", display_name="Outdoor", entity_id="sensor.outdoor", role="outdoor")
    temp = {"mean": 5, "coverage_percent": 100, "quality": "good", "period_start_utc": start.isoformat(), "period_end_utc": end.isoformat(), "source_provenance": {"entity_id": "sensor.outdoor", "attribute": None, "role": "outdoor", "mapping_effective_at": "2025-01-01T00:00:00+00:00"}}
    daily = [{"local_date": day, "temperatures": {"out": temp}}]
    device = EquipmentMapping(equipment_id="hp", display_name="HP", kind="heat_pump", commissioning_date="2025-01-01", historical_energy_mappings=[{"entity_id": "sensor.hp", "start_date": "2025-01-01", "end_date": "2027-01-01"}])
    def run(device=device, occupied=(), temp=temp):
        return recovered_heating_points({"records": [record]}, occupied, [{"local_date": day, "temperatures": {"out": temp}}], [device], outdoor, "Europe/Ljubljana", 95)
    point = run()["points"][0]
    assert point["day_seconds"] == seconds and point["heating_kwh"] == .7 and "unknown" not in point["categories"]
    open_ended = EquipmentMapping(equipment_id="hp", display_name="HP", kind="heat_pump", commissioning_date="2025-01-01", historical_energy_mappings=[{"entity_id": "sensor.hp", "start_date": day}])
    assert run(device=open_ended)["points"][0]["heating_kwh"] == .7
    bounded = EquipmentMapping(equipment_id="hp", display_name="HP", kind="heat_pump", commissioning_date="2025-01-01", historical_energy_mappings=[{"entity_id": "sensor.hp", "start_date": "2025-01-01", "end_date": day}])
    assert run(device=bounded)["points"][0]["heating_kwh"] == .7
    expired = EquipmentMapping(equipment_id="hp", display_name="HP", kind="heat_pump", commissioning_date="2025-01-01", historical_energy_mappings=[{"entity_id": "sensor.hp", "start_date": "2025-01-01", "end_date": "2025-12-31"}])
    assert run(device=expired)["excluded"][0]["reason"] == "historical_mapping_unverified_or_mismatched"
    unknown = EquipmentMapping(equipment_id="hp", display_name="HP", kind="heat_pump")
    assert run(device=unknown)["excluded"][0]["reason"] == "commissioning_date_unknown"
    assert run(occupied=[(day, "equipment.hp")])["excluded"][0]["reason"] == "overlapping_authoritative_source_day"
    assert run(temp={**temp, "mean": 155})["excluded"][0]["reason"] == "outdoor_temperature_invalid_or_incomplete"
    assert run(temp={**temp, "coverage_percent": 94})["excluded"][0]["reason"] == "outdoor_temperature_invalid_or_incomplete"
    remapped = EquipmentMapping(equipment_id="hp", display_name="HP", kind="heat_pump", commissioning_date="2025-01-01", historical_energy_mappings=[{"entity_id": "sensor.old", "start_date": "2025-01-01", "end_date": day}])
    assert run(device=remapped)["excluded"][0]["reason"] == "historical_mapping_unverified_or_mismatched"
    later = EquipmentMapping(equipment_id="hp", display_name="HP", kind="heat_pump", commissioning_date="2027-01-01")
    assert run(device=later)["excluded"][0]["reason"] == "before_commissioning"
    assert run(temp={**temp, "source_provenance": {"entity_id": "sensor.flow", "role": "supply"}})["excluded"][0]["reason"] == "outdoor_source_identity_unverified"


@pytest.mark.asyncio
async def test_v02_report_accepts_open_ended_mapping(hass, monkeypatch):
    import inspect
    from custom_components.home_energy_intelligence import websocket
    day = "2026-09-28"
    start, end = local_day_bounds(date.fromisoformat(day), "Europe/Ljubljana")
    temp = {"mean": 8, "coverage_percent": 100, "quality": "good", "period_start_utc": start.isoformat(), "period_end_utc": end.isoformat(), "source_provenance": {"entity_id": "sensor.outdoor", "attribute": None, "role": "outdoor", "mapping_effective_at": "2025-01-01T00:00:00+00:00"}}
    source = {**evidence(day), "categories": {"heating_assumed": .7, "unknown": .3}, "meter_coverage_seconds": 86400}
    db = SimpleNamespace(history_period=lambda *_: [{"local_date": day, "temperatures": {"out": temp}, "allocations": {}}], electricity_recovery_evidence_range=lambda *_: {"records": [source]}, electricity_occupied_sources=lambda *_: [], thermal_readings_period=lambda *_: [])
    config = {"equipment": json.dumps([{"equipment_id": "hp", "display_name": "HP", "kind": "heat_pump", "energy_entity": "sensor.hp", "commissioning_date": "2025-01-01", "historical_energy_mappings": [{"entity_id": "sensor.hp", "start_date": "2026-09-01"}]}]), "temperature_sources": json.dumps([{"source_id": "out", "display_name": "Outdoor", "entity_id": "sensor.outdoor", "role": "outdoor"}]), "representative_outdoor_source_id": "out"}
    coordinator = SimpleNamespace(database=db, timezone="Europe/Ljubljana", config=config)
    monkeypatch.setattr(websocket, "_coordinator", lambda _: coordinator)
    connection = SimpleNamespace(send_result=Mock(), send_error=Mock())
    await inspect.unwrap(websocket.ws_v02_report)(hass, connection, {"id": 1, "start_date": day, "end_date": day})
    report = connection.send_result.call_args.args[1]
    assert report["recovered_heating"]["whole_house"][0]["known_heating_kwh"] == .7
    assert report["ecodan_reports"]["heating"]["cop"] is None


@pytest.mark.parametrize(("day", "seconds"), [("2026-09-25", 86400), ("2026-09-28", 86400), ("2026-09-30", 86400), ("2026-10-25", 90000)])
def test_whole_house_recovered_subtotal_excludes_missing_unknown_and_overlap(day, seconds):
    from custom_components.home_energy_intelligence.configuration import EquipmentMapping, TemperatureMapping
    from custom_components.home_energy_intelligence.electricity_recovery import recovered_heating_points
    start, end = local_day_bounds(date.fromisoformat(day), "Europe/Ljubljana")
    outdoor = TemperatureMapping(source_id="out", display_name="Outdoor", entity_id="sensor.outdoor", role="outdoor")
    temp = {"mean": 8, "coverage_percent": 100, "quality": "good", "period_start_utc": start.isoformat(), "period_end_utc": end.isoformat(), "source_provenance": {"entity_id": "sensor.outdoor", "attribute": None, "role": "outdoor", "mapping_effective_at": "2025-01-01T00:00:00+00:00"}}
    devices, records = [], []
    for index, name in enumerate(("hp", "ac1", "ac2", "ac3")):
        kind = "heat_pump" if name == "hp" else "air_conditioner"
        devices.append(EquipmentMapping(equipment_id=name, display_name=name, kind=kind, commissioning_date="2025-01-01", historical_energy_mappings=[{"entity_id": f"sensor.{name}", "start_date": "2025-01-01", "end_date": "2027-01-01"}]))
        category = "heating_assumed" if kind == "heat_pump" else "heating_selected"
        records.append({**evidence(day), "source_key": f"equipment.{name}", "source_entity_id": f"sensor.{name}", "categories": {category: index + 1}, "meter_coverage_seconds": seconds})
    def run(items=records, occupied=()):
        return recovered_heating_points({"records": items}, occupied, [{"local_date": day, "temperatures": {"out": temp}}], devices, outdoor, "Europe/Ljubljana", 95)
    complete = run()["whole_house"][0]
    assert complete["known_heating_kwh"] == 10 and len(complete["components"]) == 4
    assert complete["quality"] == "provisional partial known subtotal" and not complete["missing_sources"]
    partial = run(items=records[:-1], occupied=[(day, "equipment.ac2")])["whole_house"][0]
    assert partial["known_heating_kwh"] == 3 and len(partial["missing_sources"]) == 2
    assert {item["reason"] for item in partial["missing_sources"]} == {"overlapping_authoritative_source_day", "source_evidence_unavailable"}
    unknown = [{**records[0], "categories": {"unknown": 1}}, *records[1:]]
    result = run(items=unknown)["whole_house"][0]
    assert result["known_heating_kwh"] == 9 and result["missing_sources"][0]["reason"] == "heating_classification_unavailable"
    ambiguous = recovered_heating_points({"records": records[:-1], "ambiguous": [{"local_date": day, "source_key": "equipment.ac3", "versions": 2}]}, (), [{"local_date": day, "temperatures": {"out": temp}}], devices, outdoor, "Europe/Ljubljana", 95)
    assert ambiguous["whole_house"][0]["missing_sources"][0]["reason"] == "conflicting_evidence"


@pytest.mark.parametrize(("day", "seconds"), [("2026-03-29", 82800), ("2026-09-25", 86400), ("2026-10-25", 90000)])
def test_site_recovered_evidence_keeps_overlap_coverage_and_missing_separate(day, seconds):
    from custom_components.home_energy_intelligence.electricity_recovery import add_recovered_presentation_metrics
    pv = {**evidence(day, value=0), "source_key": "site.pv", "source_entity_id": "sensor.pv", "meter_coverage_seconds": seconds, "counter_source": "recorder_states"}
    imp = {**evidence(day, value=2), "source_key": "site.import", "source_entity_id": "sensor.import", "meter_coverage_seconds": seconds / 2, "counter_source": "recorder_states_partial"}
    bad = {**evidence(day), "source_key": "site.export", "source_entity_id": "sensor.old"}
    sources = {"site.pv": "sensor.pv", "site.import": "sensor.import", "site.export": "sensor.new"}
    summary = summarize_provisional_evidence({"records": [pv, imp, bad]}, {(day, "site.import")}, sources)
    report = {"rows": [{"month": day[:7], "pv_kwh": 7}]}
    add_recovered_presentation_metrics(report, summary, [])
    row = report["rows"][0]
    assert row["recovered_pv_kwh"] == 0 and row["pv_kwh"] == 7
    assert row["recovered_reference_import_kwh"] == 2 and "recovered_import_kwh" not in row
    assert "recovered_export_kwh" not in row and "recovered_reference_export_kwh" not in row
    details = row["recovered_metric_evidence"]["recovered_reference_import_kwh"][0]
    assert details["source_entity_id"] == "sensor.import" and details["expected_seconds"] == seconds
    assert details["coverage_seconds"] == seconds / 2 and details["recovery_kind"] == "overlapping_reference"
    assert {item["reason"] for item in row["recovered_excluded"]} == {"current_source_identity_mismatch", "existing_electrical_record"}


def test_august_thermal_values_remain_separate_from_assumed_recovery():
    from custom_components.home_energy_intelligence.electricity_recovery import add_recovered_presentation_metrics
    row = {"month": "2026-08", "heating_thermal_kwh": 6, "dhw_thermal_kwh": 242,
           "primary_heating_cop": None, "whole_house_heating_electricity_kwh": None}
    item = {"month": "2026-08", "source_key": "equipment.hp", "source_entity_id": "sensor.hp",
            "meter_kwh": 30, "categories": {"heating_assumed": 30}, "days": 1}
    add_recovered_presentation_metrics({"rows": [row]}, {"rows": [item]}, [{"equipment_id": "hp", "kind": "heat_pump"}])
    assert (row["heating_thermal_kwh"], row["dhw_thermal_kwh"]) == (6, 242)
    assert row["recovered_ecodan_assumed_heating_kwh"] == 30
    assert "recovered_ecodan_confirmed_heating_kwh" not in row
    assert row["primary_heating_cop"] is None and row["whole_house_heating_electricity_kwh"] is None


def test_additive_migration_and_idempotent_recovery_preserve_existing_data(tmp_path):
    db = EnergyDatabase(tmp_path / "energy.sqlite3")
    db.initialize()
    with db.connect() as connection:
        connection.execute("INSERT INTO mapping_revisions (effective_at,checksum,payload_json) VALUES (?,?,?)", ("2026-01-01", "old", "{}"))
        connection.execute("""INSERT INTO hourly_records
            (interval_start_utc,interval_end_utc,local_date,timezone,payload_json,checksum,quality,updated_at)
            VALUES (?,?,?,?,?,?,?,?)""", ("2026-09-25T00:00:00+00:00", "2026-09-25T01:00:00+00:00",
            "2026-09-25", "Europe/Ljubljana", '{"energy":3}', "hour-checksum", "good", "now"))
        connection.execute("""INSERT INTO temperature_history_records
            (record_kind,record_key,interval_start_utc,interval_end_utc,local_date,timezone,source_id,
             payload_json,checksum,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            ("hourly", "saved", "2026-09-25T00:00:00+00:00", "2026-09-25T01:00:00+00:00",
             "2026-09-25", "Europe/Ljubljana", "indoor", '{"mean":22}', "temperature-checksum", "now"))
    first = db.create_electricity_recovery_job("2026-09-25", "2026-09-25", "Europe/Ljubljana", "evidence_only")
    assert db.commit_electricity_recovery_day(first, "2026-09-25", [evidence("2026-09-25")])["inserted"] == 1
    second = db.create_electricity_recovery_job("2026-09-25", "2026-09-25", "Europe/Ljubljana", "evidence_only")
    assert db.commit_electricity_recovery_day(second, "2026-09-25", [evidence("2026-09-25")])["inserted"] == 0
    with db.connect() as connection:
        assert connection.execute("SELECT count(*) FROM electricity_recovery_evidence").fetchone()[0] == 1
        assert connection.execute("SELECT payload_json FROM mapping_revisions WHERE checksum='old'").fetchone()[0] == "{}"
        assert connection.execute("SELECT checksum FROM hourly_records").fetchone()[0] == "hour-checksum"
        assert connection.execute("SELECT checksum FROM temperature_history_records").fetchone()[0] == "temperature-checksum"
        assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == SCHEMA_VERSION


def test_existing_v7_database_is_migrated_without_losing_mapping(tmp_path):
    db = EnergyDatabase(tmp_path / "energy.sqlite3")
    db.initialize()
    with db.connect() as connection:
        connection.execute("INSERT INTO mapping_revisions (effective_at,checksum,payload_json) VALUES (?,?,?)", ("2026-01-01", "saved", '{"entity":"sensor.hp"}'))
        connection.execute("DROP TABLE electricity_recovery_evidence")
        connection.execute("DROP TABLE electricity_recovery_jobs")
        connection.execute("UPDATE schema_version SET version=7")
    db.initialize(recover_corrupt=False)
    with db.connect() as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == 8
        assert connection.execute("SELECT payload_json FROM mapping_revisions WHERE checksum='saved'").fetchone()[0] == '{"entity":"sensor.hp"}'
        assert connection.execute("SELECT count(*) FROM electricity_recovery_evidence").fetchone()[0] == 0


def test_different_versions_are_ambiguous_not_summed_or_selected_by_recency(tmp_path):
    db = EnergyDatabase(tmp_path / "energy.sqlite3")
    db.initialize()
    for value in (1, 2):
        job = db.create_electricity_recovery_job("2026-09-25", "2026-09-25", "Europe/Ljubljana", "evidence_only")
        db.commit_electricity_recovery_day(job, "2026-09-25", [evidence("2026-09-25", value=value)])
    result = db.electricity_recovery_evidence_range("2026-09-01", "2026-09-30")
    assert result["records"] == []
    assert result["ambiguous"] == [{"local_date": "2026-09-25", "source_key": "equipment.hp", "versions": 2}]


def test_corrupt_evidence_is_excluded_from_reporting_candidates(tmp_path):
    db = EnergyDatabase(tmp_path / "energy.sqlite3")
    db.initialize()
    job = db.create_electricity_recovery_job("2026-09-25", "2026-09-25", "Europe/Ljubljana", "evidence_only")
    db.commit_electricity_recovery_day(job, "2026-09-25", [evidence("2026-09-25")])
    with db.connect() as connection:
        connection.execute("UPDATE electricity_recovery_evidence SET payload_json='{}'")
    result = db.electricity_recovery_evidence_range("2026-09-25", "2026-09-25")
    assert result["records"] == []
    assert result["ambiguous"][0]["reason"] == "checksum_mismatch"


def test_exact_source_unverified_evidence_remains_provisional(tmp_path):
    db = EnergyDatabase(tmp_path / "energy.sqlite3")
    db.initialize()
    record = {**evidence("2026-09-25"), "historical_mapping_quality": "unverified historical mapping",
              "counter_source": "recorder_states_partial", "meter_coverage_seconds": 3600,
              "source_provenance": {"energy_entity_id": "sensor.hp"}}
    job = db.create_electricity_recovery_job("2026-09-25", "2026-09-25", "Europe/Ljubljana", "evidence_only")
    db.commit_electricity_recovery_day(job, "2026-09-25", [record])
    selected = db.electricity_recovery_evidence_range("2026-09-25", "2026-09-25")
    assert selected["records"][0]["verification_status"] == "provisional"
    assert selected["records"][0]["categories"] == {"unknown": 1}


def test_unverified_source_mismatch_is_excluded(tmp_path):
    db = EnergyDatabase(tmp_path / "energy.sqlite3")
    db.initialize()
    record = {**evidence("2026-09-25"), "historical_mapping_quality": "unverified historical mapping",
              "counter_source": "influx", "meter_coverage_seconds": 3600,
              "source_provenance": {"energy_entity_id": "sensor.other"}}
    job = db.create_electricity_recovery_job("2026-09-25", "2026-09-25", "Europe/Ljubljana", "evidence_only")
    db.commit_electricity_recovery_day(job, "2026-09-25", [record])
    selected = db.electricity_recovery_evidence_range("2026-09-25", "2026-09-25")
    assert selected["records"] == []
    assert selected["ambiguous"][0]["reason"] == "source_identity_or_coverage_unverified"


def test_influx_evidence_requires_exact_identity_check(tmp_path):
    db = EnergyDatabase(tmp_path / "energy.sqlite3")
    db.initialize()
    record = {**evidence("2026-09-25"), "historical_mapping_quality": "unverified historical mapping",
              "counter_source": "influx", "meter_coverage_seconds": 3600,
              "source_provenance": {"energy_entity_id": "sensor.hp"}}
    job = db.create_electricity_recovery_job("2026-09-25", "2026-09-25", "Europe/Ljubljana", "evidence_only")
    db.commit_electricity_recovery_day(job, "2026-09-25", [record])
    assert db.electricity_recovery_evidence_range("2026-09-25", "2026-09-25")["records"] == []


def test_reporting_evidence_requires_persisted_exact_mapping_event(tmp_path):
    db = EnergyDatabase(tmp_path / "energy.sqlite3")
    db.initialize()
    mapping = {"equipment_id": "hp", "energy_entity": "sensor.hp", "state_entity": "sensor.mode",
               "state_attribute": None, "state_map": {"heat": "confirmed_heating"}, "enabled": True}
    event = db.ensure_mapping_revision({"effective_at": "2026-01-01T00:00:00+00:00", "equipment": [mapping]})
    record = evidence("2026-09-25")
    record["mapping_applicability"] = {"status": "verified", "source_entity_id": "sensor.hp",
        "effective_start_utc": "2026-01-01T00:00:00+00:00", "effective_end_utc": "2026-12-01T00:00:00+00:00",
        "mapping_event_id": event["mapping_event_id"],
        "mapping_identity": {key: mapping[key] for key in ("equipment_id", "energy_entity", "state_entity", "state_attribute", "state_map")}}
    job = db.create_electricity_recovery_job("2026-09-25", "2026-09-25", "Europe/Ljubljana", "evidence_only")
    db.commit_electricity_recovery_day(job, "2026-09-25", [record])
    assert len(db.electricity_recovery_evidence_range("2026-09-25", "2026-09-25")["records"]) == 1
    fallback_record = {**evidence("2026-09-26"), "mapping_applicability": record["mapping_applicability"],
                       "source_provenance": {"fallback_entity": "climate.zone"}}
    fallback_job = db.create_electricity_recovery_job("2026-09-26", "2026-09-26", "Europe/Ljubljana", "evidence_only")
    db.commit_electricity_recovery_day(fallback_job, "2026-09-26", [fallback_record])
    assert db.electricity_recovery_evidence_range("2026-09-26", "2026-09-26")["ambiguous"][0]["reason"] == "mapping_interval_unverified"
    db.ensure_mapping_revision({"effective_at": "2026-09-25T12:00:00+00:00", "equipment": [{**mapping, "state_entity": "sensor.changed"}]})
    blocked = db.electricity_recovery_evidence_range("2026-09-25", "2026-09-25")
    assert blocked["records"] == [] and blocked["ambiguous"][0]["reason"] == "mapping_interval_unverified"


def test_atomic_failure_and_overlap_cancellation(tmp_path):
    db = EnergyDatabase(tmp_path / "energy.sqlite3")
    db.initialize()
    job = db.create_electricity_recovery_job("2026-09-25", "2026-09-26", "Europe/Ljubljana", "evidence_only")
    with pytest.raises(ValueError, match="another"):
        db.create_electricity_recovery_job("2026-09-25", "2026-09-25", "Europe/Ljubljana", "evidence_only")
    invalid = {**evidence("2026-09-25"), "source_key": "equipment.bad", "meter_kwh": 2}
    with pytest.raises(ValueError, match="reconcile"):
        db.commit_electricity_recovery_day(job, "2026-09-25", [evidence("2026-09-25"), invalid])
    with db.connect() as connection:
        assert connection.execute("SELECT count(*) FROM electricity_recovery_evidence").fetchone()[0] == 0
    assert db.electricity_recovery_job(job)["cursor_date"] == "2026-09-25"
    assert db.cancel_electricity_recovery_job(job)
    assert db.commit_electricity_recovery_day(job, "2026-09-25", []) == {"cancelled": True, "inserted": 0}
    assert db.electricity_recovery_job(job)["status"] == "cancelled"


def test_explicit_job_cursor_survives_database_reopen(tmp_path):
    path = tmp_path / "energy.sqlite3"
    db = EnergyDatabase(path)
    db.initialize()
    job = db.create_electricity_recovery_job("2026-09-25", "2026-09-26", "Europe/Ljubljana", "evidence_only")
    db.commit_electricity_recovery_day(job, "2026-09-25", [evidence("2026-09-25")])
    reopened = EnergyDatabase(path)
    jobs = reopened.resumable_electricity_recovery_jobs()
    assert len(jobs) == 1 and jobs[0]["cursor_date"] == "2026-09-26"
    reopened.commit_electricity_recovery_day(job, "2026-09-26", [evidence("2026-09-26")])
    assert reopened.resumable_electricity_recovery_jobs() == []


@pytest.mark.parametrize("day,hours", [("2026-03-29", 23), ("2026-10-25", 25)])
def test_local_day_boundaries_accept_dst_days(tmp_path, day, hours):
    db = EnergyDatabase(tmp_path / "energy.sqlite3")
    db.initialize()
    record = evidence(day)
    assert (local_day_bounds(date.fromisoformat(day), "Europe/Ljubljana")[1] -
            local_day_bounds(date.fromisoformat(day), "Europe/Ljubljana")[0]).total_seconds() == hours * 3600
    job = db.create_electricity_recovery_job(day, day, "Europe/Ljubljana", "evidence_only")
    assert db.commit_electricity_recovery_day(job, day, [record])["status"] == "completed"


def test_dry_run_preserves_input_and_keeps_unknown_and_missing_sources_distinct():
    preview = {"local_date": "2026-09-25", "historical_mapping_quality": "unverified historical mapping",
        "allocations": {"hp": {"meter_kwh": 12.04, "categories": {"unknown": 12.04}, "residual_kwh": 0,
                                "source_provenance": {"energy_entity_id": "sensor.hp", "meter_coverage_seconds": 80000}},
                        "ac": {"meter_kwh": None, "source_provenance": {"energy_entity_id": "sensor.ac"}}},
        "site": {"pv": {"entity_id": "sensor.pv", "meter_kwh": 2.5, "covered_seconds": 3600, "status": "partial_known_subtotal"}}}
    before = deepcopy(preview)
    plan = prepare_recovery_day(preview, "Europe/Ljubljana")
    assert preview == before
    assert len(plan["records"]) == 2 and len(plan["skipped"]) == 1
    assert plan["records"][0]["categories"] == {"unknown": 12.04}
    assert all(row["verification_status"] == "provisional" for row in plan["records"])
    assert plan["records"][1]["quality"] == "partial_known_subtotal"


@pytest.mark.asyncio
async def test_admin_preview_can_return_plan_without_any_database_write(monkeypatch):
    from custom_components.home_energy_intelligence import websocket
    preview = {"local_date": "2026-09-25", "allocations": {"hp": {"meter_kwh": 1,
        "categories": {"unknown": 1}, "residual_kwh": 0,
        "source_provenance": {"energy_entity_id": "sensor.hp"}}}, "site": {}}
    database = Mock()
    coordinator = SimpleNamespace(timezone="Europe/Ljubljana", database=database,
                                  async_preview_historical_energy=AsyncMock(return_value=preview))
    monkeypatch.setattr(websocket, "_coordinator", lambda _: coordinator)
    connection = SimpleNamespace(send_result=Mock(), send_error=Mock())
    handler = websocket.ws_historical_energy_preview.__wrapped__.__wrapped__
    await handler(None, connection, {"id": 1, "start_date": "2026-09-25", "end_date": "2026-09-25",
                                     "attest_current_mapping": False, "evidence_only": True,
                                     "include_recovery_plan": True})
    payload = connection.send_result.call_args.args[1]
    assert payload["recovery_dry_run"][0]["records"][0]["categories"] == {"unknown": 1}
    assert database.mock_calls == []


def test_monthly_overlay_excludes_existing_or_mismatched_days():
    item = {**evidence("2026-09-25"), "historical_mapping_quality": "unverified historical mapping"}
    other = {**item, "local_date": "2026-09-26"}
    result = summarize_provisional_evidence({"records": [item, other], "ambiguous": []}, {"2026-09-25"}, {"equipment.hp": "sensor.hp"})
    assert len(result["rows"]) == 1 and result["rows"][0]["meter_kwh"] == 1
    assert result["rows"][0]["categories"] == {"unknown": 1}
    assert result["skipped"][0]["reason"] == "existing_electrical_record"
    mismatch = summarize_provisional_evidence({"records": [item]}, set(), {"equipment.hp": "sensor.other"})
    assert mismatch["rows"] == [] and mismatch["skipped"][0]["reason"] == "current_source_identity_mismatch"
    mismatch_overlap = summarize_provisional_evidence({"records": [item]}, {"2026-09-25"}, {"equipment.hp": "sensor.other"})
    assert mismatch_overlap["reference_rows"] == [] and mismatch_overlap["skipped"][0]["reason"] == "current_source_identity_mismatch"


@pytest.mark.asyncio
async def test_recovery_worker_resumes_durable_cursor_and_rejects_changed_config(hass, tmp_path):
    from custom_components.home_energy_intelligence.coordinator import HomeEnergyCoordinator
    db = EnergyDatabase(tmp_path / "worker.sqlite3")
    db.initialize()
    entry = SimpleNamespace(entry_id="test", data={"equipment": "[]", "site_sources": {}, "temperature_sources": "[]"}, options={})
    coordinator = HomeEnergyCoordinator(hass, entry, db, registered_mapping={"effective_at": "2026-01-01T00:00:00+00:00"})
    coordinator.timezone = "Europe/Ljubljana"
    fingerprint = coordinator._electricity_recovery_fingerprint()
    job = db.create_electricity_recovery_job("2026-09-25", "2026-09-26", coordinator.timezone, "evidence_only", fingerprint)
    db.commit_electricity_recovery_day(job, "2026-09-25", [evidence("2026-09-25")])
    preview = {"local_date": "2026-09-26", "historical_mapping_quality": "unverified historical mapping",
        "allocations": {"hp": {"meter_kwh": 1, "categories": {"unknown": 1}, "residual_kwh": 0,
        "source_provenance": {"energy_entity_id": "sensor.hp", "meter_coverage_seconds": 3600},
        "authoritative_counter_source": "recorder_states_partial"}}, "site": {}}
    coordinator.async_preview_historical_energy = AsyncMock(return_value=preview)
    await coordinator._async_run_electricity_recovery(job)
    assert db.electricity_recovery_job(job)["status"] == "completed"
    assert coordinator.async_preview_historical_energy.await_args.args[0] == date(2026, 9, 26)
    second = db.create_electricity_recovery_job("2026-09-27", "2026-09-27", coordinator.timezone, "evidence_only", fingerprint)
    coordinator.config["equipment"] = '[{"equipment_id":"changed"}]'
    await coordinator._async_run_electricity_recovery(second)
    assert db.electricity_recovery_job(second)["status"] == "failed"


@pytest.mark.asyncio
async def test_start_rejects_unpreviewed_or_conflicting_requests(hass, tmp_path):
    from custom_components.home_energy_intelligence.coordinator import HomeEnergyCoordinator
    db = EnergyDatabase(tmp_path / "start.sqlite3")
    db.initialize()
    entry = SimpleNamespace(entry_id="test", data={"equipment": "[]", "site_sources": {}, "temperature_sources": "[]"}, options={})
    coordinator = HomeEnergyCoordinator(hass, entry, db, registered_mapping={"effective_at": "2026-01-01T00:00:00+00:00"})
    with pytest.raises(ValueError, match="preview"):
        await coordinator.async_start_electricity_recovery(date(2026, 9, 1), date(2026, 9, 2), evidence_only=True, attest_current_mapping=False, preview_confirmed=True)
    with pytest.raises(ValueError, match="cannot be combined"):
        await coordinator.async_start_electricity_recovery(date(2026, 9, 1), date(2026, 9, 2), evidence_only=True, attest_current_mapping=True, preview_confirmed=True)
    assert db.resumable_electricity_recovery_jobs() == []


@pytest.mark.asyncio
async def test_explicit_start_idempotent_retry_and_cancel_during_day(hass, tmp_path):
    from custom_components.home_energy_intelligence.coordinator import HomeEnergyCoordinator
    db = EnergyDatabase(tmp_path / "execution.sqlite3")
    db.initialize()
    entry = SimpleNamespace(entry_id="test", data={"equipment": "[]", "site_sources": {}, "temperature_sources": "[]"}, options={})
    coordinator = HomeEnergyCoordinator(hass, entry, db, registered_mapping={"effective_at": "2026-01-01T00:00:00+00:00"})
    coordinator.timezone = "Europe/Ljubljana"
    coordinator._schedule_electricity_recovery = Mock()
    preview = {"local_date": "2026-09-25", "historical_mapping_quality": "unverified historical mapping",
        "allocations": {"hp": {"meter_kwh": 1, "categories": {"unknown": 1}, "residual_kwh": 0,
        "source_provenance": {"energy_entity_id": "sensor.hp", "meter_coverage_seconds": 3600},
        "authoritative_counter_source": "recorder_states_partial"}}, "site": {}}
    coordinator.async_preview_historical_energy = AsyncMock(return_value=preview)
    async def start():
        coordinator._recovery_preview_key = ("2026-09-25", "2026-09-25", True, False, coordinator._electricity_recovery_fingerprint())
        return (await coordinator.async_start_electricity_recovery(date(2026, 9, 25), date(2026, 9, 25),
            evidence_only=True, attest_current_mapping=False, preview_confirmed=True))["job_id"]
    first = await start()
    await coordinator._async_run_electricity_recovery(first)
    assert db.electricity_recovery_job(first)["status"] == "completed"
    second = await start()
    await coordinator._async_run_electricity_recovery(second)
    assert '"inserted":0' in db.electricity_recovery_job(second)["details_json"]
    third = await start()
    async def cancel_during_preview(*args, **kwargs):
        db.cancel_electricity_recovery_job(third)
        return preview
    coordinator.async_preview_historical_energy = AsyncMock(side_effect=cancel_during_preview)
    await coordinator._async_run_electricity_recovery(third)
    assert db.electricity_recovery_job(third)["status"] == "cancelled"
    with db.connect() as connection:
        assert connection.execute("SELECT count(*) FROM electricity_recovery_evidence").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_monthly_report_includes_only_nonoverlapping_provisional_evidence(hass, tmp_path, monkeypatch):
    import csv
    import inspect
    import io
    from custom_components.home_energy_intelligence import websocket
    from custom_components.home_energy_intelligence.websocket import _monthly_report_payload
    db = EnergyDatabase(tmp_path / "monthly.sqlite3")
    db.initialize()
    record = {**evidence("2026-09-25"), "historical_mapping_quality": "unverified historical mapping",
        "counter_source": "recorder_states_partial", "meter_coverage_seconds": 3600,
        "source_provenance": {"energy_entity_id": "sensor.hp"}}
    job = db.create_electricity_recovery_job("2026-09-25", "2026-09-25", "Europe/Ljubljana", "evidence_only")
    db.commit_electricity_recovery_day(job, "2026-09-25", [record])
    coordinator = SimpleNamespace(database=db, timezone="Europe/Ljubljana", config={
        "equipment": json.dumps([{"equipment_id": "hp", "display_name": "HP", "kind": "heat_pump", "energy_entity": "sensor.hp"}])})
    result = await _monthly_report_payload(hass, coordinator, "2026-09", 1)
    assert result["provisional_recovered"]["rows"][0]["categories"] == {"unknown": 1}
    assert result["rows"][0]["recovered_ecodan_unknown_kwh"] == 1
    assert result["rows"][0]["recovered_ecodan_meter_kwh"] == 1
    assert "recovered_ecodan_dhw_kwh" not in result["rows"][0]
    assert result["rows"][0]["recovered_metric_evidence"]["recovered_ecodan_unknown_kwh"][0]["source_entity_id"] == "sensor.hp"
    monkeypatch.setattr(websocket, "_coordinator", lambda _hass: coordinator)
    connection = SimpleNamespace(send_result=Mock(), send_error=Mock())
    await inspect.unwrap(websocket.ws_monthly_report_csv)(hass, connection, {"id": 1, "end_month": "2026-09", "count": 1, "metric_ids": ["recovered_ecodan_unknown_kwh"]})
    csv_text = connection.send_result.call_args.args[1]["csv"]
    csv_row = next(csv.DictReader(io.StringIO(csv_text)))
    assert float(csv_row["recovered_ecodan_unknown_kwh (kWh)"]) == result["rows"][0]["recovered_ecodan_unknown_kwh"]
    assert "sensor.hp" in csv_row["recovered_ecodan_unknown_kwh_evidence"]
    with db.connect() as connection:
        connection.execute("""INSERT INTO hourly_records (interval_start_utc,interval_end_utc,local_date,timezone,payload_json,checksum,quality,updated_at)
            VALUES (?,?,?,?,?,?,?,?)""", ("2026-09-25T00:00:00+00:00", "2026-09-25T01:00:00+00:00", "2026-09-25",
            "Europe/Ljubljana", json.dumps({"allocations": {"hp": {"meter_kwh": 0}}}), "saved", "partial", "now"))
    result = await _monthly_report_payload(hass, coordinator, "2026-09", 1)
    assert result["provisional_recovered"]["rows"] == []
    assert result["provisional_recovered"]["skipped"][0]["reason"] == "existing_electrical_record"
    assert "recovered_ecodan_unknown_kwh" not in result["rows"][0]
    assert result["rows"][0]["recovered_excluded"][0]["local_date"] == "2026-09-25"


def test_recovered_monthly_metrics_remain_separate_and_keep_missing_values():
    from custom_components.home_energy_intelligence.electricity_recovery import add_recovered_presentation_metrics
    report = {"rows": [{"month": "2026-09", "primary_heating_cop": None, "ecodan_heating_electricity_kwh": 3}]}
    common = {"month": "2026-09", "days": 2, "coverage_seconds": 3600, "expected_seconds": 86400,
              "sources": ["recorder"], "quality": "provisional partial"}
    summary = {"rows": [
        {**common, "source_key": "equipment.hp", "source_entity_id": "sensor.hp", "meter_kwh": 12,
         "categories": {"heating_assumed": 4, "unknown": 8}},
        {**common, "source_key": "equipment.ac", "source_entity_id": "sensor.ac", "meter_kwh": 6,
         "categories": {"heating_selected": 2, "cooling_selected": 4}}], "skipped": []}
    equipment = [{"equipment_id": "hp", "kind": "heat_pump"}, {"equipment_id": "ac", "kind": "air_conditioner"}]
    add_recovered_presentation_metrics(report, summary, equipment)
    row = report["rows"][0]
    assert [row[k] for k in ("recovered_ecodan_meter_kwh", "recovered_ecodan_assumed_heating_kwh", "recovered_ecodan_unknown_kwh", "recovered_ac_selected_heating_kwh", "recovered_ac_selected_cooling_kwh")] == [12, 4, 8, 2, 4]
    assert "recovered_ecodan_dhw_kwh" not in row
    assert row["primary_heating_cop"] is None and row["ecodan_heating_electricity_kwh"] == 3
    other = {"rows": [{"month": "2026-09"}]}
    add_recovered_presentation_metrics(other, summary, equipment + [{"equipment_id": "hp2", "kind": "heat_pump"}])
    assert "recovered_ecodan_meter_kwh" not in other["rows"][0]
    assert other["rows"][0]["recovered_ac_selected_heating_kwh"] == 2


@pytest.mark.asyncio
async def test_recovered_ac_monthly_csv_and_source_day_exclusion(hass, tmp_path, monkeypatch):
    import csv
    import inspect
    import io
    from custom_components.home_energy_intelligence import websocket

    db = EnergyDatabase(tmp_path / "ac-monthly.sqlite3")
    db.initialize()
    job = db.create_electricity_recovery_job("2026-09-25", "2026-09-25", "Europe/Ljubljana", "evidence_only")
    records = [{**evidence("2026-09-25", value=value), "source_key": f"equipment.{name}",
                "source_entity_id": f"sensor.{name}", "categories": {"unknown": value},
                "source_provenance": {"energy_entity_id": f"sensor.{name}"},
                "historical_mapping_quality": "unverified historical mapping",
                "meter_coverage_seconds": 3600, "counter_source": "recorder_states_partial"}
               for name, value in (("ac1", 2), ("ac2", 3))]
    db.commit_electricity_recovery_day(job, "2026-09-25", records)
    equipment = [{"equipment_id": name, "display_name": name, "kind": "air_conditioner", "energy_entity": f"sensor.{name}"} for name in ("ac1", "ac2", "missing")]
    coordinator = SimpleNamespace(database=db, timezone="Europe/Ljubljana", config={"equipment": json.dumps(equipment)})
    report = await websocket._monthly_report_payload(hass, coordinator, "2026-09", 1)
    row = report["rows"][0]
    assert row["recovered_ac_meter_kwh"] == row["recovered_ac_unknown_kwh"] == 5
    assert len(row["recovered_metric_evidence"]["recovered_ac_meter_kwh"]) == 2
    monkeypatch.setattr(websocket, "_coordinator", lambda _hass: coordinator)
    connection = SimpleNamespace(send_result=Mock(), send_error=Mock())
    await inspect.unwrap(websocket.ws_monthly_report_csv)(hass, connection, {"id": 1, "end_month": "2026-09", "count": 1, "metric_ids": ["recovered_ac_meter_kwh", "recovered_ac_unknown_kwh"]})
    csv_row = next(csv.DictReader(io.StringIO(connection.send_result.call_args.args[1]["csv"])))
    assert float(csv_row["recovered_ac_meter_kwh (kWh)"]) == row["recovered_ac_meter_kwh"]
    assert float(csv_row["recovered_ac_unknown_kwh (kWh)"]) == row["recovered_ac_unknown_kwh"]
    with db.connect() as connection:
        connection.execute("""INSERT INTO hourly_records (interval_start_utc,interval_end_utc,local_date,timezone,payload_json,checksum,quality,updated_at)
            VALUES (?,?,?,?,?,?,?,?)""", ("2026-09-25T00:00:00+00:00", "2026-09-25T01:00:00+00:00", "2026-09-25", "Europe/Ljubljana", json.dumps({"allocations": {"ac1": {"meter_kwh": 0}}}), "saved", "partial", "now"))
    row = (await websocket._monthly_report_payload(hass, coordinator, "2026-09", 1))["rows"][0]
    assert row["recovered_ac_meter_kwh"] == row["recovered_ac_unknown_kwh"] == 3
    assert any(item["source_key"] == "equipment.ac1" for item in row["recovered_excluded"])

    empty = {"rows": [{"month": "2026-09"}]}
    from custom_components.home_energy_intelligence.electricity_recovery import add_recovered_presentation_metrics
    add_recovered_presentation_metrics(empty, {"rows": [], "skipped": []}, equipment)
    assert "recovered_ac_meter_kwh" not in empty["rows"][0]
    assert "recovered_ac_unknown_kwh" not in empty["rows"][0]


@pytest.mark.asyncio
async def test_seven_overlapping_september_sources_remain_nonadditive_references(hass, tmp_path, monkeypatch):
    import csv
    import inspect
    import io
    from custom_components.home_energy_intelligence import websocket

    db = EnergyDatabase(tmp_path / "reference.sqlite3")
    db.initialize()
    job = db.create_electricity_recovery_job("2026-09-25", "2026-09-25", "Europe/Ljubljana", "evidence_only")
    sources = [("equipment.hp", 12.073), ("equipment.ac1", 1.4), ("equipment.ac2", .8), ("equipment.ac3", .541), ("site.pv", 3), ("site.import", 4), ("site.export", 1)]
    records = [{**evidence("2026-09-25", value=value), "source_key": key,
                "source_entity_id": f"sensor.{key.split('.')[1]}",
                "source_provenance": {"energy_entity_id": f"sensor.{key.split('.')[1]}"},
                "historical_mapping_quality": "unverified historical mapping",
                "meter_coverage_seconds": 3600, "counter_source": "recorder_states_partial"}
               for key, value in sources]
    db.commit_electricity_recovery_day(job, "2026-09-25", records)
    payload = {"allocations": {name: {"meter_kwh": 0} for name in ("hp", "ac1", "ac2", "ac3")}, "pv_kwh": 0, "import_kwh": 0, "export_kwh": 0}
    with db.connect() as connection:
        connection.execute("""INSERT INTO hourly_records (interval_start_utc,interval_end_utc,local_date,timezone,payload_json,checksum,quality,updated_at)
            VALUES (?,?,?,?,?,?,?,?)""", ("2026-09-25T00:00:00+00:00", "2026-09-25T01:00:00+00:00", "2026-09-25", "Europe/Ljubljana", json.dumps(payload), "saved", "partial", "now"))
    equipment = [{"equipment_id": name, "display_name": name, "kind": "heat_pump" if name == "hp" else "air_conditioner", "energy_entity": f"sensor.{name}"} for name in ("hp", "ac1", "ac2", "ac3")]
    coordinator = SimpleNamespace(database=db, timezone="Europe/Ljubljana", config={"equipment": json.dumps(equipment)}, _site_sources=lambda: {name: f"sensor.{name}" for name in ("pv", "import", "export")})
    report = await websocket._monthly_report_payload(hass, coordinator, "2026-09", 1)
    row = report["rows"][0]
    assert row["recovered_reference_ecodan_meter_kwh"] == pytest.approx(12.073)
    assert row["recovered_reference_ac_meter_kwh"] == pytest.approx(2.741)
    assert row["recovered_reference_pv_kwh"] == 3
    assert row["recovered_reference_import_kwh"] == 4
    assert row["recovered_reference_export_kwh"] == 1
    assert "recovered_ecodan_meter_kwh" not in row and "recovered_ac_meter_kwh" not in row
    assert len(row["recovered_excluded"]) == 7
    assert all(item["reason"] == "existing_electrical_record" for item in row["recovered_excluded"])
    assert len(report["provisional_recovered"]["reference_rows"]) == 7
    assert row["recovered_metric_evidence"]["recovered_reference_ac_meter_kwh"][0]["coverage_seconds"] == 3600
    monkeypatch.setattr(websocket, "_coordinator", lambda _hass: coordinator)
    connection = SimpleNamespace(send_result=Mock(), send_error=Mock())
    await inspect.unwrap(websocket.ws_monthly_report_csv)(hass, connection, {"id": 1, "end_month": "2026-09", "count": 13, "metric_ids": ["recovered_reference_ecodan_meter_kwh", "recovered_reference_ac_meter_kwh", "recovered_reference_pv_kwh"]})
    csv_rows = list(csv.DictReader(io.StringIO(connection.send_result.call_args.args[1]["csv"])))
    assert len(csv_rows) == 13
    csv_row = next(item for item in csv_rows if item["month"] == "2026-09")
    assert float(csv_row["recovered_reference_ecodan_meter_kwh (kWh)"]) == pytest.approx(12.073)
    assert float(csv_row["recovered_reference_ac_meter_kwh (kWh)"]) == pytest.approx(2.741)
    assert float(csv_row["recovered_reference_pv_kwh (kWh)"]) == 3
    assert "sensor.pv" in csv_row["recovered_reference_pv_kwh_evidence"]
    assert "existing_electrical_record" in csv_row["recovered_excluded"]
    monkeypatch.setattr(db, "electricity_recovery_evidence_range", lambda *_: {"records": [], "ambiguous": []})
    baseline = (await websocket._monthly_report_payload(hass, coordinator, "2026-09", 1))["rows"][0]
    assert {key: value for key, value in row.items() if not key.startswith("recovered_")} == {key: value for key, value in baseline.items() if not key.startswith("recovered_")}
    with db.connect() as connection:
        assert connection.execute("SELECT count(*) FROM electricity_recovery_evidence").fetchone()[0] == 7
        assert connection.execute("SELECT checksum FROM hourly_records").fetchone()[0] == "saved"


@pytest.mark.parametrize("handler_name", ["ws_electricity_recovery_start", "ws_electricity_recovery_status", "ws_electricity_recovery_cancel"])
def test_recovery_commands_require_admin(handler_name):
    from homeassistant.exceptions import Unauthorized
    from custom_components.home_energy_intelligence import websocket
    handler = getattr(websocket, handler_name)
    with pytest.raises(Unauthorized):
        handler(None, SimpleNamespace(user=SimpleNamespace(is_admin=False)), {"id": 1})

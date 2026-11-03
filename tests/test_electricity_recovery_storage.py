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


@pytest.mark.parametrize("handler_name", ["ws_electricity_recovery_start", "ws_electricity_recovery_status", "ws_electricity_recovery_cancel"])
def test_recovery_commands_require_admin(handler_name):
    from homeassistant.exceptions import Unauthorized
    from custom_components.home_energy_intelligence import websocket
    handler = getattr(websocket, handler_name)
    with pytest.raises(Unauthorized):
        handler(None, SimpleNamespace(user=SimpleNamespace(is_admin=False)), {"id": 1})

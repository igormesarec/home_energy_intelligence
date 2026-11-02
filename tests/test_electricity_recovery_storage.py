from copy import deepcopy
from datetime import date

import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from custom_components.home_energy_intelligence.database import EnergyDatabase, SCHEMA_VERSION
from custom_components.home_energy_intelligence.timeutils import local_day_bounds
from custom_components.home_energy_intelligence.electricity_recovery import prepare_recovery_day


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

from datetime import UTC, datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import sqlite3
from threading import Barrier
import time
import tracemalloc

import pytest

from custom_components.home_energy_intelligence.analytics import heating_response_profile
from custom_components.home_energy_intelligence.database import EnergyDatabase, protected_export_path
from custom_components.home_energy_intelligence.models import DailyRecord


def logical_daily_checksum(db, local_date):
    payload = db.daily(local_date)
    payload.pop("revision", None)
    payload.pop("updated_at", None)
    serialized = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(serialized.encode()).hexdigest()


def make_v3_inline_database(db, daily_payload):
    """Convert a test v4 database to the inline temperature shape used by v3."""
    serialized = json.dumps(
        daily_payload, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    checksum = hashlib.sha256(serialized.encode()).hexdigest()
    with db.connect() as connection:
        connection.execute("DELETE FROM temperature_summaries")
        connection.execute("DELETE FROM hourly_temperature_rejections")
        connection.execute(
            "UPDATE daily_records SET payload_json=?,checksum=? WHERE local_date=?",
            (serialized, checksum, daily_payload["local_date"]),
        )
        connection.execute("UPDATE schema_version SET version=3")


def record(value=10.0):
    return DailyRecord(
        "2026-10-25", "Europe/Ljubljana", "2026-10-24T22:00:00+00:00",
        "2026-10-25T23:00:00+00:00", 90000, value, 5, 7, 2, 6, "good",
        allocations={"ecodan": {"categories": {"confirmed_heating": 4},
                                  "coverage_percent": 100, "quality": "good"}},
    )


def test_schema_and_idempotent_revision(tmp_path):
    db = EnergyDatabase(tmp_path / "energy.sqlite3")
    db.initialize()
    assert db.upsert_daily(record()) is True
    assert db.upsert_daily(record()) is False
    assert db.upsert_daily(record(11)) is True
    latest = db.latest()
    assert latest["revision"] == 2
    assert latest["household_kwh"] == 11


def test_corrupt_database_is_preserved_and_recovered(tmp_path):
    path = tmp_path / "energy.sqlite3"
    path.write_bytes(b"not a sqlite database")
    db = EnergyDatabase(path)
    recovered = db.initialize()
    assert recovered is not None and recovered.exists()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_v4_migration_normalizes_large_temperature_audit_losslessly(tmp_path):
    db = EnergyDatabase(tmp_path / "energy.sqlite3")
    db.initialize()
    item = record(42.631)
    large_value = "x" * 1_000_000
    item.temperatures = {"outdoor": {
        "mean": 5.0, "minimum": -2.0, "maximum": 9.0,
        "coverage_seconds": 89900, "period_seconds": 90000,
        "coverage_percent": 99.888, "quality": "filtered", "rejected_count": 1,
        "rejections": [{
            "timestamp": "2026-10-25T01:00:00+00:00", "value": large_value,
            "reason": "invalid_sentinel", "duration_seconds": 100,
            "source": "recorder",
        }],
        "source_provenance": {"entity_id": "sensor.outdoor", "source_id": "outdoor"},
    }, "indoor-unit": {
        "mean": None, "minimum": None, "maximum": None,
        "coverage_seconds": 0, "period_seconds": 90000,
        "coverage_percent": 0, "quality": "missing", "rejected_count": 1,
        "rejected_reasons": ["incompatible_temperature_unit:72"],
        "rejections": [],
        "source_provenance": {"entity_id": "sensor.indoor", "source_id": "indoor-unit"},
    }}
    assert db.upsert_daily(item)
    logical = db.daily(item.local_date)
    logical.pop("revision")
    logical.pop("updated_at")
    with db.connect() as connection:
        revision_rows = connection.execute("SELECT COUNT(*) FROM revisions").fetchone()[0]
        rejection_rows = connection.execute(
            "SELECT COUNT(*) FROM rejected_readings"
        ).fetchone()[0]
    make_v3_inline_database(db, logical)

    db.initialize()
    migrated = db.daily(item.local_date)
    migrated.pop("revision")
    migrated.pop("updated_at")
    assert migrated == logical
    with db.connect() as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == 5
        raw = connection.execute("SELECT payload_json FROM daily_records").fetchone()[0]
        assert "temperatures" not in json.loads(raw)
        assert connection.execute(
            "SELECT COUNT(*) FROM temperature_summaries"
        ).fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM revisions").fetchone()[0] == revision_rows
        assert connection.execute(
            "SELECT COUNT(*) FROM rejected_readings"
        ).fetchone()[0] == rejection_rows
    logical_bytes = len(json.dumps(logical, separators=(",", ":")).encode())
    # Startup intentionally does not VACUUM.  The live DB plus WAL may briefly
    # retain old pages, but migration remains bounded and does not duplicate the
    # logical payload without limit.
    assert db.size_bytes() < logical_bytes * 3
    before = db.size_bytes()
    db.initialize()
    assert db.daily(item.local_date)["temperatures"] == logical["temperatures"]
    assert db.size_bytes() == before


@pytest.mark.parametrize("legacy_version", [1, 2, 3])
def test_every_supported_legacy_schema_upgrades_losslessly_and_idempotently(
    tmp_path, legacy_version,
):
    db = EnergyDatabase(tmp_path / f"v{legacy_version}.sqlite3")
    db.initialize()
    item = record()
    item.temperatures = {"outdoor": {
        "mean": 6.5, "coverage_seconds": 90000, "period_seconds": 90000,
        "coverage_percent": 100, "quality": "good", "rejections": [],
        "source_provenance": {"entity_id": "sensor.outdoor"},
    }}
    db.upsert_daily(item)
    logical = db.daily(item.local_date)
    logical.pop("revision")
    logical.pop("updated_at")
    make_v3_inline_database(db, logical)
    with db.connect() as connection:
        connection.execute("DROP TABLE hourly_temperature_rejections")
        connection.execute("DROP TABLE temperature_summaries")
        if legacy_version < 3:
            connection.execute("DROP TABLE hourly_records")
        if legacy_version == 1:
            connection.execute("ALTER TABLE rejected_readings DROP COLUMN source")
            connection.execute("ALTER TABLE rejected_readings DROP COLUMN duration_seconds")
        connection.execute("UPDATE schema_version SET version=?", (legacy_version,))
    db.initialize()
    assert db.daily(item.local_date)["temperatures"]["outdoor"]["mean"] == 6.5
    with db.connect() as connection:
            assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == 5
    before = db.size_bytes()
    db.initialize()
    assert db.size_bytes() == before


def test_v4_migration_streams_thousands_of_daily_and_hourly_rows(tmp_path):
    db = EnergyDatabase(tmp_path / "large-v3.sqlite3")
    db.initialize()
    base = datetime(2020, 1, 1, tzinfo=UTC)
    with db.connect() as connection:
        now = datetime.now(UTC).isoformat()
        for index in range(1200):
            stamp = base + timedelta(days=index)
            local_date = stamp.date().isoformat()
            payload = {
                "local_date": local_date, "timezone": "UTC",
                "period_start_utc": stamp.isoformat(),
                "period_end_utc": (stamp + timedelta(days=1)).isoformat(),
                "day_seconds": 86400, "household_kwh": 1.0,
                "temperatures": {"outdoor": {
                    "mean": 5.0, "coverage_seconds": 86400,
                    "period_seconds": 86400, "coverage_percent": 100,
                    "quality": "good", "rejections": [],
                    "source_provenance": {"entity_id": "sensor.outdoor"},
                }}, "allocations": {}, "provenance": {}, "quality": "good",
            }
            serialized = json.dumps(payload, separators=(",", ":"), sort_keys=True)
            connection.execute(
                "INSERT INTO daily_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (local_date, "UTC", stamp.isoformat(),
                 (stamp + timedelta(days=1)).isoformat(), 86400, 1.0,
                 None, None, None, None, "good", serialized,
                 hashlib.sha256(serialized.encode()).hexdigest(), 1, now),
            )
            hour = stamp + timedelta(hours=1)
            hour_payload = {
                **payload, "period_start_utc": hour.isoformat(),
                "period_end_utc": (hour + timedelta(hours=1)).isoformat(),
                "day_seconds": 3600,
            }
            hour_serialized = json.dumps(
                hour_payload, separators=(",", ":"), sort_keys=True
            )
            connection.execute(
                "INSERT INTO hourly_records VALUES (?,?,?,?,?,?,?,?)",
                (hour.isoformat(), (hour + timedelta(hours=1)).isoformat(),
                 local_date, "UTC", hour_serialized,
                 hashlib.sha256(hour_serialized.encode()).hexdigest(), "good", now),
            )
        connection.execute("DROP TABLE hourly_temperature_rejections")
        connection.execute("DROP TABLE temperature_summaries")
        connection.execute("UPDATE schema_version SET version=3")
    tracemalloc.start()
    db.initialize()
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < 32 * 1024 * 1024
    with db.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM temperature_summaries"
        ).fetchone()[0] == 2400
        assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == 5


def test_v4_migration_failure_rolls_back_data_and_version(tmp_path):
    db = EnergyDatabase(tmp_path / "energy.sqlite3")
    db.initialize()
    item = record(42.631)
    item.temperatures = {"outdoor": {
        "mean": 5.0, "coverage_seconds": 90000, "period_seconds": 90000,
        "coverage_percent": 100, "quality": "good", "rejections": [],
        "source_provenance": {"entity_id": "sensor.outdoor", "source_id": "outdoor"},
    }}
    db.upsert_daily(item)
    logical = db.daily(item.local_date)
    logical.pop("revision")
    logical.pop("updated_at")
    make_v3_inline_database(db, logical)
    with db.connect() as connection:
        connection.execute(
            """CREATE TRIGGER fail_v4 BEFORE INSERT ON temperature_summaries
            BEGIN SELECT RAISE(ABORT, 'injected migration failure'); END"""
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected migration failure"):
        db.initialize()
    with db.connect() as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == 3
        raw = json.loads(connection.execute(
            "SELECT payload_json FROM daily_records"
        ).fetchone()[0])
        assert raw["temperatures"]["outdoor"]["mean"] == 5.0
        assert connection.execute(
            "SELECT COUNT(*) FROM temperature_summaries"
        ).fetchone()[0] == 0


@pytest.mark.parametrize("legacy_version", [1, 2, 3])
def test_v4_schema_creation_failure_rolls_back_new_tables_and_version(
    tmp_path, monkeypatch, legacy_version,
):
    import custom_components.home_energy_intelligence.database as database_module

    db = EnergyDatabase(tmp_path / f"failed-v{legacy_version}.sqlite3")
    db.initialize()
    with db.connect() as connection:
        connection.execute("DROP TABLE hourly_temperature_rejections")
        connection.execute("DROP TABLE temperature_summaries")
        if legacy_version < 3:
            connection.execute("DROP TABLE hourly_records")
        if legacy_version == 1:
            connection.execute("ALTER TABLE rejected_readings DROP COLUMN source")
            connection.execute("ALTER TABLE rejected_readings DROP COLUMN duration_seconds")
        connection.execute("UPDATE schema_version SET version=?", (legacy_version,))
    monkeypatch.setattr(
        database_module, "MIGRATION_4",
        database_module.MIGRATION_4 + "\nCREATE TABL deliberately_invalid;",
    )
    with pytest.raises(sqlite3.OperationalError):
        db.initialize()
    with db.connect() as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == legacy_version
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
            "AND name IN ('temperature_summaries','hourly_temperature_rejections')"
        ).fetchone()[0] == 0
        if legacy_version < 3:
            assert connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='hourly_records'"
            ).fetchone()[0] == 0


def test_v4_failure_before_version_bump_rolls_back_payload_conversion(tmp_path):
    db = EnergyDatabase(tmp_path / "energy.sqlite3")
    db.initialize()
    item = record()
    item.temperatures = {"outdoor": {
        "mean": 5.0, "coverage_seconds": 90000, "period_seconds": 90000,
        "coverage_percent": 100, "quality": "good", "rejections": [],
        "source_provenance": {"entity_id": "sensor.outdoor"},
    }}
    db.upsert_daily(item)
    logical = db.daily(item.local_date)
    logical.pop("revision")
    logical.pop("updated_at")
    make_v3_inline_database(db, logical)
    with db.connect() as connection:
        connection.execute(
            """CREATE TRIGGER fail_version BEFORE UPDATE OF version ON schema_version
            BEGIN SELECT RAISE(ABORT, 'version bump failed'); END"""
        )
    with pytest.raises(sqlite3.IntegrityError, match="version bump failed"):
        db.initialize()
    with db.connect() as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == 3
        assert "temperatures" in json.loads(connection.execute(
            "SELECT payload_json FROM daily_records"
        ).fetchone()[0])
        assert connection.execute(
            "SELECT COUNT(*) FROM temperature_summaries"
        ).fetchone()[0] == 0


def test_v4_disk_full_failure_is_retryable_and_preserves_v3(tmp_path, monkeypatch):
    import custom_components.home_energy_intelligence.database as database_module

    db = EnergyDatabase(tmp_path / "energy.sqlite3")
    db.initialize()
    item = record()
    item.temperatures = {"outdoor": {
        "mean": 5.0, "coverage_seconds": 90000, "period_seconds": 90000,
        "coverage_percent": 100, "quality": "good", "rejections": [],
        "source_provenance": {"entity_id": "sensor.outdoor"},
    }}
    db.upsert_daily(item)
    logical = db.daily(item.local_date)
    logical.pop("revision")
    logical.pop("updated_at")
    make_v3_inline_database(db, logical)
    original = database_module._store_normalized_temperatures
    monkeypatch.setattr(
        database_module, "_store_normalized_temperatures",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            sqlite3.OperationalError("database or disk is full")
        ),
    )
    with pytest.raises(sqlite3.OperationalError, match="disk is full"):
        db.initialize()
    with db.connect() as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == 3
        assert "temperatures" in json.loads(connection.execute(
            "SELECT payload_json FROM daily_records"
        ).fetchone()[0])
    monkeypatch.setattr(database_module, "_store_normalized_temperatures", original)
    db.initialize()
    assert db.daily(item.local_date)["temperatures"]["outdoor"]["mean"] == 5.0


def test_locked_database_is_not_renamed_as_corrupt(tmp_path, monkeypatch):
    path = tmp_path / "energy.sqlite3"
    db = EnergyDatabase(path)
    db.initialize()
    with db.connect() as connection:
        connection.execute("UPDATE schema_version SET version=3")
    monkeypatch.setattr(EnergyDatabase, "CONNECT_TIMEOUT_SECONDS", 0.01)
    blocker = sqlite3.connect(path)
    blocker.execute("PRAGMA journal_mode=WAL")
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            db.initialize()
        assert path.exists()
        assert not list(tmp_path.glob("*.corrupt-*.sqlite3"))
    finally:
        blocker.rollback()
        blocker.close()


def test_storage_status_discloses_safe_maintenance_policy(tmp_path):
    db = EnergyDatabase(tmp_path / "energy.sqlite3")
    db.initialize()
    status = db.storage_status()
    assert status["schema_version"] == 5
    assert status["migration_required"] is False
    assert status["startup_vacuum_enabled"] is False
    assert status["backup_method"] == "protected_sqlite_online_backup"


def test_temperature_repair_immediately_qualifies_response_without_energy_rebuild(tmp_path):
    db = EnergyDatabase(tmp_path / "energy.sqlite3")
    db.initialize()
    item = record(42.631)
    item.quality = "partial"
    item.allocations = {"hp": {
        "meter_kwh": 4.0, "categories": {"confirmed_heating": 4.0},
        "covered_seconds": 90000, "period_seconds": 90000,
        "coverage_percent": 100, "quality": "good",
    }}
    db.upsert_daily(item)
    equipment = [{
        "equipment_id": "hp", "display_name": "Heat pump",
        "kind": "heat_pump", "enabled": True,
    }]
    before = heating_response_profile(
        [db.daily(item.local_date)], equipment, outdoor_source_id="outdoor",
        outdoor_source_is_measured_air=True, floor_area_m2=180,
    )["points"][0]
    assert before["status"] == "excluded"
    assert "outdoor_temperature_unavailable" in before["exclusion_reasons"]
    summary = {
        "mean": 5.0, "minimum": 2.0, "maximum": 8.0,
        "coverage_seconds": 90000, "period_seconds": 90000,
        "coverage_percent": 100, "quality": "good", "rejections": [],
        "period_start_utc": item.period_start_utc,
        "period_end_utc": item.period_end_utc,
        "source_provenance": {"entity_id": "sensor.outdoor", "source_id": "outdoor"},
    }
    assert db.merge_daily_temperatures(
        item.local_date, {"outdoor": summary}, {"source": "test"}
    )
    after = heating_response_profile(
        [db.daily(item.local_date)], equipment, outdoor_source_id="outdoor",
        outdoor_source_is_measured_air=True, floor_area_m2=180,
    )["points"][0]
    assert after["status"] == "validated"
    assert after["heating_electricity_kwh"] == 4.0


def test_compact_growth_for_ten_years(tmp_path):
    db = EnergyDatabase(tmp_path / "energy.sqlite3")
    db.initialize()
    with db.connect() as connection:
        now = datetime.now(UTC).isoformat()
        for index in range(3650):
            day = f"{2017 + index // 365:04d}-{(index % 365) // 31 + 1:02d}-{(index % 31) + 1:02d}"
            connection.execute(
                "INSERT OR REPLACE INTO daily_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (day, "Europe/Ljubljana", now, now, 86400, 10, 5, 7, 2, 6,
                 "good", '{"quality":"good"}', str(index), 1, now),
            )
    assert db.size_bytes() < 5_000_000


def test_job_metadata_and_rejected_readings_are_persisted(tmp_path):
    db = EnergyDatabase(tmp_path / "energy.sqlite3")
    db.initialize()
    item = record()
    item.temperatures = {"outdoor": {"rejected_reasons": ["out_of_range:155"]}}
    db.upsert_daily(item)
    job_id = db.start_job("daily", {"date": item.local_date})
    db.finish_job(job_id, "success", {"changed": True})
    with db.connect() as connection:
        rejected = connection.execute("SELECT value,reason FROM rejected_readings").fetchone()
        job = connection.execute("SELECT status,finished_at FROM job_runs WHERE id=?", (job_id,)).fetchone()
    assert tuple(rejected) == ("155", "out_of_range")
    assert job["status"] == "success" and job["finished_at"]


def test_checkpoint_upsert_is_idempotent_and_survives_daily_rebuild(tmp_path):
    db = EnergyDatabase(tmp_path / "energy.sqlite3")
    db.initialize()
    start = datetime(2026, 10, 25, tzinfo=UTC)
    end = datetime(2026, 10, 25, 6, tzinfo=UTC)
    payload = {"meter_kwh": 5, "allocations_kwh": {"heating_assumed": 5}, "quality": "uncertain"}
    assert db.upsert_checkpoint(start, end, "2026-10-25", "hp", payload)
    assert not db.upsert_checkpoint(start, end, "2026-10-25", "hp", payload)
    db.upsert_daily(record())
    checkpoints = db.checkpoints_for_day("2026-10-25")
    assert checkpoints["hp"][0]["meter_kwh"] == 5


def test_checkpoint_completeness_is_per_enabled_equipment(tmp_path):
    db = EnergyDatabase(tmp_path / "energy.sqlite3")
    db.initialize()
    start = datetime(2026, 10, 25, tzinfo=UTC)
    end = datetime(2026, 10, 25, 6, tzinfo=UTC)
    db.upsert_checkpoint(start, end, "2026-10-25", "hp-a", {"quality": "good"})
    assert db.missing_checkpoint_devices(start, end, {"hp-a", "hp-b", "ac-a"}) == {
        "hp-b", "ac-a",
    }
    assert db.checkpoint_exists(start, end, "hp-a")
    assert not db.checkpoint_exists(start, end, "hp-b")


def test_mapping_revisions_preserve_source_replacement_history(tmp_path):
    db = EnergyDatabase(tmp_path / "energy.sqlite3")
    db.initialize()
    assert db.store_mapping_revision({"effective_at": "2026-01-01T00:00:00+00:00", "entity": "sensor.old"})
    assert not db.store_mapping_revision({"effective_at": "2026-01-01T00:00:00+00:00", "entity": "sensor.old"})
    assert db.store_mapping_revision({"effective_at": "2026-02-01T00:00:00+00:00", "entity": "sensor.new"})
    with db.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM mapping_revisions").fetchone()[0] == 2


def test_temperature_mapping_applicability_verifies_entity_attribute_role_and_interval(tmp_path):
    db = EnergyDatabase(tmp_path / "energy.sqlite3")
    db.initialize()
    old = {
        "effective_at": "2026-01-01T00:00:00+00:00", "equipment": [],
        "temperatures": [{
            "source_id": "outdoor", "entity_id": "sensor.outdoor_old",
            "attribute": None, "role": "outdoor", "enabled": True,
        }],
    }
    new = {
        "effective_at": "2026-02-01T00:00:00+00:00", "equipment": [],
        "temperatures": [{
            "source_id": "outdoor", "entity_id": "sensor.outdoor_new",
            "attribute": "temperature", "role": "outdoor", "enabled": True,
        }],
    }
    assert db.store_mapping_revision(old)
    assert db.store_mapping_revision(new)
    old_identity = {
        "entity_id": "sensor.outdoor_old", "attribute": None, "role": "outdoor",
    }
    valid = db.temperature_mapping_applicability(
        "outdoor", datetime(2026, 1, 15, tzinfo=UTC),
        datetime(2026, 1, 16, tzinfo=UTC), old_identity,
    )
    assert valid["eligible"] is True
    assert valid["mapping_effective_end"] == "2026-02-01T00:00:00+00:00"
    crossing = db.temperature_mapping_applicability(
        "outdoor", datetime(2026, 1, 31, tzinfo=UTC),
        datetime(2026, 2, 2, tzinfo=UTC), old_identity,
    )
    assert crossing["reason"] == "mapping_changes_within_interval"
    remapped = db.temperature_mapping_applicability(
        "outdoor", datetime(2026, 2, 2, tzinfo=UTC),
        datetime(2026, 2, 3, tzinfo=UTC), old_identity,
    )
    assert remapped["reason"] == "mapping_entity_id_changed"
    changed_attribute = db.temperature_mapping_applicability(
        "outdoor", datetime(2026, 2, 2, tzinfo=UTC),
        datetime(2026, 2, 3, tzinfo=UTC), {
            "entity_id": "sensor.outdoor_new", "attribute": None, "role": "outdoor",
        },
    )
    assert changed_attribute["reason"] == "mapping_attribute_changed"
    changed_role = db.temperature_mapping_applicability(
        "outdoor", datetime(2026, 2, 2, tzinfo=UTC),
        datetime(2026, 2, 3, tzinfo=UTC), {
            "entity_id": "sensor.outdoor_new", "attribute": "temperature", "role": "indoor",
        },
    )
    assert changed_role["reason"] == "mapping_role_changed"


def test_legacy_mapping_without_attribute_cannot_authorize_historical_repair(tmp_path):
    db = EnergyDatabase(tmp_path / "energy.sqlite3")
    db.initialize()
    db.store_mapping_revision({
        "effective_at": "2026-01-01T00:00:00+00:00", "equipment": [],
        "temperatures": [{
            "source_id": "outdoor", "entity_id": "sensor.outdoor",
            "role": "outdoor", "enabled": True,
        }],
    })
    result = db.temperature_mapping_applicability(
        "outdoor", datetime(2026, 1, 2, tzinfo=UTC),
        datetime(2026, 1, 3, tzinfo=UTC), {
            "entity_id": "sensor.outdoor", "attribute": None, "role": "outdoor",
        },
    )
    assert result["eligible"] is False
    assert result["reason"] == "historical_mapping_attribute_unknown"


def test_unverified_mapping_cannot_fill_absent_summary_or_change_energy(tmp_path):
    db = EnergyDatabase(tmp_path / "energy.sqlite3")
    db.initialize()
    item = record(42.631)
    db.upsert_daily(item)
    with db.connect() as connection:
        before = connection.execute(
            "SELECT payload_json,checksum,revision FROM daily_records WHERE local_date=?",
            (item.local_date,),
        ).fetchone()
        allocations_before = connection.execute(
            "SELECT COUNT(*) FROM allocations WHERE local_date=?", (item.local_date,)
        ).fetchone()[0]
    candidate = {
        "mean": 5.0, "coverage_seconds": 90000, "period_seconds": 90000,
        "coverage_percent": 100, "quality": "good", "rejections": [],
        "period_start_utc": item.period_start_utc,
        "period_end_utc": item.period_end_utc,
        "source_provenance": {
            "entity_id": "sensor.current", "attribute": None, "role": "outdoor",
            "mapping_effective_at": "2026-10-26T00:00:00+00:00",
        },
    }
    assert not db.merge_daily_temperatures(
        item.local_date, {"outdoor": candidate},
        {"mapping_applicability": {}},
    )
    with db.connect() as connection:
        after = connection.execute(
            "SELECT payload_json,checksum,revision FROM daily_records WHERE local_date=?",
            (item.local_date,),
        ).fetchone()
        allocations_after = connection.execute(
            "SELECT COUNT(*) FROM allocations WHERE local_date=?", (item.local_date,)
        ).fetchone()[0]
    assert tuple(after) == tuple(before)
    assert allocations_after == allocations_before


def test_realistic_ten_year_checkpoint_volume_remains_compact(tmp_path):
    db = EnergyDatabase(tmp_path / "energy.sqlite3")
    db.initialize()
    now = datetime.now(UTC).isoformat()
    payload = '{"meter_kwh":5.0,"quality":"good","allocations_kwh":{"heating":5.0}}'
    with db.connect() as connection:
        rows = []
        for day_index in range(3650):
            for cycle in range(4):
                for device in ("hp", "ac_1", "ac_2", "ac_3"):
                    start = f"{day_index:04d}-{cycle}"
                    rows.append((start, f"{start}-end", f"day-{day_index:04d}", device,
                                 payload, f"{day_index}-{cycle}-{device}", now))
        connection.executemany("INSERT INTO checkpoints VALUES (?,?,?,?,?,?,?)", rows)
    assert db.size_bytes() < 50_000_000


def test_protected_export_never_uses_www_and_rejects_path_injection(tmp_path):
    target = protected_export_path(tmp_path, "20261028-010203")
    assert ".storage" in target.parts and "www" not in target.parts
    import pytest
    with pytest.raises(ValueError):
        protected_export_path(tmp_path, "../../www/backup")


def test_detailed_temperature_rejection_audit_is_persisted(tmp_path):
    db = EnergyDatabase(tmp_path / "energy.sqlite3")
    db.initialize()
    item = record()
    item.temperatures = {"outdoor": {"rejections": [{
        "timestamp": "2026-10-25T01:00:00+00:00", "value": -55,
        "duration_seconds": 900, "reason": "invalid_sentinel", "source": "sensor.outdoor",
    }]}}
    db.upsert_daily(item)
    with db.connect() as connection:
        row = connection.execute(
            "SELECT observed_at,value,duration_seconds,reason,source FROM rejected_readings"
        ).fetchone()
    assert tuple(row) == ("2026-10-25T01:00:00+00:00", "-55", 900, "invalid_sentinel", "sensor.outdoor")


def test_hourly_records_are_idempotent_bounded_and_dst_safe(tmp_path):
    db = EnergyDatabase(tmp_path / "energy.sqlite3")
    db.initialize()
    start = datetime(2026, 10, 24, 22, tzinfo=UTC)
    for index in range(25):
        interval_start = start + timedelta(hours=index)
        payload = {
            "local_date": "2026-10-25", "timezone": "Europe/Ljubljana",
            "period_start_utc": interval_start.isoformat(),
            "period_end_utc": (interval_start + timedelta(hours=1)).isoformat(),
            "quality": "good", "day_seconds": 3600,
        }
        assert db.upsert_hourly(payload)
        assert not db.upsert_hourly(payload)
    rows = db.hourly_range(start, start + timedelta(hours=25))
    assert len(rows) == 25
    with db.connect() as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == 5


def _mapping(effective_at, entity_id="sensor.outdoor"):
    return {
        "effective_at": effective_at,
        "equipment": [],
        "temperatures": [{
            "source_id": "outdoor", "entity_id": entity_id,
            "attribute": None, "role": "outdoor", "enabled": True,
        }],
    }


def _temperature_summary(start, end, entity_id="sensor.outdoor"):
    return {
        "mean": 5.5, "minimum": 5.0, "maximum": 6.0,
        "coverage_seconds": (end - start).total_seconds(),
        "period_seconds": (end - start).total_seconds(),
        "coverage_percent": 100.0, "quality": "good", "rejected_count": 0,
        "period_start_utc": start.isoformat(), "period_end_utc": end.isoformat(),
        "source_provenance": {
            "source_id": "outdoor", "entity_id": entity_id,
            "attribute": None, "role": "outdoor",
            "mapping_effective_at": "2026-01-01T00:00:00+00:00",
        },
    }


def test_v5_temperature_history_exists_without_energy_parent_and_is_idempotent(tmp_path):
    db = EnergyDatabase(tmp_path / "energy.sqlite3")
    db.initialize()
    db.ensure_mapping_revision(_mapping("2026-01-01T00:00:00+00:00"))
    start = datetime(2026, 10, 28, tzinfo=UTC)
    hour_end = start + timedelta(hours=1)
    day_end = start + timedelta(days=1)
    hour_mapping = db.temperature_mapping_applicability(
        "outdoor", start, hour_end, {
            "source_id": "outdoor", "entity_id": "sensor.outdoor",
            "attribute": None, "role": "outdoor", "enabled": True,
        }, allow_historical_identity=True,
    )
    day_mapping = db.temperature_mapping_applicability(
        "outdoor", start, day_end, {
            "source_id": "outdoor", "entity_id": "sensor.outdoor",
            "attribute": None, "role": "outdoor", "enabled": True,
        }, allow_historical_identity=True,
    )
    assert db.upsert_temperature_history(
        "hourly", start, hour_end, "2026-10-28", "UTC", "outdoor",
        _temperature_summary(start, hour_end), hour_mapping, dry_run=True,
    ) == "inserted"
    assert db.temperature_history_counts(start, day_end, None) == {
        "hourly": 0, "daily": 0,
    }
    assert db.upsert_temperature_history(
        "hourly", start, hour_end, "2026-10-28", "UTC", "outdoor",
        _temperature_summary(start, hour_end), hour_mapping,
    ) == "inserted"
    assert db.upsert_temperature_history(
        "hourly", start, hour_end, "2026-10-28", "UTC", "outdoor",
        _temperature_summary(start, hour_end), hour_mapping,
    ) == "unchanged"
    assert db.upsert_temperature_history(
        "daily", start, day_end, "2026-10-28", "UTC", "outdoor",
        _temperature_summary(start, day_end), day_mapping,
    ) == "inserted"
    recovered = db.daily("2026-10-28")
    assert recovered["temperature_only"] is True
    assert recovered["household_kwh"] is None
    assert recovered["temperatures"]["outdoor"]["mean"] == 5.5
    page, total = db.history_range(
        start_date="2026-10-28", end_date="2026-10-28", limit=10,
    )
    assert total == 1 and page[0]["temperature_only"] is True
    assert db.temperature_history_counts(start, day_end, None) == {
        "hourly": 1, "daily": 1,
    }


def test_hourly_range_returns_independent_temperature_without_energy_parent(tmp_path):
    db = EnergyDatabase(tmp_path / "temperature-only-hour.sqlite3")
    db.initialize()
    start = datetime(2026, 10, 29, tzinfo=UTC)
    end = start + timedelta(hours=1)
    identity = {
        "source_id": "indoor", "entity_id": "sensor.indoor",
        "attribute": None, "role": "indoor", "enabled": True,
    }
    db.ensure_mapping_revision({
        "effective_at": (start - timedelta(days=1)).isoformat(),
        "temperatures": [identity],
    })
    applicability = db.temperature_mapping_applicability(
        "indoor", start, end, identity, allow_historical_identity=True,
    )
    assert applicability["eligible"], applicability
    assert db.upsert_temperature_history(
        "hourly", start, end, "2026-10-29", "Europe/Berlin", "indoor",
        {"mean": 23.0, "minimum": 22.5, "maximum": 23.2,
         "coverage_seconds": 3600, "period_seconds": 3600,
         "coverage_percent": 100, "quality": "good",
         "period_start_utc": start.isoformat(),
         "period_end_utc": end.isoformat(),
         "source_provenance": {
             **identity,
             "mapping_effective_at": applicability["mapping_effective_at"],
         }}, applicability,
    ) == "inserted"
    before_daily = db.history_period("2026-10-29", "2026-10-29")
    rows = db.hourly_range(start, end)
    after_daily = db.history_period("2026-10-29", "2026-10-29")
    assert len(rows) == 1 and rows[0]["temperature_only"] is True
    assert rows[0]["temperatures"]["indoor"]["mean"] == 23.0
    assert rows[0]["household_kwh"] is None
    assert after_daily == before_daily


def test_v5_mapping_events_represent_a_to_b_to_a_chronologically(tmp_path):
    db = EnergyDatabase(tmp_path / "energy.sqlite3")
    db.initialize()
    first = db.ensure_mapping_revision(_mapping("2026-01-01T00:00:00+00:00", "sensor.a"))
    second = db.ensure_mapping_revision(_mapping("2026-02-01T00:00:00+00:00", "sensor.b"))
    third = db.ensure_mapping_revision(_mapping("2026-03-01T00:00:00+00:00", "sensor.a"))
    assert len({first["mapping_event_id"], second["mapping_event_id"], third["mapping_event_id"]}) == 3
    current = {"source_id": "outdoor", "entity_id": "sensor.a", "attribute": None,
               "role": "outdoor", "enabled": True}
    january = db.temperature_mapping_applicability(
        "outdoor", datetime(2026, 1, 10, tzinfo=UTC),
        datetime(2026, 1, 11, tzinfo=UTC), current,
        allow_historical_identity=True,
    )
    february = db.temperature_mapping_applicability(
        "outdoor", datetime(2026, 2, 10, tzinfo=UTC),
        datetime(2026, 2, 11, tzinfo=UTC), current,
        allow_historical_identity=True,
    )
    march = db.temperature_mapping_applicability(
        "outdoor", datetime(2026, 3, 10, tzinfo=UTC),
        datetime(2026, 3, 11, tzinfo=UTC), current,
        allow_historical_identity=True,
    )
    assert january["entity_id"] == "sensor.a"
    assert february["entity_id"] == "sensor.b"
    assert march["entity_id"] == "sensor.a"
    assert january["mapping_event_id"] != march["mapping_event_id"]


def test_temperature_backfill_job_cursor_survives_restart_and_cancellation(tmp_path):
    path = tmp_path / "energy.sqlite3"
    db = EnergyDatabase(path)
    db.initialize()
    job_id = db.create_temperature_backfill_job(
        "2026-10-01", "2026-10-03", dry_run=False,
        details={"processed_days": 1},
    )
    db.update_temperature_backfill_job(
        job_id, status="running", cursor_date="2026-10-02",
        details={"processed_days": 1, "inserted_hourly": 24},
    )
    reopened = EnergyDatabase(path)
    reopened.initialize()
    resumable = reopened.resumable_temperature_backfill_jobs()
    assert [item["id"] for item in resumable] == [job_id]
    assert resumable[0]["cursor_date"] == "2026-10-02"
    assert resumable[0]["details"]["inserted_hourly"] == 24
    assert reopened.request_temperature_backfill_cancel(job_id) is True
    assert reopened.temperature_backfill_job(job_id)["cancel_requested"] is True
    assert reopened.resumable_temperature_backfill_jobs() == []


def test_v4_to_v5_migration_preserves_energy_and_checkpoint_payloads(tmp_path):
    db = EnergyDatabase(tmp_path / "energy.sqlite3")
    db.initialize()
    db.upsert_daily(record())
    start = datetime(2026, 10, 25, tzinfo=UTC)
    db.upsert_checkpoint(start, start + timedelta(hours=6), "2026-10-25", "hp", {
        "meter_kwh": 2.5, "categories": {"heating": 2.5},
    })
    with db.connect() as connection:
        daily_before = connection.execute(
            "SELECT checksum,payload_json FROM daily_records"
        ).fetchone()
        checkpoint_before = connection.execute(
            "SELECT checksum,payload_json FROM checkpoints"
        ).fetchone()
        connection.execute("UPDATE schema_version SET version=4")
    db.initialize()
    with db.connect() as connection:
        daily_after = connection.execute(
            "SELECT checksum,payload_json FROM daily_records"
        ).fetchone()
        checkpoint_after = connection.execute(
            "SELECT checksum,payload_json FROM checkpoints"
        ).fetchone()
        assert tuple(daily_after) == tuple(daily_before)
        assert tuple(checkpoint_after) == tuple(checkpoint_before)
        assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == 5


def test_v4_to_v5_migration_failure_rolls_back_schema_and_version(tmp_path, monkeypatch):
    import custom_components.home_energy_intelligence.database as database_module

    db = EnergyDatabase(tmp_path / "energy.sqlite3")
    db.initialize()
    with db.connect() as connection:
        connection.execute("UPDATE schema_version SET version=4")
        for table in (
            "temperature_history_records", "temperature_mapping_events",
            "temperature_mapping_attestations", "temperature_backfill_jobs",
        ):
            connection.execute(f"DROP TABLE {table}")

    def fail_after_schema_write(connection):
        connection.execute("CREATE TABLE v5_partial_write(value INTEGER)")
        raise sqlite3.OperationalError("injected v5 migration failure")

    monkeypatch.setattr(database_module, "_migrate_temperature_storage_v5", fail_after_schema_write)
    with pytest.raises(sqlite3.OperationalError, match="injected v5 migration failure"):
        db.initialize()
    with db.connect() as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == 4
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='temperature_history_records'"
        ).fetchone() is None
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='v5_partial_write'"
        ).fetchone() is None


def test_v4_to_v5_low_disk_defers_without_modifying_database(tmp_path, monkeypatch):
    import custom_components.home_energy_intelligence.database as database_module

    db = EnergyDatabase(tmp_path / "energy.sqlite3")
    db.initialize()
    with db.connect() as connection:
        connection.execute("UPDATE schema_version SET version=4")
        for table in (
            "temperature_history_records", "temperature_mapping_events",
            "temperature_mapping_attestations", "temperature_backfill_jobs",
        ):
            connection.execute(f"DROP TABLE {table}")
    monkeypatch.setattr(
        database_module.shutil, "disk_usage",
        lambda _path: type("Usage", (), {"free": 0})(),
    )
    with pytest.raises(RuntimeError, match="insufficient free space"):
        db.initialize()
    with db.connect() as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == 4
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='temperature_history_records'"
        ).fetchone() is None


def test_bounded_history_pagination(tmp_path):
    db = EnergyDatabase(tmp_path / "energy.sqlite3")
    db.initialize()
    for index, day in enumerate(("2026-10-23", "2026-10-24", "2026-10-25")):
        item = record(index + 1)
        item.local_date = day
        db.upsert_daily(item)
    rows, total = db.history_range(start_date="2026-10-24", end_date="2026-10-25",
                                   limit=1, offset=1)
    assert total == 2
    assert [row["local_date"] for row in rows] == ["2026-10-24"]


def test_poor_hour_can_be_reprocessed_after_late_recorder_data(tmp_path):
    db = EnergyDatabase(tmp_path / "energy.sqlite3")
    db.initialize()
    start = datetime(2026, 10, 28, 10, tzinfo=UTC)
    end = start + timedelta(hours=1)
    poor = {"local_date": "2026-10-28", "timezone": "Europe/Ljubljana",
            "period_start_utc": start.isoformat(), "period_end_utc": end.isoformat(),
            "quality": "partial", "household_kwh": None}
    assert db.upsert_hourly(poor)
    assert not db.hourly_needs_reprocess(start, end)
    assert not db.hourly_needs_reprocess(start, end, include_partial=True)
    with db.connect() as connection:
        connection.execute(
            "UPDATE hourly_records SET updated_at=?",
            ((datetime.now(UTC) - timedelta(minutes=20)).isoformat(),),
        )
    assert db.hourly_needs_reprocess(start, end, include_partial=True)
    corrected = {**poor, "quality": "good", "household_kwh": 2.5}
    assert db.upsert_hourly(corrected)
    assert not db.hourly_needs_reprocess(start, end, include_partial=True)
    assert db.hourly_range(start, end)[0]["household_kwh"] == 2.5


def test_temperature_history_repair_is_idempotent_and_preserves_energy(tmp_path):
    db = EnergyDatabase(tmp_path / "energy.sqlite3")
    db.initialize()
    item = record(42.631)
    item.temperatures = {
        "indoor": {"mean": 21.2, "coverage_seconds": 90000, "quality": "good"},
    }
    assert db.upsert_daily(item)
    start = datetime(2026, 10, 24, 22, tzinfo=UTC)
    end = start + timedelta(hours=1)
    hourly = {
        "local_date": "2026-10-25", "timezone": "Europe/Ljubljana",
        "period_start_utc": start.isoformat(), "period_end_utc": end.isoformat(),
        "day_seconds": 3600, "household_kwh": 1.2, "quality": "partial",
        "temperatures": {}, "allocations": {}, "provenance": {},
    }
    assert db.upsert_hourly(hourly)
    recovered = {
        "indoor": {"mean": 99, "coverage_seconds": 100, "quality": "partial"},
        "outdoor": {
            "mean": 8.2, "minimum": 7.8, "maximum": 8.7,
            "coverage_seconds": 90000, "period_seconds": 90000,
            "coverage_percent": 100, "latest_value": 8.4,
            "latest_timestamp": "2026-10-25T22:00:00+00:00", "quality": "good",
            "rejections": [], "period_start_utc": item.period_start_utc,
            "period_end_utc": item.period_end_utc,
            "source_provenance": {
                "entity_id": "sensor.outdoor", "source_id": "outdoor",
                "attribute": None, "role": "outdoor",
            },
        },
    }
    assert db.merge_daily_temperatures("2026-10-25", recovered, {"source": "test"})
    repaired = db.daily("2026-10-25")
    assert repaired["household_kwh"] == 42.631
    assert repaired["allocations"]["ecodan"]["categories"]["confirmed_heating"] == 4
    assert repaired["temperatures"]["indoor"]["mean"] == 21.2
    assert repaired["temperatures"]["outdoor"]["mean"] == 8.2
    assert not db.merge_daily_temperatures("2026-10-25", recovered, {"source": "test"})

    hourly_temperature = {
        "mean": 8.1, "coverage_seconds": 3600, "period_seconds": 3600,
        "coverage_percent": 100, "quality": "good", "rejections": [],
        "period_start_utc": start.isoformat(), "period_end_utc": end.isoformat(),
        "source_provenance": {"entity_id": "sensor.outdoor", "source_id": "outdoor"},
    }
    assert db.merge_hourly_temperatures(
        start, end, {"outdoor": hourly_temperature}, {"source": "test"}
    )
    repaired_hour = db.hourly_range(start, end)[0]
    assert repaired_hour["household_kwh"] == 1.2
    assert repaired_hour["temperatures"]["outdoor"]["mean"] == 8.1
    assert not db.merge_hourly_temperatures(
        start, end, {"outdoor": hourly_temperature}, {"source": "test"}
    )


def test_temperature_repair_does_not_route_through_general_daily_upsert_or_touch_auxiliary_rows(
    tmp_path, monkeypatch,
):
    db = EnergyDatabase(tmp_path / "energy.sqlite3")
    db.initialize()
    item = record(42.631)
    item.temperatures = {"indoor": {
        "mean": 21, "coverage_seconds": 90000, "period_seconds": 90000,
        "coverage_percent": 100, "quality": "good", "rejections": [],
    }}
    item.allocations["ecodan"]["rejected_count"] = 2
    db.upsert_daily(item)
    with db.connect() as connection:
        allocations_before = [tuple(row) for row in connection.execute(
            "SELECT * FROM allocations ORDER BY device,category"
        )]
        connection.execute(
            "INSERT INTO rejected_readings(local_date,entity_key,reason) VALUES (?,?,?)",
            (item.local_date, "ecodan", "equipment_power_rejected"),
        )
        rejections_before = [tuple(row) for row in connection.execute(
            "SELECT local_date,entity_key,reason FROM rejected_readings ORDER BY id"
        )]
    monkeypatch.setattr(db, "upsert_daily", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("temperature repair must not call general upsert_daily")
    ))
    assert db.merge_daily_temperatures(item.local_date, {"outdoor": {
        "mean": 8, "coverage_seconds": 90000, "period_seconds": 90000,
        "coverage_percent": 100, "quality": "good", "rejections": [],
        "period_start_utc": item.period_start_utc,
        "period_end_utc": item.period_end_utc,
        "source_provenance": {"entity_id": "sensor.outdoor", "source_id": "outdoor"},
    }}, {"source": "test"})
    with db.connect() as connection:
        allocations_after = [tuple(row) for row in connection.execute(
            "SELECT * FROM allocations ORDER BY device,category"
        )]
        rejections_after = [tuple(row) for row in connection.execute(
            "SELECT local_date,entity_key,reason FROM rejected_readings ORDER BY id"
        )]
        revision = connection.execute(
            "SELECT reason FROM revisions ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    assert allocations_after == allocations_before
    assert rejections_after == rejections_before
    assert revision == "temperature_history_repair"


def test_atomic_temperature_repair_preserves_checksum_revision_and_mapping_meaning(tmp_path):
    db = EnergyDatabase(tmp_path / "energy.sqlite3")
    db.initialize()
    item = record(42.631)
    item.temperatures = {"outdoor": {
        "mean": 5, "coverage_seconds": 90000, "period_seconds": 90000,
        "coverage_percent": 100, "quality": "good", "rejections": [],
        "source_provenance": {"entity_id": "sensor.original", "source_id": "outdoor"},
    }}
    db.upsert_daily(item)
    with db.connect() as connection:
        before = connection.execute(
            "SELECT revision,updated_at,checksum,payload_json FROM daily_records"
        ).fetchone()
    assert not db.merge_daily_temperatures(item.local_date, {"new-source": {
        "mean": 8, "coverage_seconds": 90000, "period_seconds": 90000,
        "coverage_percent": 100, "quality": "good", "rejections": [],
        "period_start_utc": "2026-10-23T22:00:00+00:00",
        "period_end_utc": item.period_end_utc,
        "source_provenance": {"entity_id": "sensor.new", "source_id": "new-source"},
    }}, {"source": "test"})
    # Lower quality cannot overwrite a complete validated summary, even with more duration.
    assert not db.merge_daily_temperatures(item.local_date, {"outdoor": {
        "mean": 6, "coverage_seconds": 99999, "period_seconds": 99999,
        "coverage_percent": 100, "quality": "filtered", "rejections": [],
        "period_start_utc": item.period_start_utc, "period_end_utc": item.period_end_utc,
        "source_provenance": {"entity_id": "sensor.original", "source_id": "outdoor"},
    }}, {"source": "test"})
    # A remapped entity never rewrites the historical meaning of the stable source ID.
    assert not db.merge_daily_temperatures(item.local_date, {"outdoor": {
        "mean": 7, "coverage_seconds": 100000, "period_seconds": 100000,
        "coverage_percent": 100, "quality": "good", "rejections": [],
        "period_start_utc": item.period_start_utc, "period_end_utc": item.period_end_utc,
        "source_provenance": {"entity_id": "sensor.replacement", "source_id": "outdoor"},
    }}, {"source": "test"})
    with db.connect() as connection:
        after = connection.execute(
            "SELECT revision,updated_at,checksum,payload_json FROM daily_records"
        ).fetchone()
    assert tuple(after) == tuple(before)
    assert after["checksum"] == logical_daily_checksum(db, item.local_date)


def test_legacy_temperature_without_entity_provenance_is_not_rewritten_by_current_mapping(tmp_path):
    db = EnergyDatabase(tmp_path / "energy.sqlite3")
    db.initialize()
    item = record()
    item.temperatures = {"outdoor": {
        "mean": 4.0, "coverage_seconds": 45000, "period_seconds": 90000,
        "coverage_percent": 50, "quality": "partial", "rejections": [],
    }}
    db.upsert_daily(item)
    assert not db.merge_daily_temperatures(item.local_date, {"outdoor": {
        "mean": 9.0, "coverage_seconds": 90000, "period_seconds": 90000,
        "coverage_percent": 100, "quality": "good", "rejections": [],
        "period_start_utc": item.period_start_utc,
        "period_end_utc": item.period_end_utc,
        "source_provenance": {"entity_id": "sensor.current_outdoor"},
    }}, {"source": "repair"})
    assert db.daily(item.local_date)["temperatures"]["outdoor"]["mean"] == 4.0


def test_temperature_repair_replaces_only_changed_source_rejections_without_duplicates(tmp_path):
    db = EnergyDatabase(tmp_path / "energy.sqlite3")
    db.initialize()
    db.upsert_daily(record(42.631))
    with db.connect() as connection:
        connection.execute(
            "INSERT INTO rejected_readings(local_date,entity_key,reason) VALUES (?,?,?)",
            ("2026-10-25", "outdoor", "out_of_range:155"),
        )
        connection.execute(
            "INSERT INTO rejected_readings(local_date,entity_key,reason) VALUES (?,?,?)",
            ("2026-10-25", "ecodan", "equipment_power_rejected"),
        )
    rejection = {
        "timestamp": "2026-10-25T12:00:00+00:00", "value": -55,
        "reason": "invalid_sentinel", "duration_seconds": 300,
        "source": "sensor.outdoor",
    }
    assert db.merge_daily_temperatures("2026-10-25", {"outdoor": {
        "mean": 8, "coverage_seconds": 89700, "period_seconds": 90000,
        "coverage_percent": 99.67, "quality": "filtered",
        "period_start_utc": record().period_start_utc,
        "period_end_utc": record().period_end_utc,
        "source_provenance": {"entity_id": "sensor.outdoor", "source_id": "outdoor"},
        "rejections": [rejection, dict(rejection)],
    }}, {"source": "test"})
    with db.connect() as connection:
        outdoor = [tuple(row) for row in connection.execute(
            """SELECT reason,source,value FROM rejected_readings
            WHERE local_date=? AND entity_key=?""", ("2026-10-25", "outdoor"),
        )]
        equipment = connection.execute(
            """SELECT reason FROM rejected_readings
            WHERE local_date=? AND entity_key=?""", ("2026-10-25", "ecodan"),
        ).fetchone()[0]
    assert outdoor == [("invalid_sentinel", "sensor.outdoor", "-55")]
    assert equipment == "equipment_power_rejected"


def test_atomic_temperature_repair_rolls_back_payload_and_revision_on_write_failure(tmp_path):
    db = EnergyDatabase(tmp_path / "energy.sqlite3")
    db.initialize()
    db.upsert_daily(record(42.631))
    with db.connect() as connection:
        before = connection.execute(
            "SELECT payload_json,checksum,revision FROM daily_records"
        ).fetchone()
        revision_count = connection.execute("SELECT COUNT(*) FROM revisions").fetchone()[0]
        connection.execute("""CREATE TRIGGER reject_temperature_repair
            BEFORE UPDATE OF payload_json ON daily_records
            BEGIN SELECT RAISE(ABORT, 'simulated repair failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="simulated repair failure"):
        db.merge_daily_temperatures("2026-10-25", {"outdoor": {
            "mean": 8, "coverage_seconds": 90000, "period_seconds": 90000,
            "coverage_percent": 100, "quality": "good", "rejections": [],
            "period_start_utc": record().period_start_utc,
            "period_end_utc": record().period_end_utc,
            "source_provenance": {"entity_id": "sensor.outdoor", "source_id": "outdoor"},
        }}, {"source": "test"})
    with db.connect() as connection:
        after = connection.execute(
            "SELECT payload_json,checksum,revision FROM daily_records"
        ).fetchone()
        assert connection.execute("SELECT COUNT(*) FROM revisions").fetchone()[0] == revision_count
    assert tuple(after) == tuple(before)
    with db.connect() as connection:
        connection.execute("DROP TRIGGER reject_temperature_repair")
    candidate = {
        "mean": 8, "coverage_seconds": 90000, "period_seconds": 90000,
        "coverage_percent": 100, "quality": "good", "rejections": [],
        "period_start_utc": record().period_start_utc,
        "period_end_utc": record().period_end_utc,
        "source_provenance": {"entity_id": "sensor.outdoor", "source_id": "outdoor"},
    }
    assert db.merge_daily_temperatures("2026-10-25", {"outdoor": candidate}, {"source": "retry"})
    assert not db.merge_daily_temperatures(
        "2026-10-25", {"outdoor": candidate}, {"source": "retry"}
    )


def test_competing_temperature_repairs_serialize_and_best_evidence_wins(tmp_path):
    db = EnergyDatabase(tmp_path / "energy.sqlite3")
    db.initialize()
    db.upsert_daily(record(42.631))
    barrier = Barrier(2)

    def repair(mean, coverage, quality):
        barrier.wait()
        return db.merge_daily_temperatures("2026-10-25", {"outdoor": {
            "mean": mean, "coverage_seconds": coverage, "period_seconds": 90000,
            "coverage_percent": coverage / 900, "quality": quality, "rejections": [],
            "period_start_utc": record().period_start_utc,
            "period_end_utc": record().period_end_utc,
            "source_provenance": {
                "entity_id": "sensor.outdoor", "source_id": "outdoor",
                "attribute": None, "role": "outdoor",
            },
        }}, {"source": "concurrent"})

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(
            lambda args: repair(*args), [(7.0, 45000, "partial"), (8.0, 90000, "good")]
        ))
    repaired = db.daily("2026-10-25")
    assert any(results)
    assert repaired["temperatures"]["outdoor"]["mean"] == 8.0
    with db.connect() as connection:
        row = connection.execute(
            "SELECT payload_json,checksum FROM daily_records WHERE local_date='2026-10-25'"
        ).fetchone()
    assert row["checksum"] == logical_daily_checksum(db, "2026-10-25")


def test_temperature_repair_and_normal_daily_hourly_writers_do_not_lose_evidence(tmp_path):
    db = EnergyDatabase(tmp_path / "energy.sqlite3")
    db.initialize()
    db.upsert_daily(record(42.631))
    candidate = {
        "mean": 8, "coverage_seconds": 90000, "period_seconds": 90000,
        "coverage_percent": 100, "quality": "good", "rejections": [],
        "period_start_utc": record().period_start_utc,
        "period_end_utc": record().period_end_utc,
        "source_provenance": {"entity_id": "sensor.outdoor", "source_id": "outdoor"},
    }
    daily_barrier = Barrier(2)

    def repair_daily():
        daily_barrier.wait()
        return db.merge_daily_temperatures(
            "2026-10-25", {"outdoor": candidate}, {"source": "concurrent"}
        )

    def write_daily():
        daily_barrier.wait()
        return db.upsert_daily(record(43.0), reason="concurrent_daily")

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(lambda function: function(), (repair_daily, write_daily)))
    daily = db.daily("2026-10-25")
    assert daily["household_kwh"] == 43.0
    assert daily["temperatures"]["outdoor"]["mean"] == 8

    start = datetime(2026, 10, 25, tzinfo=UTC)
    end = start + timedelta(hours=1)
    base_hour = {
        "local_date": "2026-10-25", "timezone": "UTC",
        "period_start_utc": start.isoformat(), "period_end_utc": end.isoformat(),
        "day_seconds": 3600, "household_kwh": 1.0, "quality": "partial",
        "temperatures": {}, "allocations": {}, "provenance": {},
    }
    db.upsert_hourly(base_hour)
    hour_candidate = {**candidate, "coverage_seconds": 3600, "period_seconds": 3600,
                      "period_start_utc": start.isoformat(),
                      "period_end_utc": end.isoformat()}
    hourly_barrier = Barrier(2)

    def repair_hour():
        hourly_barrier.wait()
        return db.merge_hourly_temperatures(
            start, end, {"outdoor": hour_candidate}, {"source": "concurrent"}
        )

    def write_hour():
        hourly_barrier.wait()
        return db.upsert_hourly({**base_hour, "household_kwh": 2.0, "quality": "good"})

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(lambda function: function(), (repair_hour, write_hour)))
    hourly = db.hourly_range(start, end)[0]
    assert hourly["household_kwh"] == 2.0
    assert hourly["temperatures"]["outdoor"]["mean"] == 8


def test_temperature_repair_waits_for_bounded_sqlite_writer_contention(tmp_path):
    db = EnergyDatabase(tmp_path / "energy.sqlite3")
    db.initialize()
    db.upsert_daily(record(42.631))
    candidate = {
        "mean": 8, "coverage_seconds": 90000, "period_seconds": 90000,
        "coverage_percent": 100, "quality": "good", "rejections": [],
        "period_start_utc": record().period_start_utc,
        "period_end_utc": record().period_end_utc,
        "source_provenance": {"entity_id": "sensor.outdoor", "source_id": "outdoor"},
    }
    locker = sqlite3.connect(db.path)
    locker.execute("BEGIN IMMEDIATE")
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                db.merge_daily_temperatures, "2026-10-25",
                {"outdoor": candidate}, {"source": "contention"},
            )
            time.sleep(0.1)
            assert not future.done()
            locker.commit()
            assert future.result(timeout=3) is True
    finally:
        locker.close()
    assert db.daily("2026-10-25")["temperatures"]["outdoor"]["mean"] == 8


def test_authoritative_full_year_is_not_limited_by_history_page(tmp_path):
    db = EnergyDatabase(tmp_path / "energy.sqlite3")
    db.initialize()
    start = datetime(2024, 1, 1, tzinfo=UTC)
    for index in range(366):
        item = record(1)
        item.local_date = (start + timedelta(days=index)).date().isoformat()
        db.upsert_daily(item)
    page, total = db.history_range(start_date="2024-01-01", end_date="2024-12-31",
                                   limit=90)
    period = db.history_period("2024-01-01", "2024-12-31")
    assert len(page) == 90 and total == 366
    assert len(period) == 366

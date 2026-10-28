from datetime import UTC, datetime
import sqlite3

from custom_components.home_energy_intelligence.database import EnergyDatabase, protected_export_path
from custom_components.home_energy_intelligence.models import DailyRecord


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


def test_mapping_revisions_preserve_source_replacement_history(tmp_path):
    db = EnergyDatabase(tmp_path / "energy.sqlite3")
    db.initialize()
    assert db.store_mapping_revision({"effective_at": "2026-01-01T00:00:00+00:00", "entity": "sensor.old"})
    assert not db.store_mapping_revision({"effective_at": "2026-01-01T00:00:00+00:00", "entity": "sensor.old"})
    assert db.store_mapping_revision({"effective_at": "2026-02-01T00:00:00+00:00", "entity": "sensor.new"})
    with db.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM mapping_revisions").fetchone()[0] == 2


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

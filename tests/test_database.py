from datetime import UTC, datetime
import sqlite3

from custom_components.home_energy_intelligence.database import EnergyDatabase
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

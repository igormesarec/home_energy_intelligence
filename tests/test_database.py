from datetime import UTC, datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
import hashlib
import sqlite3
from threading import Barrier
import time

import pytest

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
        assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == 3


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
            "source_provenance": {"entity_id": "sensor.outdoor", "source_id": "outdoor"},
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
    assert after["checksum"] == hashlib.sha256(after["payload_json"].encode()).hexdigest()


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
            "source_provenance": {"entity_id": "sensor.outdoor", "source_id": "outdoor"},
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
    assert row["checksum"] == hashlib.sha256(row["payload_json"].encode()).hexdigest()


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

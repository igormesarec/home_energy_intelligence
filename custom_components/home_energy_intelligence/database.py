"""Compact, versioned SQLite storage owned by this integration."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
from typing import Any

from .models import DailyRecord

SCHEMA_VERSION = 1
MIGRATION_1 = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS daily_records (
  local_date TEXT PRIMARY KEY, timezone TEXT NOT NULL,
  period_start_utc TEXT NOT NULL, period_end_utc TEXT NOT NULL,
  day_seconds INTEGER NOT NULL, household_kwh REAL, pv_kwh REAL,
  import_kwh REAL, export_kwh REAL, non_hvac_kwh REAL,
  quality TEXT NOT NULL, payload_json TEXT NOT NULL, checksum TEXT NOT NULL,
  revision INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS allocations (
  local_date TEXT NOT NULL, device TEXT NOT NULL, category TEXT NOT NULL,
  kwh REAL NOT NULL, measured INTEGER NOT NULL, coverage_percent REAL NOT NULL,
  quality TEXT NOT NULL, PRIMARY KEY (local_date, device, category),
  FOREIGN KEY (local_date) REFERENCES daily_records(local_date) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS checkpoints (
  interval_start_utc TEXT NOT NULL, interval_end_utc TEXT NOT NULL,
  local_date TEXT NOT NULL, device TEXT NOT NULL, payload_json TEXT NOT NULL,
  checksum TEXT NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY (interval_start_utc, interval_end_utc, device)
);
CREATE TABLE IF NOT EXISTS rejected_readings (
  id INTEGER PRIMARY KEY AUTOINCREMENT, local_date TEXT NOT NULL,
  entity_key TEXT NOT NULL, observed_at TEXT, value TEXT, reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS job_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, job_type TEXT NOT NULL,
  started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL,
  details_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS revisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, local_date TEXT NOT NULL,
  revision INTEGER NOT NULL, prior_checksum TEXT, new_checksum TEXT NOT NULL,
  reason TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS manual_thermal_readings (
  id INTEGER PRIMARY KEY AUTOINCREMENT, local_date TEXT NOT NULL,
  scope TEXT NOT NULL, thermal_kwh REAL NOT NULL, provenance TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS forecast_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
  provider TEXT NOT NULL, payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS model_parameters (
  key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_daily_records_date ON daily_records(local_date DESC);
CREATE INDEX IF NOT EXISTS idx_checkpoints_date ON checkpoints(local_date);
"""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _checksum(payload: str) -> str:
    return hashlib.sha256(payload.encode()).hexdigest()


class EnergyDatabase:
    """Dedicated database; never opens or writes the Recorder database."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(self.path, timeout=30)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            yield connection
            connection.commit()
        except Exception:
            if connection is not None:
                connection.rollback()
            raise
        finally:
            if connection is not None:
                connection.close()

    def initialize(self, *, recover_corrupt: bool = True) -> Path | None:
        recovered = None
        if self.path.exists():
            try:
                with self.connect() as connection:
                    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise sqlite3.DatabaseError("integrity check failed")
            except sqlite3.DatabaseError:
                if not recover_corrupt:
                    raise
                stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
                recovered = self.path.with_suffix(f".corrupt-{stamp}.sqlite3")
                shutil.move(self.path, recovered)
                for suffix in ("-wal", "-shm"):
                    companion = Path(f"{self.path}{suffix}")
                    if companion.exists():
                        companion.unlink()
        with self.connect() as connection:
            connection.executescript(MIGRATION_1)
            row = connection.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
            if row is None:
                connection.execute("INSERT INTO schema_version VALUES (?)", (SCHEMA_VERSION,))
            elif row[0] != SCHEMA_VERSION:
                raise RuntimeError(f"Unsupported database schema {row[0]}")
        return recovered

    def upsert_daily(self, record: DailyRecord, *, reason: str = "daily_close") -> bool:
        payload = _canonical_json(record.as_dict())
        checksum = _checksum(payload)
        now = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            prior = connection.execute(
                "SELECT checksum,revision FROM daily_records WHERE local_date=?", (record.local_date,)
            ).fetchone()
            if prior and prior["checksum"] == checksum:
                return False
            revision = 1 if prior is None else prior["revision"] + 1
            connection.execute(
                """INSERT INTO daily_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(local_date) DO UPDATE SET timezone=excluded.timezone,
                period_start_utc=excluded.period_start_utc,period_end_utc=excluded.period_end_utc,
                day_seconds=excluded.day_seconds,household_kwh=excluded.household_kwh,
                pv_kwh=excluded.pv_kwh,import_kwh=excluded.import_kwh,
                export_kwh=excluded.export_kwh,non_hvac_kwh=excluded.non_hvac_kwh,
                quality=excluded.quality,payload_json=excluded.payload_json,
                checksum=excluded.checksum,revision=excluded.revision,updated_at=excluded.updated_at""",
                (record.local_date, record.timezone, record.period_start_utc,
                 record.period_end_utc, record.day_seconds, record.household_kwh,
                 record.pv_kwh, record.import_kwh, record.export_kwh,
                 record.non_hvac_kwh, record.quality, payload, checksum, revision, now),
            )
            connection.execute(
                "INSERT INTO revisions(local_date,revision,prior_checksum,new_checksum,reason,created_at) VALUES (?,?,?,?,?,?)",
                (record.local_date, revision, prior["checksum"] if prior else None, checksum, reason, now),
            )
            connection.execute("DELETE FROM allocations WHERE local_date=?", (record.local_date,))
            connection.execute("DELETE FROM rejected_readings WHERE local_date=?", (record.local_date,))
            for device, data in record.allocations.items():
                for category, kwh in data.get("categories", {}).items():
                    connection.execute(
                        "INSERT INTO allocations VALUES (?,?,?,?,?,?,?)",
                        (record.local_date, device, category, float(kwh),
                         int(bool(data.get("measured", True))),
                         float(data.get("coverage_percent", 0)),
                         str(data.get("quality", "missing"))),
                    )
                if count := int(data.get("rejected_count", 0)):
                    connection.execute(
                        "INSERT INTO rejected_readings(local_date,entity_key,reason) VALUES (?,?,?)",
                        (record.local_date, device, f"energy_readings_rejected:{count}"),
                    )
            for entity_key, data in record.temperatures.items():
                for rejection in data.get("rejected_reasons", []):
                    reason, _, value = str(rejection).partition(":")
                    connection.execute(
                        "INSERT INTO rejected_readings(local_date,entity_key,value,reason) VALUES (?,?,?,?)",
                        (record.local_date, entity_key, value or None, reason),
                    )
        return True

    def start_job(self, job_type: str, details: dict[str, Any] | None = None) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO job_runs(job_type,started_at,status,details_json) VALUES (?,?,?,?)",
                (job_type, datetime.now(UTC).isoformat(), "running", _canonical_json(details or {})),
            )
            return int(cursor.lastrowid)

    def finish_job(self, job_id: int, status: str, details: dict[str, Any] | None = None) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE job_runs SET finished_at=?,status=?,details_json=? WHERE id=?",
                (datetime.now(UTC).isoformat(), status, _canonical_json(details or {}), job_id),
            )

    def upsert_checkpoint(self, start: datetime, end: datetime, local_date: str,
                          device: str, payload: dict[str, Any]) -> bool:
        serialized = _canonical_json(payload)
        checksum = _checksum(serialized)
        now = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            prior = connection.execute(
                "SELECT checksum FROM checkpoints WHERE interval_start_utc=? AND interval_end_utc=? AND device=?",
                (start.isoformat(), end.isoformat(), device),
            ).fetchone()
            if prior and prior["checksum"] == checksum:
                return False
            connection.execute(
                """INSERT INTO checkpoints VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(interval_start_utc,interval_end_utc,device) DO UPDATE SET
                local_date=excluded.local_date,payload_json=excluded.payload_json,
                checksum=excluded.checksum,updated_at=excluded.updated_at""",
                (start.isoformat(), end.isoformat(), local_date, device, serialized, checksum, now),
            )
        return True

    def history(self, *, limit: int = 90, before: str | None = None) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 3660))
        sql = "SELECT payload_json,revision,updated_at FROM daily_records"
        args: list[Any] = []
        if before:
            sql += " WHERE local_date < ?"
            args.append(before)
        sql += " ORDER BY local_date DESC LIMIT ?"
        args.append(limit)
        with self.connect() as connection:
            rows = connection.execute(sql, args).fetchall()
        result = []
        for row in rows:
            item = json.loads(row["payload_json"])
            item.update(revision=row["revision"], updated_at=row["updated_at"])
            result.append(item)
        return result

    def latest(self) -> dict[str, Any] | None:
        rows = self.history(limit=1)
        return rows[0] if rows else None

    def export_to(self, destination: str | Path) -> Path:
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as source, sqlite3.connect(target) as destination_db:
            source.backup(destination_db)
        return target

    def size_bytes(self) -> int:
        return sum(path.stat().st_size for path in (
            self.path, Path(f"{self.path}-wal"), Path(f"{self.path}-shm")
        ) if path.exists())

"""Compact, versioned SQLite storage owned by this integration."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import hashlib
import json
import logging
from math import isfinite
from pathlib import Path
import shutil
import sqlite3
import time
from typing import Any

from .models import DailyRecord

_LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION = 4
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
  entity_key TEXT NOT NULL, observed_at TEXT, value TEXT, reason TEXT NOT NULL,
  duration_seconds REAL, source TEXT
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
CREATE TABLE IF NOT EXISTS mapping_revisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, effective_at TEXT NOT NULL,
  checksum TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hourly_records (
  interval_start_utc TEXT NOT NULL, interval_end_utc TEXT NOT NULL,
  local_date TEXT NOT NULL, timezone TEXT NOT NULL, payload_json TEXT NOT NULL,
  checksum TEXT NOT NULL, quality TEXT NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY (interval_start_utc, interval_end_utc)
);
CREATE INDEX IF NOT EXISTS idx_daily_records_date ON daily_records(local_date DESC);
CREATE INDEX IF NOT EXISTS idx_checkpoints_date ON checkpoints(local_date);
CREATE INDEX IF NOT EXISTS idx_mapping_effective ON mapping_revisions(effective_at DESC);
CREATE INDEX IF NOT EXISTS idx_hourly_local_date ON hourly_records(local_date, interval_start_utc);
"""
MIGRATION_2 = """
ALTER TABLE rejected_readings ADD COLUMN duration_seconds REAL;
ALTER TABLE rejected_readings ADD COLUMN source TEXT;
CREATE TABLE IF NOT EXISTS mapping_revisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, effective_at TEXT NOT NULL,
  checksum TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mapping_effective ON mapping_revisions(effective_at DESC);
"""
MIGRATION_3 = """
CREATE TABLE IF NOT EXISTS hourly_records (
  interval_start_utc TEXT NOT NULL, interval_end_utc TEXT NOT NULL,
  local_date TEXT NOT NULL, timezone TEXT NOT NULL, payload_json TEXT NOT NULL,
  checksum TEXT NOT NULL, quality TEXT NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY (interval_start_utc, interval_end_utc)
);
CREATE INDEX IF NOT EXISTS idx_hourly_local_date ON hourly_records(local_date, interval_start_utc);
"""
MIGRATION_4 = """
CREATE TABLE IF NOT EXISTS temperature_summaries (
  record_kind TEXT NOT NULL, record_key TEXT NOT NULL, source_id TEXT NOT NULL,
  payload_json TEXT NOT NULL, checksum TEXT NOT NULL,
  PRIMARY KEY (record_kind, record_key, source_id)
);
CREATE TABLE IF NOT EXISTS hourly_temperature_rejections (
  record_key TEXT NOT NULL, source_id TEXT NOT NULL, observed_at TEXT NOT NULL,
  value_json TEXT NOT NULL, reason TEXT NOT NULL, duration_seconds REAL NOT NULL,
  source TEXT NOT NULL,
  PRIMARY KEY (record_key,source_id,observed_at,value_json,reason,duration_seconds,source)
);
CREATE INDEX IF NOT EXISTS idx_temperature_summary_record
  ON temperature_summaries(record_kind, record_key);
"""

_TEMPERATURE_REJECTION_REASONS = (
    "invalid_sentinel", "out_of_range", "temporal_spike",
    "contaminated_statistics_bounds", "invalid_statistics_bounds",
    "incompatible_temperature_unit", "non_numeric",
)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _checksum(payload: str) -> str:
    return hashlib.sha256(payload.encode()).hexdigest()


def _compact_temperature_summary(summary: dict[str, Any]) -> dict[str, Any]:
    """Keep interval statistics separate from potentially large audit arrays."""
    compact = {
        key: value for key, value in summary.items()
        if key != "rejections"
    }
    compact["_normalized_audit_fields"] = [
        key for key in ("rejections",) if key in summary
    ]
    return compact


def _normalized_rejections(summary: dict[str, Any]) -> list[dict[str, Any]]:
    # ``rejected_reasons`` remains in the compact summary.  It is not a
    # substitute for the richer detailed rejection list: synthesizing one
    # would change the logical payload and turn an unavailable/unit reason into
    # a fabricated timestamped measurement rejection.
    return [
        dict(item) for item in summary.get("rejections", [])
        if isinstance(item, dict)
    ]


def _store_normalized_temperatures(
    connection: sqlite3.Connection, record_kind: str, record_key: str,
    temperatures: dict[str, Any],
) -> None:
    """Replace normalized temperature evidence inside the caller transaction."""
    connection.execute(
        "DELETE FROM temperature_summaries WHERE record_kind=? AND record_key=?",
        (record_kind, record_key),
    )
    if record_kind == "hourly":
        connection.execute(
            "DELETE FROM hourly_temperature_rejections WHERE record_key=?", (record_key,)
        )
    for source_id, summary in (temperatures or {}).items():
        if not isinstance(summary, dict):
            continue
        compact = _canonical_json(_compact_temperature_summary(summary))
        connection.execute(
            "INSERT INTO temperature_summaries VALUES (?,?,?,?,?)",
            (record_kind, record_key, source_id, compact, _checksum(compact)),
        )
        if record_kind != "hourly":
            continue
        seen: set[tuple[Any, ...]] = set()
        for rejection in _normalized_rejections(summary):
            values = (
                str(rejection.get("timestamp") or ""),
                _canonical_json(rejection.get("value")),
                str(rejection.get("reason") or "invalid"),
                float(rejection.get("duration_seconds")
                      if rejection.get("duration_seconds") is not None else -1),
                str(rejection.get("source") or ""),
            )
            if values in seen:
                continue
            seen.add(values)
            connection.execute(
                "INSERT OR IGNORE INTO hourly_temperature_rejections VALUES (?,?,?,?,?,?,?)",
                (record_key, source_id, *values),
            )


def _daily_rejections(
    connection: sqlite3.Connection, local_date: str, source_id: str,
) -> list[dict[str, Any]]:
    reason_clauses = " OR ".join(
        "reason LIKE ?" for _ in _TEMPERATURE_REJECTION_REASONS
    )
    rows = connection.execute(
        f"""SELECT observed_at,value,reason,duration_seconds,source
        FROM rejected_readings WHERE local_date=? AND entity_key=?
        AND source IS NOT NULL AND ({reason_clauses}) ORDER BY id""",
        (local_date, source_id,
         *(f"{reason}%" for reason in _TEMPERATURE_REJECTION_REASONS)),
    ).fetchall()
    result = []
    for row in rows:
        raw_value = row["value"]
        try:
            value = json.loads(raw_value) if raw_value is not None else None
        except (TypeError, json.JSONDecodeError):
            value = raw_value
        result.append({
            "timestamp": row["observed_at"], "value": value,
            "reason": row["reason"], "duration_seconds": row["duration_seconds"],
            "source": row["source"],
        })
    return result


def _hydrate_payload(
    connection: sqlite3.Connection, payload_json: str, record_kind: str,
    record_key: str,
) -> dict[str, Any]:
    payload = json.loads(payload_json)
    rows = connection.execute(
        """SELECT source_id,payload_json FROM temperature_summaries
        WHERE record_kind=? AND record_key=? ORDER BY source_id""",
        (record_kind, record_key),
    ).fetchall()
    if not rows:
        return payload
    temperatures: dict[str, Any] = {}
    for row in rows:
        summary = json.loads(row["payload_json"])
        audit_fields = summary.pop("_normalized_audit_fields", [])
        if record_kind == "daily":
            rejections = _daily_rejections(connection, record_key, row["source_id"])
        else:
            rejection_rows = connection.execute(
                """SELECT observed_at,value_json,reason,duration_seconds,source
                FROM hourly_temperature_rejections
                WHERE record_key=? AND source_id=? ORDER BY observed_at,reason""",
                (record_key, row["source_id"]),
            ).fetchall()
            rejections = [{
                "timestamp": item["observed_at"] or None,
                "value": json.loads(item["value_json"]),
                "reason": item["reason"],
                "duration_seconds": (None if item["duration_seconds"] < 0
                                     else item["duration_seconds"]),
                "source": item["source"] or None,
            } for item in rejection_rows]
        if rejections or "rejections" in audit_fields:
            summary["rejections"] = rejections
        temperatures[row["source_id"]] = summary
    payload["temperatures"] = temperatures
    return payload


def _core_payload(payload: dict[str, Any]) -> str:
    core = dict(payload)
    core.pop("temperatures", None)
    return _canonical_json(core)


def _migrate_temperature_storage_v4(connection: sqlite3.Connection) -> None:
    """Normalize existing temperature evidence without changing logical checksums."""
    # Keyset pagination keeps peak migration memory bounded by one batch rather
    # than materialising every historical JSON payload at startup.
    last_date = ""
    while True:
        daily_rows = connection.execute(
            """SELECT local_date,payload_json FROM daily_records
            WHERE local_date>? ORDER BY local_date LIMIT 100""", (last_date,)
        ).fetchall()
        if not daily_rows:
            break
        for row in daily_rows:
            last_date = row["local_date"]
            payload = json.loads(row["payload_json"])
            temperatures = payload.get("temperatures") or {}
            if not temperatures:
                continue
            for source_id, summary in temperatures.items():
                if not isinstance(summary, dict):
                    continue
                for rejection in _normalized_rejections(summary):
                    observed = rejection.get("timestamp")
                    value = rejection.get("value")
                    value_text = None if value is None else str(value)
                    reason = str(rejection.get("reason") or "invalid")
                    duration = rejection.get("duration_seconds")
                    source = rejection.get("source")
                    exists = connection.execute(
                        """SELECT 1 FROM rejected_readings
                        WHERE local_date=? AND entity_key=?
                        AND COALESCE(observed_at,'')=COALESCE(?,'')
                        AND COALESCE(value,'')=COALESCE(?,'') AND reason=?
                        AND COALESCE(duration_seconds,-1)=COALESCE(?,-1)
                        AND COALESCE(source,'')=COALESCE(?,'') LIMIT 1""",
                        (row["local_date"], source_id, observed, value_text, reason,
                         duration, source),
                    ).fetchone()
                    if exists is None:
                        connection.execute(
                            """INSERT INTO rejected_readings
                            (local_date,entity_key,observed_at,value,reason,duration_seconds,source)
                            VALUES (?,?,?,?,?,?,?)""",
                            (row["local_date"], source_id, observed, value_text, reason,
                             duration, source),
                        )
            _store_normalized_temperatures(
                connection, "daily", row["local_date"], temperatures
            )
            connection.execute(
                "UPDATE daily_records SET payload_json=? WHERE local_date=?",
                (_core_payload(payload), row["local_date"]),
            )

    last_hour = ""
    while True:
        hourly_rows = connection.execute(
            """SELECT interval_start_utc,payload_json FROM hourly_records
            WHERE interval_start_utc>? ORDER BY interval_start_utc LIMIT 100""",
            (last_hour,),
        ).fetchall()
        if not hourly_rows:
            break
        for row in hourly_rows:
            last_hour = row["interval_start_utc"]
            payload = json.loads(row["payload_json"])
            temperatures = payload.get("temperatures") or {}
            if not temperatures:
                continue
            _store_normalized_temperatures(
                connection, "hourly", row["interval_start_utc"], temperatures
            )
            connection.execute(
                """UPDATE hourly_records SET payload_json=?
                WHERE interval_start_utc=?""",
                (_core_payload(payload), row["interval_start_utc"]),
            )


def _temperature_coverage(value: dict[str, Any] | None) -> float:
    if not isinstance(value, dict):
        return -1.0
    coverage = value.get("coverage_seconds")
    if isinstance(coverage, (int, float)):
        return float(coverage)
    percent = value.get("coverage_percent")
    period = value.get("period_seconds")
    if isinstance(percent, (int, float)) and isinstance(period, (int, float)):
        return float(percent) * float(period) / 100
    return 0.0


def _merge_temperature_maps(
    existing: dict[str, Any], recovered: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Add better recovered summaries without replacing valid measurements."""
    merged = dict(existing or {})
    changed = False
    quality_rank = {"missing": 0, "poor": 1, "partial": 2, "filtered": 3, "good": 4}
    for source_id, candidate in (recovered or {}).items():
        current = merged.get(source_id)
        candidate_mean = candidate.get("mean") if isinstance(candidate, dict) else None
        current_mean = current.get("mean") if isinstance(current, dict) else None
        candidate_valid = (
            isinstance(candidate_mean, (int, float)) and isfinite(float(candidate_mean))
            and _temperature_coverage(candidate) > 0
            and candidate.get("quality") not in {"missing", "poor", "rejected", "unreconciled"}
        )
        current_valid = (
            isinstance(current_mean, (int, float)) and isfinite(float(current_mean))
            and _temperature_coverage(current) > 0
        )
        current_source = (current or {}).get("source_provenance") or {}
        candidate_source = (candidate or {}).get("source_provenance") or {}
        if current is not None and not current_source.get("entity_id"):
            # Legacy rows without source identity are intentionally immutable.
            # A current mapping cannot prove that it represents the same
            # historical sensor, even when the stable source_id is unchanged.
            continue
        if (
            current_source.get("entity_id") and candidate_source.get("entity_id")
            and current_source["entity_id"] != candidate_source["entity_id"]
        ):
            # A remap is a new historical meaning, not permission to rewrite
            # an older source under the same stable source ID.
            continue
        if current_valid and quality_rank.get(
            str(candidate.get("quality")), 0
        ) < quality_rank.get(str(current.get("quality")), 0):
            continue
        if current_valid and (
            not candidate_valid
            or _temperature_coverage(current) >= _temperature_coverage(candidate)
        ):
            continue
        if candidate_valid or current is None:
            merged[source_id] = candidate
            changed = changed or current != candidate
    return merged, changed


def _eligible_temperature_repair(
    recovered: dict[str, Any], start: str, end: str,
) -> dict[str, Any]:
    """Accept only summaries tied to the exact interval and configured entity."""
    return {
        source_id: summary for source_id, summary in (recovered or {}).items()
        if isinstance(summary, dict)
        and summary.get("period_start_utc") == start
        and summary.get("period_end_utc") == end
        and isinstance(summary.get("source_provenance"), dict)
        and bool(summary["source_provenance"].get("entity_id"))
    }


def protected_export_path(config_path: str | Path, stamp: str) -> Path:
    """Return a non-web-served backup path owned by the integration."""
    if not stamp or any(char not in "0123456789-" for char in stamp):
        raise ValueError("invalid export timestamp")
    return Path(config_path) / ".storage" / "home_energy_intelligence" / "exports" / f"home_energy_intelligence-{stamp}.sqlite3"


class EnergyDatabase:
    """Dedicated database; never opens or writes the Recorder database."""

    CONNECT_TIMEOUT_SECONDS = 30.0

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                self.path, timeout=self.CONNECT_TIMEOUT_SECONDS
            )
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
            except sqlite3.DatabaseError as error:
                # Lock contention and transient I/O failures are not evidence
                # of corruption.  Never rename a healthy database merely
                # because another HA worker currently owns the SQLite lock.
                if any(token in str(error).lower() for token in (
                    "locked", "busy", "disk i/o", "readonly", "full",
                )):
                    raise
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
            has_schema = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
            ).fetchone()
            if has_schema is None:
                connection.executescript(MIGRATION_1)
            row = connection.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
            if row is not None and row[0] < SCHEMA_VERSION:
                required = max(8 * 1024 * 1024, self.size_bytes() // 2)
                free = shutil.disk_usage(self.path.parent).free
                if free < required:
                    raise RuntimeError(
                        "Deferred schema migration: insufficient free space; "
                        "create a protected SQLite backup and free at least "
                        f"{required} bytes"
                    )
                _LOGGER.warning(
                    "Home Energy Intelligence schema migration %s -> %s is starting; "
                    "a protected SQLite backup is recommended. Automatic VACUUM is disabled.",
                    row[0], SCHEMA_VERSION,
                )
            if row is None:
                connection.executescript("BEGIN IMMEDIATE;\n" + MIGRATION_4)
                connection.execute("INSERT INTO schema_version VALUES (?)", (SCHEMA_VERSION,))
            elif row[0] == 1:
                connection.executescript(
                    "BEGIN IMMEDIATE;\n" + MIGRATION_2 + MIGRATION_3 + MIGRATION_4
                )
                _migrate_temperature_storage_v4(connection)
                connection.execute("UPDATE schema_version SET version=?", (SCHEMA_VERSION,))
            elif row[0] == 2:
                connection.executescript("BEGIN IMMEDIATE;\n" + MIGRATION_3 + MIGRATION_4)
                _migrate_temperature_storage_v4(connection)
                connection.execute("UPDATE schema_version SET version=?", (SCHEMA_VERSION,))
            elif row[0] == 3:
                connection.executescript("BEGIN IMMEDIATE;\n" + MIGRATION_4)
                _migrate_temperature_storage_v4(connection)
                connection.execute("UPDATE schema_version SET version=?", (SCHEMA_VERSION,))
            elif row[0] != SCHEMA_VERSION:
                raise RuntimeError(f"Unsupported database schema {row[0]}")
        return recovered

    def upsert_daily(self, record: DailyRecord, *, reason: str = "daily_close") -> bool:
        now = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                "SELECT checksum,revision,payload_json FROM daily_records WHERE local_date=?",
                (record.local_date,),
            ).fetchone()
            payload_data = record.as_dict()
            if prior:
                prior_payload = _hydrate_payload(
                    connection, prior["payload_json"], "daily", record.local_date
                )
                eligible_prior = _eligible_temperature_repair(
                    prior_payload.get("temperatures", {}),
                    payload_data["period_start_utc"], payload_data["period_end_utc"],
                )
                payload_data["temperatures"], carried_temperature = _merge_temperature_maps(
                    payload_data.get("temperatures", {}), eligible_prior,
                )
                if carried_temperature and prior_payload.get("provenance", {}).get(
                    "temperature_reprocessing"
                ):
                    payload_data.setdefault("provenance", {}).setdefault(
                        "temperature_reprocessing",
                        prior_payload["provenance"]["temperature_reprocessing"],
                    )
            logical_payload = _canonical_json(payload_data)
            payload = _core_payload(payload_data)
            checksum = _checksum(logical_payload)
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
            _store_normalized_temperatures(
                connection, "daily", record.local_date,
                payload_data.get("temperatures", {}),
            )
            connection.execute(
                "INSERT INTO revisions(local_date,revision,prior_checksum,new_checksum,reason,created_at) VALUES (?,?,?,?,?,?)",
                (record.local_date, revision, prior["checksum"] if prior else None, checksum, reason, now),
            )
            connection.execute("DELETE FROM allocations WHERE local_date=?", (record.local_date,))
            connection.execute("DELETE FROM rejected_readings WHERE local_date=?", (record.local_date,))
            for device, data in payload_data.get("allocations", {}).items():
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
            for entity_key, data in payload_data.get("temperatures", {}).items():
                detailed = data.get("rejections", [])
                for rejection in detailed:
                    connection.execute(
                        """INSERT INTO rejected_readings
                        (local_date,entity_key,observed_at,value,reason,duration_seconds,source)
                        VALUES (?,?,?,?,?,?,?)""",
                        (record.local_date, entity_key, rejection.get("timestamp"),
                         rejection.get("value"), rejection.get("reason", "invalid"),
                         rejection.get("duration_seconds"), rejection.get("source")),
                    )
                if detailed:
                    continue
                for rejection in data.get("rejected_reasons", []):
                    reason, _, value = str(rejection).partition(":")
                    connection.execute(
                        "INSERT INTO rejected_readings(local_date,entity_key,value,reason) VALUES (?,?,?,?)",
                        (record.local_date, entity_key, value or None, reason),
                    )
        return True

    def store_mapping_revision(self, payload: dict[str, Any]) -> bool:
        serialized = _canonical_json(payload)
        checksum = _checksum(_canonical_json({key: value for key, value in payload.items()
                                              if key != "effective_at"}))
        with self.connect() as connection:
            prior = connection.execute(
                "SELECT 1 FROM mapping_revisions WHERE checksum=?", (checksum,)
            ).fetchone()
            if prior:
                return False
            connection.execute(
                "INSERT INTO mapping_revisions(effective_at,checksum,payload_json) VALUES (?,?,?)",
                (payload.get("effective_at", datetime.now(UTC).isoformat()), checksum, serialized),
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

    def checkpoints_for_day(self, local_date: str) -> dict[str, list[dict[str, Any]]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT interval_start_utc,interval_end_utc,device,payload_json,updated_at
                FROM checkpoints WHERE local_date=? ORDER BY interval_start_utc""",
                (local_date,),
            ).fetchall()
        result: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            payload = json.loads(row["payload_json"])
            payload.update(
                interval_start_utc=row["interval_start_utc"],
                interval_end_utc=row["interval_end_utc"],
                updated_at=row["updated_at"],
            )
            result.setdefault(row["device"], []).append(payload)
        return result

    def checkpoint_exists(self, start: datetime, end: datetime, device: str | None = None) -> bool:
        sql = "SELECT 1 FROM checkpoints WHERE interval_start_utc=? AND interval_end_utc=?"
        arguments: list[Any] = [start.isoformat(), end.isoformat()]
        if device is not None:
            sql += " AND device=?"
            arguments.append(device)
        sql += " LIMIT 1"
        with self.connect() as connection:
            row = connection.execute(sql, arguments).fetchone()
        return row is not None

    def missing_checkpoint_devices(
        self, start: datetime, end: datetime, enabled_devices: set[str]
    ) -> set[str]:
        """Return only enabled equipment missing this exact checkpoint."""
        if not enabled_devices:
            return set()
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT device FROM checkpoints WHERE interval_start_utc=? AND interval_end_utc=?",
                (start.isoformat(), end.isoformat()),
            ).fetchall()
        present = {str(row["device"]) for row in rows}
        return set(enabled_devices) - present

    def history(self, *, limit: int = 90, before: str | None = None) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 3660))
        sql = "SELECT local_date,payload_json,revision,updated_at FROM daily_records"
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
                item = _hydrate_payload(
                    connection, row["payload_json"], "daily", row["local_date"]
                )
                item.update(revision=row["revision"], updated_at=row["updated_at"])
                result.append(item)
        return result

    def history_range(
        self, *, start_date: str | None = None, end_date: str | None = None,
        limit: int = 90, offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """Return a bounded page and total count without loading all history."""
        limit = max(1, min(limit, 366))
        offset = max(0, offset)
        clauses, args = [], []
        if start_date:
            clauses.append("local_date >= ?")
            args.append(start_date)
        if end_date:
            clauses.append("local_date <= ?")
            args.append(end_date)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connect() as connection:
            total = int(connection.execute(
                f"SELECT COUNT(*) FROM daily_records{where}", args
            ).fetchone()[0])
            rows = connection.execute(
                f"SELECT local_date,payload_json,revision,updated_at FROM daily_records{where} "
                "ORDER BY local_date DESC LIMIT ? OFFSET ?", [*args, limit, offset],
            ).fetchall()
            result = []
            for row in rows:
                item = _hydrate_payload(
                    connection, row["payload_json"], "daily", row["local_date"]
                )
                item.update(revision=row["revision"], updated_at=row["updated_at"])
                result.append(item)
        return result, total

    def history_period(self, start_date: str, end_date: str) -> list[dict[str, Any]]:
        """Load an entire explicitly bounded period independently of page size."""
        from datetime import date

        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
        if end < start or (end - start).days > 3660:
            raise ValueError("history period must be ordered and no longer than ten years")
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT local_date,payload_json,revision,updated_at FROM daily_records
                WHERE local_date>=? AND local_date<=? ORDER BY local_date""",
                (start_date, end_date),
            ).fetchall()
            result = []
            for row in rows:
                item = _hydrate_payload(
                    connection, row["payload_json"], "daily", row["local_date"]
                )
                item.update(revision=row["revision"], updated_at=row["updated_at"])
                result.append(item)
        return result

    def upsert_hourly(self, payload: dict[str, Any]) -> bool:
        """Store one completed elapsed-hour interval idempotently."""
        start = str(payload["period_start_utc"])
        end = str(payload["period_end_utc"])
        now = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                """SELECT checksum,payload_json FROM hourly_records
                WHERE interval_start_utc=? AND interval_end_utc=?""",
                (start, end),
            ).fetchone()
            payload_data = dict(payload)
            if prior:
                prior_payload = _hydrate_payload(
                    connection, prior["payload_json"], "hourly", start
                )
                eligible_prior = _eligible_temperature_repair(
                    prior_payload.get("temperatures", {}), start, end,
                )
                carried_temperature = False
                if "temperatures" in payload_data or eligible_prior:
                    payload_data["temperatures"], carried_temperature = _merge_temperature_maps(
                        payload_data.get("temperatures", {}), eligible_prior,
                    )
                if carried_temperature and prior_payload.get("provenance", {}).get(
                    "temperature_reprocessing"
                ):
                    payload_data.setdefault("provenance", {}).setdefault(
                        "temperature_reprocessing",
                        prior_payload["provenance"]["temperature_reprocessing"],
                    )
            logical_payload = _canonical_json(payload_data)
            serialized = _core_payload(payload_data)
            checksum = _checksum(logical_payload)
            if prior and prior["checksum"] == checksum:
                return False
            connection.execute(
                """INSERT INTO hourly_records VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(interval_start_utc,interval_end_utc) DO UPDATE SET
                local_date=excluded.local_date,timezone=excluded.timezone,
                payload_json=excluded.payload_json,checksum=excluded.checksum,
                quality=excluded.quality,updated_at=excluded.updated_at""",
                (start, end, payload_data["local_date"], payload_data["timezone"], serialized,
                 checksum, payload_data.get("quality", "missing"), now),
            )
            _store_normalized_temperatures(
                connection, "hourly", start, payload_data.get("temperatures", {})
            )
        return True

    def merge_daily_temperatures(
        self, local_date: str, temperatures: dict[str, Any],
        provenance: dict[str, Any] | None = None,
    ) -> bool:
        """Atomically merge temperature evidence without rebuilding daily energy."""
        for attempt in range(3):
            try:
                with self.connect() as connection:
                    connection.execute("PRAGMA busy_timeout=1000")
                    connection.execute("BEGIN IMMEDIATE")
                    row = connection.execute(
                        "SELECT payload_json,checksum,revision FROM daily_records WHERE local_date=?",
                        (local_date,),
                    ).fetchone()
                    if row is None:
                        return False
                    payload = _hydrate_payload(
                        connection, row["payload_json"], "daily", local_date
                    )
                    eligible = _eligible_temperature_repair(
                        temperatures, payload.get("period_start_utc"),
                        payload.get("period_end_utc"),
                    )
                    merged, changed = _merge_temperature_maps(
                        payload.get("temperatures", {}), eligible,
                    )
                    if not changed:
                        return False
                    changed_sources = sorted(
                        source_id for source_id, value in merged.items()
                        if payload.get("temperatures", {}).get(source_id) != value
                    )
                    payload["temperatures"] = merged
                    payload.setdefault("provenance", {})["temperature_reprocessing"] = {
                        **(provenance or {}), "source_ids": changed_sources,
                    }
                    logical_payload = _canonical_json(payload)
                    serialized = _core_payload(payload)
                    checksum = _checksum(logical_payload)
                    now = datetime.now(UTC).isoformat()
                    revision = int(row["revision"]) + 1
                    updated = connection.execute(
                        """UPDATE daily_records SET payload_json=?,checksum=?,revision=?,updated_at=?
                        WHERE local_date=? AND checksum=?""",
                        (serialized, checksum, revision, now, local_date, row["checksum"]),
                    )
                    if updated.rowcount != 1:
                        raise sqlite3.OperationalError("daily record changed during repair")
                    _store_normalized_temperatures(
                        connection, "daily", local_date, payload.get("temperatures", {})
                    )
                    connection.execute(
                        """INSERT INTO revisions
                        (local_date,revision,prior_checksum,new_checksum,reason,created_at)
                        VALUES (?,?,?,?,?,?)""",
                        (local_date, revision, row["checksum"], checksum,
                         "temperature_history_repair", now),
                    )
                    for source_id in changed_sources:
                        reason_clauses = " OR ".join(
                            "reason LIKE ?" for _ in _TEMPERATURE_REJECTION_REASONS
                        )
                        connection.execute(
                            f"""DELETE FROM rejected_readings WHERE local_date=? AND entity_key=?
                            AND ({reason_clauses})""",
                            (local_date, source_id,
                             *(f"{reason}%" for reason in _TEMPERATURE_REJECTION_REASONS)),
                        )
                        inserted_rejections: set[tuple[Any, ...]] = set()
                        for rejection in (merged[source_id].get("rejections") or []):
                            rejection_key = (
                                rejection.get("timestamp"), rejection.get("value"),
                                rejection.get("reason", "invalid"),
                                rejection.get("duration_seconds"), rejection.get("source"),
                            )
                            if rejection_key in inserted_rejections:
                                continue
                            inserted_rejections.add(rejection_key)
                            connection.execute(
                                """INSERT INTO rejected_readings
                                (local_date,entity_key,observed_at,value,reason,duration_seconds,source)
                                VALUES (?,?,?,?,?,?,?)""",
                                (local_date, source_id, *rejection_key),
                            )
                return True
            except sqlite3.OperationalError as error:
                if attempt == 2 or "locked" not in str(error).lower():
                    raise
                time.sleep(0.05 * (attempt + 1))
        return False

    def merge_hourly_temperatures(
        self, start: datetime, end: datetime, temperatures: dict[str, Any],
        provenance: dict[str, Any] | None = None,
    ) -> bool:
        """Atomically merge an existing hour without reconstructing energy values."""
        for attempt in range(3):
            try:
                with self.connect() as connection:
                    connection.execute("PRAGMA busy_timeout=1000")
                    connection.execute("BEGIN IMMEDIATE")
                    row = connection.execute(
                        """SELECT payload_json,checksum FROM hourly_records
                        WHERE interval_start_utc=? AND interval_end_utc=?""",
                        (start.isoformat(), end.isoformat()),
                    ).fetchone()
                    if row is None:
                        return False
                    payload = _hydrate_payload(
                        connection, row["payload_json"], "hourly", start.isoformat()
                    )
                    eligible = _eligible_temperature_repair(
                        temperatures, payload.get("period_start_utc"),
                        payload.get("period_end_utc"),
                    )
                    merged, changed = _merge_temperature_maps(
                        payload.get("temperatures", {}), eligible,
                    )
                    if not changed:
                        return False
                    changed_sources = sorted(
                        source_id for source_id, value in merged.items()
                        if payload.get("temperatures", {}).get(source_id) != value
                    )
                    payload["temperatures"] = merged
                    payload.setdefault("provenance", {})["temperature_reprocessing"] = {
                        **(provenance or {}), "source_ids": changed_sources,
                    }
                    logical_payload = _canonical_json(payload)
                    serialized = _core_payload(payload)
                    checksum = _checksum(logical_payload)
                    updated = connection.execute(
                        """UPDATE hourly_records SET payload_json=?,checksum=?,updated_at=?
                        WHERE interval_start_utc=? AND interval_end_utc=? AND checksum=?""",
                        (serialized, checksum, datetime.now(UTC).isoformat(),
                         start.isoformat(), end.isoformat(), row["checksum"]),
                    )
                    if updated.rowcount != 1:
                        raise sqlite3.OperationalError("hourly record changed during repair")
                    _store_normalized_temperatures(
                        connection, "hourly", start.isoformat(),
                        payload.get("temperatures", {}),
                    )
                return True
            except sqlite3.OperationalError as error:
                if attempt == 2 or "locked" not in str(error).lower():
                    raise
                time.sleep(0.05 * (attempt + 1))
        return False

    def hourly_range(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        """Load only intervals overlapping a bounded UTC range."""
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT interval_start_utc,payload_json,updated_at FROM hourly_records
                WHERE interval_start_utc>=? AND interval_end_utc<=?
                ORDER BY interval_start_utc""", (start.isoformat(), end.isoformat()),
            ).fetchall()
            result = []
            for row in rows:
                item = _hydrate_payload(
                    connection, row["payload_json"], "hourly",
                    row["interval_start_utc"],
                )
                item["updated_at"] = row["updated_at"]
                result.append(item)
        return result

    def hourly_exists(self, start: datetime, end: datetime) -> bool:
        with self.connect() as connection:
            return connection.execute(
                "SELECT 1 FROM hourly_records WHERE interval_start_utc=? AND interval_end_utc=?",
                (start.isoformat(), end.isoformat()),
            ).fetchone() is not None

    def hourly_needs_reprocess(
        self, start: datetime, end: datetime, *, include_partial: bool = False,
    ) -> bool:
        """Return whether an interval is absent or eligible for controlled repair."""
        with self.connect() as connection:
            row = connection.execute(
                """SELECT quality,updated_at FROM hourly_records
                WHERE interval_start_utc=? AND interval_end_utc=?""",
                (start.isoformat(), end.isoformat()),
            ).fetchone()
        if row is None:
            return True
        retryable = {"missing", "poor", "missing_counter", "unreconciled"}
        if include_partial:
            updated = datetime.fromisoformat(row["updated_at"])
            if datetime.now(UTC) - updated >= timedelta(minutes=15):
                retryable.add("partial")
        return str(row["quality"]) in retryable

    def latest(self) -> dict[str, Any] | None:
        rows = self.history(limit=1)
        return rows[0] if rows else None

    def daily(self, local_date: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT payload_json,revision,updated_at FROM daily_records WHERE local_date=?",
                (local_date,),
            ).fetchone()
            if row is None:
                return None
            item = _hydrate_payload(
                connection, row["payload_json"], "daily", local_date
            )
            item.update(revision=row["revision"], updated_at=row["updated_at"])
        return item

    def daily_bounds(self) -> tuple[str | None, str | None]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT MIN(local_date),MAX(local_date) FROM daily_records"
            ).fetchone()
        return row[0], row[1]

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
    def storage_status(self) -> dict[str, Any]:
        """Return non-secret migration/maintenance diagnostics for Settings."""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT version FROM schema_version LIMIT 1"
            ).fetchone()
        size = self.size_bytes()
        return {
            "schema_version": row[0] if row else None,
            "target_schema_version": SCHEMA_VERSION,
            "migration_required": bool(row and row[0] < SCHEMA_VERSION),
            "database_size_bytes": size,
            "free_space_bytes": shutil.disk_usage(self.path.parent).free,
            "startup_vacuum_enabled": False,
            "backup_recommended_before_migration": True,
            "backup_method": "protected_sqlite_online_backup",
            "maintenance_compaction": "manual_offline_only",
        }

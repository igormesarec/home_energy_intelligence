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

SCHEMA_VERSION = 5
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
MIGRATION_5 = """
CREATE TABLE IF NOT EXISTS temperature_history_records (
  record_kind TEXT NOT NULL CHECK(record_kind IN ('hourly','daily')),
  record_key TEXT NOT NULL, interval_start_utc TEXT NOT NULL,
  interval_end_utc TEXT NOT NULL, local_date TEXT NOT NULL,
  timezone TEXT NOT NULL, source_id TEXT NOT NULL,
  mapping_event_id INTEGER, payload_json TEXT NOT NULL,
  checksum TEXT NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY (record_kind, record_key, source_id)
);
CREATE INDEX IF NOT EXISTS idx_temperature_history_interval
  ON temperature_history_records(record_kind, interval_start_utc, interval_end_utc);
CREATE INDEX IF NOT EXISTS idx_temperature_history_local_date
  ON temperature_history_records(record_kind, local_date, source_id);
CREATE TABLE IF NOT EXISTS temperature_mapping_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, effective_at TEXT NOT NULL,
  identity_checksum TEXT NOT NULL, payload_json TEXT NOT NULL,
  origin TEXT NOT NULL DEFAULT 'configuration', created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_temperature_mapping_event_effective
  ON temperature_mapping_events(effective_at, id);
CREATE TABLE IF NOT EXISTS temperature_mapping_attestations (
  id INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT NOT NULL,
  interval_start_utc TEXT NOT NULL, interval_end_utc TEXT NOT NULL,
  identity_checksum TEXT NOT NULL, payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_temperature_attestation_interval
  ON temperature_mapping_attestations(source_id, interval_start_utc, interval_end_utc);
CREATE TABLE IF NOT EXISTS temperature_backfill_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, start_date TEXT NOT NULL,
  end_date TEXT NOT NULL, cursor_date TEXT, dry_run INTEGER NOT NULL,
  status TEXT NOT NULL, cancel_requested INTEGER NOT NULL DEFAULT 0,
  details_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
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


def _temperature_identity(mapping: dict[str, Any]) -> dict[str, Any]:
    return {
        key: mapping.get(key)
        for key in ("source_id", "entity_id", "attribute", "role", "enabled")
    }


def _temperature_identity_checksum(mapping: dict[str, Any]) -> str:
    return _checksum(_canonical_json(_temperature_identity(mapping)))


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


def _migrate_temperature_storage_v5(connection: sqlite3.Connection) -> None:
    """Add independent temperature history without rewriting V4 evidence."""
    for statement in MIGRATION_5.split(";"):
        if statement.strip():
            connection.execute(statement)
    rows = connection.execute(
        "SELECT effective_at,checksum,payload_json FROM mapping_revisions ORDER BY id"
    ).fetchall()
    now = datetime.now(UTC).isoformat()
    for row in rows:
        # Events intentionally have no uniqueness constraint on identity.  A
        # source may legitimately follow A -> B -> A and the second A must be
        # a new chronological occurrence.
        connection.execute(
            """INSERT INTO temperature_mapping_events
            (effective_at,identity_checksum,payload_json,origin,created_at)
            VALUES (?,?,?,?,?)""",
            (row["effective_at"], row["checksum"], row["payload_json"],
             "v4_mapping_revision", now),
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
        if current is not None and (
            not current_source.get("entity_id")
            or any(field not in current_source for field in ("attribute", "role"))
        ):
            # Legacy rows without source identity are intentionally immutable.
            # A current mapping cannot prove that it represents the same
            # historical sensor, even when the stable source_id is unchanged.
            continue
        if current_source.get("entity_id") and any(
            current_source.get(field) != candidate_source.get(field)
            for field in ("entity_id", "attribute", "role")
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
    expected_mappings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Accept only summaries tied to the exact interval and proven mapping."""
    eligible: dict[str, Any] = {}
    for source_id, summary in (recovered or {}).items():
        if (
            not isinstance(summary, dict)
            or summary.get("period_start_utc") != start
            or summary.get("period_end_utc") != end
            or not isinstance(summary.get("source_provenance"), dict)
            or not summary["source_provenance"].get("entity_id")
        ):
            continue
        if expected_mappings is not None:
            expected = expected_mappings.get(source_id)
            provenance = summary["source_provenance"]
            if not isinstance(expected, dict) or not expected.get("eligible"):
                continue
            if any(
                provenance.get(field) != expected.get(field)
                for field in ("entity_id", "attribute", "role")
            ):
                continue
            try:
                mapping_start = datetime.fromisoformat(
                    str(expected["mapping_effective_at"]).replace("Z", "+00:00")
                )
                interval_start = datetime.fromisoformat(start.replace("Z", "+00:00"))
                interval_end = datetime.fromisoformat(end.replace("Z", "+00:00"))
                mapping_end = (
                    datetime.fromisoformat(
                        str(expected["mapping_effective_end"]).replace("Z", "+00:00")
                    ) if expected.get("mapping_effective_end") else None
                )
            except (KeyError, TypeError, ValueError):
                continue
            if interval_start < mapping_start or (
                mapping_end is not None and interval_end > mapping_end
            ):
                continue
            provenance_effective = provenance.get("mapping_effective_at")
            if provenance_effective is None:
                continue
            try:
                if datetime.fromisoformat(
                    str(provenance_effective).replace("Z", "+00:00")
                ) != mapping_start:
                    continue
            except ValueError:
                continue
        eligible[source_id] = summary
    return eligible


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
                connection.executescript("BEGIN IMMEDIATE;\n" + MIGRATION_4 + MIGRATION_5)
                connection.execute("INSERT INTO schema_version VALUES (?)", (SCHEMA_VERSION,))
            elif row[0] == 1:
                connection.executescript(
                    "BEGIN IMMEDIATE;\n" + MIGRATION_2 + MIGRATION_3 + MIGRATION_4
                )
                _migrate_temperature_storage_v4(connection)
                _migrate_temperature_storage_v5(connection)
                connection.execute("UPDATE schema_version SET version=?", (SCHEMA_VERSION,))
            elif row[0] == 2:
                connection.executescript("BEGIN IMMEDIATE;\n" + MIGRATION_3 + MIGRATION_4)
                _migrate_temperature_storage_v4(connection)
                _migrate_temperature_storage_v5(connection)
                connection.execute("UPDATE schema_version SET version=?", (SCHEMA_VERSION,))
            elif row[0] == 3:
                connection.executescript("BEGIN IMMEDIATE;\n" + MIGRATION_4)
                _migrate_temperature_storage_v4(connection)
                _migrate_temperature_storage_v5(connection)
                connection.execute("UPDATE schema_version SET version=?", (SCHEMA_VERSION,))
            elif row[0] == 4:
                connection.execute("BEGIN IMMEDIATE")
                _migrate_temperature_storage_v5(connection)
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
        with self.connect() as connection:
            prior = connection.execute(
                "SELECT id FROM temperature_mapping_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
        registered = self.ensure_mapping_revision(payload)
        return prior is None or int(registered["mapping_event_id"]) != int(prior["id"])

    def ensure_mapping_revision(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Persist a chronological mapping occurrence, including A -> B -> A."""
        serialized_identity = _canonical_json({
            key: value for key, value in payload.items() if key != "effective_at"
        })
        checksum = _checksum(serialized_identity)
        effective_at = str(payload.get("effective_at") or datetime.now(UTC).isoformat())
        serialized = _canonical_json({**payload, "effective_at": effective_at})
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            latest = connection.execute(
                """SELECT id,identity_checksum,payload_json FROM temperature_mapping_events
                ORDER BY effective_at DESC,id DESC LIMIT 1"""
            ).fetchone()
            if latest and latest["identity_checksum"] == checksum:
                result = json.loads(latest["payload_json"])
                result["mapping_event_id"] = int(latest["id"])
                return result
            cursor = connection.execute(
                """INSERT INTO temperature_mapping_events
                (effective_at,identity_checksum,payload_json,origin,created_at)
                VALUES (?,?,?,?,?)""",
                (effective_at, checksum, serialized, "configuration",
                 datetime.now(UTC).isoformat()),
            )
            # Keep the V4 table readable for downgrade diagnostics.  Its
            # unique checksum cannot represent repeated identities, so V5
            # eligibility never relies on it.
            connection.execute(
                """INSERT OR IGNORE INTO mapping_revisions
                (effective_at,checksum,payload_json) VALUES (?,?,?)""",
                (effective_at, checksum, serialized),
            )
            event_id = int(cursor.lastrowid)
        return {**payload, "effective_at": effective_at, "mapping_event_id": event_id}

    def temperature_mapping_applicability(
        self, source_id: str, start: datetime, end: datetime,
        current_mapping: dict[str, Any], *, allow_historical_identity: bool = False,
    ) -> dict[str, Any]:
        """Prove one temperature mapping applies to the complete UTC interval."""
        if start.tzinfo is None or end.tzinfo is None or end <= start:
            return {"eligible": False, "reason": "invalid_repair_interval"}

        def instant(value: str) -> datetime:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)

        with self.connect() as connection:
            attestation = connection.execute(
                """SELECT id,interval_start_utc,interval_end_utc,payload_json
                FROM temperature_mapping_attestations
                WHERE source_id=? AND interval_start_utc<=? AND interval_end_utc>=?
                AND identity_checksum=? ORDER BY id DESC LIMIT 1""",
                (source_id, start.astimezone(UTC).isoformat(), end.astimezone(UTC).isoformat(),
                 _temperature_identity_checksum(current_mapping)),
            ).fetchone()
            if attestation:
                identity = json.loads(attestation["payload_json"])
                return {
                    "eligible": True, "reason": "mapping_user_attested",
                    **identity, "mapping_event_id": None,
                    "mapping_attestation_id": int(attestation["id"]),
                    "mapping_effective_at": attestation["interval_start_utc"],
                    "mapping_effective_end": attestation["interval_end_utc"],
                }
            rows = connection.execute(
                """SELECT id,effective_at,payload_json
                FROM temperature_mapping_events ORDER BY effective_at,id"""
            ).fetchall()
        revisions = []
        for row in rows:
            try:
                effective = instant(row["effective_at"])
                payload = json.loads(row["payload_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            revisions.append((effective, int(row["id"]), payload))
        applicable = [item for item in revisions if item[0] <= start.astimezone(UTC)]
        if not applicable:
            return {
                "eligible": False, "reason": "mapping_not_recorded_for_interval",
                "source_id": source_id,
            }
        active_effective, active_id, active_payload = max(
            applicable, key=lambda item: (item[0], item[1])
        )
        historical = next((
            item for item in active_payload.get("temperatures", [])
            if item.get("source_id") == source_id
        ), None)
        if not isinstance(historical, dict):
            return {
                "eligible": False, "reason": "source_not_in_historical_mapping",
                "source_id": source_id,
                "mapping_effective_at": active_effective.isoformat(),
            }
        historical_identity = {
            key: historical.get(key) for key in ("entity_id", "attribute", "role", "enabled")
        }
        effective_end = None
        for later_effective, _later_id, later_payload in sorted(
            (item for item in revisions if item[0] > active_effective),
            key=lambda item: (item[0], item[1]),
        ):
            later_mapping = next((
                item for item in later_payload.get("temperatures", [])
                if item.get("source_id") == source_id
            ), None)
            later_identity = (
                {key: later_mapping.get(key) for key in historical_identity}
                if isinstance(later_mapping, dict) else None
            )
            if later_identity != historical_identity:
                effective_end = later_effective
                break
        if effective_end is not None and effective_end < end.astimezone(UTC):
            return {
                "eligible": False, "reason": "mapping_changes_within_interval",
                "source_id": source_id,
                "mapping_effective_at": active_effective.isoformat(),
                "mapping_effective_end": effective_end.isoformat(),
            }
        required_fields = ("entity_id", "attribute", "role")
        for field in required_fields:
            if field not in historical:
                return {
                    "eligible": False,
                    "reason": f"historical_mapping_{field}_unknown",
                    "source_id": source_id,
                    "mapping_effective_at": active_effective.isoformat(),
                }
            if (not allow_historical_identity
                    and historical.get(field) != current_mapping.get(field)):
                return {
                    "eligible": False, "reason": f"mapping_{field}_changed",
                    "source_id": source_id,
                    "mapping_effective_at": active_effective.isoformat(),
                    "historical_value": historical.get(field),
                    "current_value": current_mapping.get(field),
                }
        if historical.get("enabled") is False:
            return {
                "eligible": False, "reason": "source_disabled_for_interval",
                "source_id": source_id,
                "mapping_effective_at": active_effective.isoformat(),
            }
        return {
            "eligible": True, "reason": "mapping_verified",
            "source_id": source_id,
            "entity_id": historical.get("entity_id"),
            "attribute": historical.get("attribute"),
            "role": historical.get("role"),
            "mapping_event_id": active_id,
            "mapping_effective_at": active_effective.isoformat(),
            "mapping_effective_end": effective_end.isoformat() if effective_end else None,
        }

    def attest_temperature_mapping(
        self, source_id: str, start: datetime, end: datetime,
        mapping: dict[str, Any],
    ) -> int:
        """Record an explicit administrator assertion for one bounded interval."""
        if start.tzinfo is None or end.tzinfo is None or end <= start:
            raise ValueError("attestation interval must be timezone-aware and ordered")
        identity = _temperature_identity({**mapping, "source_id": source_id})
        if not identity.get("entity_id") or not identity.get("role"):
            raise ValueError("attestation requires entity and role identity")
        serialized = _canonical_json(identity)
        with self.connect() as connection:
            prior = connection.execute(
                """SELECT id FROM temperature_mapping_attestations
                WHERE source_id=? AND interval_start_utc=? AND interval_end_utc=?
                AND identity_checksum=? LIMIT 1""",
                (source_id, start.astimezone(UTC).isoformat(), end.astimezone(UTC).isoformat(),
                 _temperature_identity_checksum(identity)),
            ).fetchone()
            if prior:
                return int(prior["id"])
            cursor = connection.execute(
                """INSERT INTO temperature_mapping_attestations
                (source_id,interval_start_utc,interval_end_utc,identity_checksum,
                 payload_json,created_at) VALUES (?,?,?,?,?,?)""",
                (source_id, start.astimezone(UTC).isoformat(), end.astimezone(UTC).isoformat(),
                 _temperature_identity_checksum(identity), serialized,
                 datetime.now(UTC).isoformat()),
            )
        return int(cursor.lastrowid)

    def upsert_temperature_history(
        self, record_kind: str, start: datetime, end: datetime, local_date: str,
        timezone: str, source_id: str, summary: dict[str, Any],
        mapping_applicability: dict[str, Any], *, dry_run: bool = False,
    ) -> str:
        """Insert or improve an independent temperature-only interval."""
        if record_kind not in {"hourly", "daily"}:
            raise ValueError("temperature history kind must be hourly or daily")
        if start.tzinfo is None or end.tzinfo is None or end <= start:
            raise ValueError("temperature history interval must be timezone-aware and ordered")
        key = start.astimezone(UTC).isoformat()
        eligible = _eligible_temperature_repair(
            {source_id: summary}, start.astimezone(UTC).isoformat(),
            end.astimezone(UTC).isoformat(), {source_id: mapping_applicability},
        ).get(source_id)
        if eligible is None:
            return "skipped_unverified_mapping_or_evidence"
        now = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            connection.execute("PRAGMA busy_timeout=1000")
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                """SELECT payload_json,checksum FROM temperature_history_records
                WHERE record_kind=? AND record_key=? AND source_id=?""",
                (record_kind, key, source_id),
            ).fetchone()
            if prior:
                current = json.loads(prior["payload_json"])
                merged, changed = _merge_temperature_maps(
                    {source_id: current}, {source_id: eligible}
                )
                if not changed:
                    return "unchanged"
                eligible = merged[source_id]
            serialized = _canonical_json(eligible)
            checksum = _checksum(serialized)
            if dry_run:
                return "updated" if prior else "inserted"
            connection.execute(
                """INSERT INTO temperature_history_records
                (record_kind,record_key,interval_start_utc,interval_end_utc,
                 local_date,timezone,source_id,mapping_event_id,payload_json,
                 checksum,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(record_kind,record_key,source_id) DO UPDATE SET
                interval_end_utc=excluded.interval_end_utc,
                local_date=excluded.local_date,timezone=excluded.timezone,
                mapping_event_id=excluded.mapping_event_id,
                payload_json=excluded.payload_json,checksum=excluded.checksum,
                updated_at=excluded.updated_at""",
                (record_kind, key, start.astimezone(UTC).isoformat(),
                 end.astimezone(UTC).isoformat(), local_date, timezone, source_id,
                 mapping_applicability.get("mapping_event_id"), serialized, checksum, now),
            )
        return "updated" if prior else "inserted"

    def temperature_history_range(
        self, record_kind: str, start: datetime, end: datetime,
    ) -> list[dict[str, Any]]:
        """Return grouped independent summaries for a bounded UTC interval."""
        if record_kind not in {"hourly", "daily"}:
            raise ValueError("temperature history kind must be hourly or daily")
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT record_key,interval_start_utc,interval_end_utc,local_date,
                timezone,source_id,payload_json,updated_at
                FROM temperature_history_records
                WHERE record_kind=? AND interval_start_utc>=? AND interval_end_utc<=?
                ORDER BY interval_start_utc,source_id""",
                (record_kind, start.astimezone(UTC).isoformat(),
                 end.astimezone(UTC).isoformat()),
            ).fetchall()
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            item = grouped.setdefault(row["record_key"], {
                "local_date": row["local_date"], "timezone": row["timezone"],
                "period_start_utc": row["interval_start_utc"],
                "period_end_utc": row["interval_end_utc"],
                "day_seconds": int((
                    datetime.fromisoformat(row["interval_end_utc"])
                    - datetime.fromisoformat(row["interval_start_utc"])
                ).total_seconds()),
                "household_kwh": None, "pv_kwh": None, "import_kwh": None,
                "export_kwh": None, "non_hvac_kwh": None,
                "quality": "partial", "temperatures": {}, "allocations": {},
                "temperature_only": True,
                "provenance": {"source": "temperature_history_v5"},
                "updated_at": row["updated_at"],
            })
            item["temperatures"][row["source_id"]] = json.loads(row["payload_json"])
            if row["updated_at"] > item["updated_at"]:
                item["updated_at"] = row["updated_at"]
        return list(grouped.values())

    def temperature_history_counts(
        self, start: datetime, end: datetime, source_id: str | None,
    ) -> dict[str, int]:
        source_clause = " AND source_id=?" if source_id is not None else ""
        arguments: list[Any] = [
            start.astimezone(UTC).isoformat(), end.astimezone(UTC).isoformat(),
        ]
        if source_id is not None:
            arguments.append(source_id)
        with self.connect() as connection:
            rows = connection.execute(
                f"""SELECT record_kind,COUNT(*) AS count
                FROM temperature_history_records
                WHERE interval_start_utc>=? AND interval_end_utc<=?{source_clause}
                GROUP BY record_kind""",
                arguments,
            ).fetchall()
        result = {"hourly": 0, "daily": 0}
        result.update({str(row["record_kind"]): int(row["count"]) for row in rows})
        return result

    def energy_record_counts(
        self, start: datetime, end: datetime, start_date: str, end_date: str,
    ) -> dict[str, int]:
        with self.connect() as connection:
            hourly = int(connection.execute(
                """SELECT COUNT(*) FROM hourly_records
                WHERE interval_start_utc>=? AND interval_end_utc<=?""",
                (start.astimezone(UTC).isoformat(), end.astimezone(UTC).isoformat()),
            ).fetchone()[0])
            daily = int(connection.execute(
                """SELECT COUNT(*) FROM daily_records
                WHERE local_date>=? AND local_date<=?""", (start_date, end_date),
            ).fetchone()[0])
        return {"hourly": hourly, "daily": daily}

    def create_temperature_backfill_job(
        self, start_date: str, end_date: str, *, dry_run: bool,
        details: dict[str, Any] | None = None,
    ) -> int:
        now = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            cursor = connection.execute(
                """INSERT INTO temperature_backfill_jobs
                (start_date,end_date,cursor_date,dry_run,status,cancel_requested,
                 details_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                (start_date, end_date, start_date, int(dry_run), "queued", 0,
                 _canonical_json(details or {}), now, now),
            )
        return int(cursor.lastrowid)

    def update_temperature_backfill_job(
        self, job_id: int, *, status: str | None = None,
        cursor_date: str | None = None, details: dict[str, Any] | None = None,
    ) -> None:
        assignments = ["updated_at=?"]
        args: list[Any] = [datetime.now(UTC).isoformat()]
        if status is not None:
            assignments.append("status=?"); args.append(status)
        if cursor_date is not None:
            assignments.append("cursor_date=?"); args.append(cursor_date)
        if details is not None:
            assignments.append("details_json=?"); args.append(_canonical_json(details))
        args.append(job_id)
        with self.connect() as connection:
            connection.execute(
                f"UPDATE temperature_backfill_jobs SET {','.join(assignments)} WHERE id=?",
                args,
            )

    def temperature_backfill_job(self, job_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM temperature_backfill_jobs WHERE id=?", (job_id,)
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["dry_run"] = bool(result["dry_run"])
        result["cancel_requested"] = bool(result["cancel_requested"])
        result["details"] = json.loads(result.pop("details_json"))
        return result

    def request_temperature_backfill_cancel(self, job_id: int) -> bool:
        with self.connect() as connection:
            updated = connection.execute(
                """UPDATE temperature_backfill_jobs
                SET cancel_requested=1,updated_at=?
                WHERE id=? AND status IN ('queued','running','interrupted')""",
                (datetime.now(UTC).isoformat(), job_id),
            )
        return updated.rowcount == 1

    def resumable_temperature_backfill_jobs(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT id FROM temperature_backfill_jobs
                WHERE status IN ('queued','running','interrupted')
                AND cancel_requested=0 ORDER BY id"""
            ).fetchall()
        return [self.temperature_backfill_job(int(row["id"])) for row in rows]

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

    def _daily_with_temperature(
        self, connection: sqlite3.Connection, local_date: str,
        energy_row: sqlite3.Row | None = None,
    ) -> dict[str, Any] | None:
        temperature_rows = connection.execute(
            """SELECT interval_start_utc,interval_end_utc,timezone,source_id,
            payload_json,updated_at FROM temperature_history_records
            WHERE record_kind='daily' AND local_date=? ORDER BY source_id""",
            (local_date,),
        ).fetchall()
        if energy_row is None:
            energy_row = connection.execute(
                """SELECT payload_json,revision,updated_at FROM daily_records
                WHERE local_date=?""", (local_date,),
            ).fetchone()
        if energy_row is None and not temperature_rows:
            return None
        if energy_row is not None:
            item = _hydrate_payload(
                connection, energy_row["payload_json"], "daily", local_date
            )
            item.update(
                revision=int(energy_row["revision"]), updated_at=energy_row["updated_at"]
            )
        else:
            first = temperature_rows[0]
            item = {
                "local_date": local_date, "timezone": first["timezone"],
                "period_start_utc": first["interval_start_utc"],
                "period_end_utc": first["interval_end_utc"],
                "day_seconds": int((
                    datetime.fromisoformat(first["interval_end_utc"])
                    - datetime.fromisoformat(first["interval_start_utc"])
                ).total_seconds()),
                "household_kwh": None, "pv_kwh": None, "import_kwh": None,
                "export_kwh": None, "non_hvac_kwh": None,
                "quality": "partial", "temperatures": {}, "allocations": {},
                "temperature_only": True, "revision": 0,
                "provenance": {"source": "temperature_history_v5"},
                "updated_at": max(row["updated_at"] for row in temperature_rows),
            }
        recovered = {
            row["source_id"]: json.loads(row["payload_json"])
            for row in temperature_rows
        }
        if recovered:
            item["temperatures"], _changed = _merge_temperature_maps(
                item.get("temperatures", {}), recovered
            )
            item.setdefault("provenance", {})["temperature_history_overlay"] = True
        return item

    def history(self, *, limit: int = 90, before: str | None = None) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 3660))
        sql = """SELECT local_date FROM (
        SELECT local_date FROM daily_records UNION
        SELECT local_date FROM temperature_history_records WHERE record_kind='daily'
        )"""
        args: list[Any] = []
        if before:
            sql += " WHERE local_date < ?"
            args.append(before)
        sql += " ORDER BY local_date DESC LIMIT ?"
        args.append(limit)
        with self.connect() as connection:
            rows = connection.execute(sql, args).fetchall()
            result = [
                item for row in rows
                if (item := self._daily_with_temperature(connection, row["local_date"]))
                is not None
            ]
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
        union = """(SELECT local_date FROM daily_records UNION
        SELECT local_date FROM temperature_history_records WHERE record_kind='daily')"""
        with self.connect() as connection:
            total = int(connection.execute(
                f"SELECT COUNT(*) FROM {union}{where}", args
            ).fetchone()[0])
            rows = connection.execute(
                f"SELECT local_date FROM {union}{where} "
                "ORDER BY local_date DESC LIMIT ? OFFSET ?", [*args, limit, offset],
            ).fetchall()
            result = [
                item for row in rows
                if (item := self._daily_with_temperature(connection, row["local_date"]))
                is not None
            ]
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
                """SELECT local_date FROM (
                SELECT local_date FROM daily_records UNION
                SELECT local_date FROM temperature_history_records WHERE record_kind='daily'
                ) WHERE local_date>=? AND local_date<=? ORDER BY local_date""",
                (start_date, end_date),
            ).fetchall()
            result = [
                item for row in rows
                if (item := self._daily_with_temperature(connection, row["local_date"]))
                is not None
            ]
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
                        (provenance or {}).get("mapping_applicability"),
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
                        (provenance or {}).get("mapping_applicability"),
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
        """Load energy hours and overlay independent temperature evidence."""
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
        independent = self.temperature_history_range("hourly", start, end)
        independent_by_start = {
            item["period_start_utc"]: item for item in independent
        }
        for item in result:
            extra = independent_by_start.get(item["period_start_utc"])
            if not extra:
                continue
            merged, _changed = _merge_temperature_maps(
                item.get("temperatures", {}), extra.get("temperatures", {})
            )
            item["temperatures"] = merged
            item.setdefault("provenance", {})["temperature_history_overlay"] = True
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
            return self._daily_with_temperature(connection, local_date)

    def daily_bounds(self) -> tuple[str | None, str | None]:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT MIN(local_date),MAX(local_date) FROM (
                SELECT local_date FROM daily_records UNION
                SELECT local_date FROM temperature_history_records WHERE record_kind='daily'
                )"""
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

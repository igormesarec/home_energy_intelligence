"""Bounded, administrator-invoked read-only historical evidence export."""
from __future__ import annotations

import json
from bisect import bisect_left
from datetime import UTC, datetime, timedelta
from functools import partial

from .configuration import equipment_from_config
from .const import CONF_INFLUX_DATABASE, CONF_INFLUX_ENABLED, CONF_INFLUX_HOST, CONF_INFLUX_PASSWORD, CONF_INFLUX_PORT, CONF_INFLUX_USERNAME
from .influx import InfluxBackfillClient
from .timeutils import local_day_bounds

MAX_OBSERVATIONS = 10000
MAX_BYTES = 2_000_000
PROVENANCE_FIELDS = ("energy_entity_id", "state_entity_id", "fallback_entity", "historical_mode_source", "meter_coverage_seconds", "meter_expected_seconds", "meter_evidence_status", "first_observation_utc", "last_observation_utc", "missing_intervals", "conflicting_timestamps_utc", "boundary_policy", "strict_influx_identity", "historical_mapping_quality", "mapping_attested")
RECORD_FIELDS = ("local_date", "source_key", "source_entity_id", "interval_start_utc", "interval_end_utc", "meter_kwh", "categories", "residual_kwh", "meter_coverage_seconds", "classification_coverage_seconds", "quality", "reset_count", "rejected_count", "counter_source", "verification_status", "historical_mapping_quality", "calculation_version")


class EvidenceExportLimit(ValueError):
    """A bounded export cannot include all requested observations."""


async def build_evidence_export(hass, coordinator, start, end, equipment_id, state_entity):
    """Collect a limited single-equipment export without starting jobs or writing data."""
    if end < start or (end-start).days > 30:
        raise ValueError("Range must be ordered and at most 31 local days")
    devices = [x for x in equipment_from_config(coordinator.config) if x.enabled and x.kind == "heat_pump" and x.equipment_id == equipment_id]
    if len(devices) != 1:
        raise ValueError("Select one configured enabled heat pump")
    if not isinstance(state_entity, str) or len(state_entity) > 255 or not state_entity.startswith(("sensor.", "climate.", "binary_sensor.")) or not all(c.isalnum() or c in "_." for c in state_entity):
        raise ValueError("Select a valid operating-state entity")
    device = devices[0]
    data = await hass.async_add_executor_job(partial(coordinator.database.electricity_recovery_evidence_range, start.isoformat(), end.isoformat(), read_only=True))
    key = f"equipment.{equipment_id}"
    records = []
    for row in data.get("records", []):
        if row.get("source_key") != key:
            continue
        safe = {name: row.get(name) for name in RECORD_FIELDS}
        safe["unknown_kwh"] = (row.get("categories") or {}).get("unknown")
        applicability = row.get("mapping_applicability") or {}
        safe["mapping_applicability"] = {name: applicability.get(name) for name in ("status", "source_entity_id", "effective_start_utc", "effective_end_utc")}
        provenance = row.get("source_provenance") or {}
        safe["source_provenance"] = {name: provenance.get(name) for name in PROVENANCE_FIELDS if name in provenance}
        if "missing_intervals" in safe["source_provenance"]:
            safe["source_provenance"]["missing_intervals"] = [{k: interval.get(k) for k in ("start", "end", "reason")} for interval in (provenance["missing_intervals"] or []) if isinstance(interval, dict)]
        records.append(safe)
    ambiguous = [{"local_date": row.get("local_date"), "reason": row.get("reason", "conflicting_evidence"), "versions": row.get("versions")} for row in data.get("ambiguous", []) if row.get("source_key") == key]
    recorder, influx_rows, errors, daily_availability = [], [], [], []
    influx = None
    cfg = coordinator.config
    if cfg.get(CONF_INFLUX_ENABLED) and cfg.get(CONF_INFLUX_HOST) and cfg.get(CONF_INFLUX_DATABASE):
        influx = InfluxBackfillClient(host=cfg[CONF_INFLUX_HOST], port=int(cfg.get(CONF_INFLUX_PORT, 8086)), database=cfg[CONF_INFLUX_DATABASE], username=cfg.get(CONF_INFLUX_USERNAME), password=cfg.get(CONF_INFLUX_PASSWORD), use_ssl=bool(cfg.get("influx_use_ssl", False)), verify_ssl=bool(cfg.get("influx_verify_ssl", True)), retention_policy=str(cfg.get("influx_retention_policy", "autogen")))
    day = start
    while day <= end:
        begin, finish = local_day_bounds(day, coordinator.timezone)
        before_recorder, before_influx = len(recorder), len(influx_rows)
        try:
            history = await coordinator.reader.async_history([state_entity], begin, finish)
            for state in history.get(state_entity, []):
                stamp = state.last_updated
                if stamp.tzinfo is None or not begin <= stamp < finish:
                    continue
                attributes = {name: state.attributes[name] for name in ("hvac_action", "operation_mode", "operation_status") if name in state.attributes and isinstance(state.attributes[name], (str, int, float, bool))}
                if len(str(state.state)) > 512 or any(len(str(value)) > 512 for value in attributes.values()):
                    raise EvidenceExportLimit("State value too large for diagnostic export")
                recorder.append({"entity_id": state_entity, "timestamp": stamp.astimezone(UTC).isoformat(), "state": str(state.state), "attributes": attributes})
                if len(recorder) + len(influx_rows) > MAX_OBSERVATIONS:
                    raise EvidenceExportLimit("Observation limit exceeded; select a shorter range")
        except EvidenceExportLimit:
            raise
        except Exception as error:
            errors.append({"date": day.isoformat(), "source": "recorder", "error": type(error).__name__})
        if influx:
            try:
                if len(recorder) + len(influx_rows) >= MAX_OBSERVATIONS:
                    raise EvidenceExportLimit("Observation limit exceeded; select a shorter range")
                rows = await hass.async_add_executor_job(partial(influx.diagnostic_states_ns, begin, finish, entity_id=state_entity, limit=MAX_OBSERVATIONS-len(recorder)-len(influx_rows)))
                influx_rows.extend({"entity_id": state_entity, "measurement": "state", "field": "state", "timestamp_ns": str(stamp), "value": value} for stamp, value in rows)
                if len(recorder) + len(influx_rows) > MAX_OBSERVATIONS:
                    raise EvidenceExportLimit("Observation limit exceeded; select a shorter range")
            except EvidenceExportLimit:
                raise
            except Exception as error:
                if "limit" in str(error).lower():
                    raise EvidenceExportLimit("Observation limit exceeded; select a shorter range") from error
                errors.append({"date": day.isoformat(), "source": "influx", "error": type(error).__name__})
        daily_availability.append({"local_date": day.isoformat(), "period_start_utc": begin.isoformat(), "period_end_utc": finish.isoformat(), "recorder_observations": len(recorder)-before_recorder, "influx_observations": len(influx_rows)-before_influx if influx else None, "coverage": "unknown"})
        day += timedelta(days=1)
    # Retain both observations; comparisons are explanatory only.
    origin = datetime(1970, 1, 1, tzinfo=UTC)
    by_ns = {}
    for item in recorder:
        stamp = datetime.fromisoformat(item["timestamp"])
        delta = stamp-origin
        ns = (delta.days*86400 + delta.seconds)*1_000_000_000 + delta.microseconds*1000
        by_ns.setdefault(ns, set()).add(item["state"])
    exact = []
    for ns, values in by_ns.items():
        if len(values) > 1:
            exact.append({"timestamp_ns": str(ns), "recorder_states": sorted(values)})
    for item in influx_rows:
        states = by_ns.get(int(item["timestamp_ns"]))
        if states and item["value"] not in states:
            exact.append({"timestamp_ns": item["timestamp_ns"], "recorder_states": sorted(states), "influx_value": item["value"]})
    influx_by_ns = {}
    for item in influx_rows:
        influx_by_ns.setdefault(int(item["timestamp_ns"]), set()).add(item["value"])
    for ns, values in influx_by_ns.items():
        if len(values) > 1:
            exact.append({"timestamp_ns": str(ns), "influx_values": sorted(values)})
    timestamps = sorted(influx_by_ns)
    near = []
    for ns, states in by_ns.items():
        index = bisect_left(timestamps, ns)
        for position in (index-1, index, index+1):
            if 0 <= position < len(timestamps):
                other = timestamps[position]
                if 0 < abs(other-ns) <= 1_000_000_000 and states != influx_by_ns[other]:
                    near.append({"recorder_timestamp_ns": str(ns), "influx_timestamp_ns": str(other), "recorder_states": sorted(states), "influx_values": sorted(influx_by_ns[other])})
    candidates = []
    if device.state_entity:
        candidates.append({"entity_id": device.state_entity, "source": "configured operating state"})
    try:
        mappings = json.loads(cfg.get("influx_mapping_json", "{}") or "{}")
        for role in ("fallback_state", "actual_dhw_state"):
            mapping = mappings.get(f"equipment.{equipment_id}.{role}")
            if isinstance(mapping, dict) and isinstance(mapping.get("entity_id"), str):
                candidates.append({"entity_id": mapping["entity_id"], "source": f"configured {role}"})
    except (TypeError, ValueError, AttributeError):
        pass
    report = {"period": {"start_date": start.isoformat(), "end_date": end.isoformat(), "timezone": coordinator.timezone}, "equipment_id": equipment_id, "configured_energy_entity_id": device.energy_entity, "configured_state_entity_id": device.state_entity, "state_entity_id": state_entity, "persisted_recovered_records": records, "ambiguous_records": ambiguous, "daily_availability": daily_availability, "recorder_observations": recorder, "influx_observations": influx_rows, "exact_conflicts": exact, "near_timestamp_differences": near, "influx_status": "queried" if influx else "not configured", "errors": errors, "status": "incomplete", "limitations": ["Influx timestamps use original nanosecond integers; Recorder timestamps have microsecond precision.", "Daily counts are not coverage: missing or stale intervals and preceding-state freshness remain unknown.", "Near differences compare nearest timestamps within one second, not every possible pair.", "Only selected entity and whitelisted attributes are exported."]}
    report["candidate_state_entities"] = candidates
    report["summary"] = {"recovered_source_days": len(records), "ambiguous_source_days": len(ambiguous), "recorder_observations": len(recorder), "influx_observations": len(influx_rows), "exact_conflicts": len(exact), "near_differences": len(near)}
    serialized = json.dumps(report, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    if len(serialized.encode("utf-8")) > MAX_BYTES:
        raise ValueError("Output size limit exceeded; select a shorter range")
    return serialized

"""Bounded, administrator-invoked read-only historical evidence export."""
from __future__ import annotations

import json
from bisect import bisect_left
from datetime import UTC, datetime, timedelta
from functools import partial

from .configuration import equipment_from_config
from .power_pattern_diagnostic import classify_power, power_statistics, validate_power_model
from .const import CONF_INFLUX_DATABASE, CONF_INFLUX_ENABLED, CONF_INFLUX_HOST, CONF_INFLUX_PASSWORD, CONF_INFLUX_PORT, CONF_INFLUX_USERNAME
from .influx import InfluxBackfillClient
from .timeutils import local_day_bounds

MAX_OBSERVATIONS = 10000
MAX_BYTES = 2_000_000
MAX_TRACE_BYTES = 8_000_000
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


async def build_increment_trace_export(hass, coordinator, dates, equipment_id, state_entity):
    """Run the existing strict preview for up to four dates without persisting it."""
    if not dates or len(dates) > 4 or len(set(dates)) != len(dates):
        raise ValueError("Select one to four distinct local dates")
    devices = [item for item in equipment_from_config(coordinator.config)
               if item.enabled and item.kind == "heat_pump" and item.equipment_id == equipment_id]
    if len(devices) != 1 or not devices[0].energy_entity or devices[0].state_entity != state_entity:
        raise ValueError("Select a heat pump with matching configured energy and operating-state entities")
    days = []
    for day in sorted(dates):
        preview = await coordinator.async_preview_historical_energy(day, evidence_only=True, diagnostic_device=equipment_id)
        raw = json.loads(await build_evidence_export(hass, coordinator, day, day, equipment_id, state_entity))
        allocation = preview.get("allocations", {}).get(equipment_id)
        diagnostic = preview.get("increment_diagnostic", {})
        begin, _ = local_day_bounds(day, coordinator.timezone)
        lookback = begin - timedelta(seconds=float(diagnostic.get("freshness_seconds", 3600)))
        boundary = {"recorder": None, "influx": None, "lookback_start_utc": lookback.isoformat()}
        try:
            preceding = await coordinator.reader.async_history([state_entity], lookback, begin)
            candidates = [item for item in preceding.get(state_entity, []) if item.last_updated.tzinfo is not None and lookback <= item.last_updated < begin]
            if len(candidates) > MAX_OBSERVATIONS:
                raise EvidenceExportLimit("Boundary observation limit exceeded")
            if candidates:
                item = max(candidates, key=lambda state: state.last_updated)
                boundary["recorder"] = {"timestamp": item.last_updated.isoformat(), "state": str(item.state)}
        except EvidenceExportLimit:
            raise
        except Exception as error:
            boundary["recorder_error"] = type(error).__name__
        cfg = coordinator.config
        if cfg.get(CONF_INFLUX_ENABLED) and cfg.get(CONF_INFLUX_HOST) and cfg.get(CONF_INFLUX_DATABASE):
            try:
                client = InfluxBackfillClient(host=cfg[CONF_INFLUX_HOST], port=int(cfg.get(CONF_INFLUX_PORT, 8086)), database=cfg[CONF_INFLUX_DATABASE], username=cfg.get(CONF_INFLUX_USERNAME), password=cfg.get(CONF_INFLUX_PASSWORD), use_ssl=bool(cfg.get("influx_use_ssl", False)), verify_ssl=bool(cfg.get("influx_verify_ssl", True)), retention_policy=str(cfg.get("influx_retention_policy", "autogen")))
                observations = await hass.async_add_executor_job(partial(client.diagnostic_states_ns, lookback, begin, entity_id=state_entity, limit=MAX_OBSERVATIONS))
                if observations:
                    stamp = max(item[0] for item in observations)
                    boundary["influx"] = [{"timestamp_ns": str(t), "state": value} for t, value in observations if t == stamp]
            except Exception as error:
                if "limit" in str(error).lower():
                    raise EvidenceExportLimit("Boundary observation limit exceeded") from error
                boundary["influx_error"] = type(error).__name__
        else:
            boundary["influx_error"] = "not_configured"
        # Collect each configured source independently; never merge raw Recorder and Influx evidence.
        state_sources = {"primary": {"entity_id": state_entity,
                         "evidence": {key: raw.get(key) for key in ("recorder_observations", "influx_observations", "influx_status", "errors", "limitations")}}}
        for role in ("fallback", "actual_dhw"):
            entity = diagnostic.get("mode_source_entities", {}).get(role)
            if not entity:
                state_sources[role] = {"entity_id": None, "status": "not_configured_or_not_validated"}
                continue
            extra = json.loads(await build_evidence_export(hass, coordinator, day, day, equipment_id, entity))
            state_sources[role] = {"entity_id": entity, "evidence": {key: extra.get(key) for key in
                ("recorder_observations", "influx_observations", "influx_status", "errors", "limitations")}}
        observation_count = sum(len((info.get("evidence") or {}).get(key) or []) for info in state_sources.values()
                                for key in ("recorder_observations", "influx_observations"))
        observation_count += sum(len(items) for items in diagnostic.get("selected_source_observations", {}).values())
        if observation_count + len(diagnostic.get("trace", [])) + len(diagnostic.get("counter_observations", [])) > MAX_OBSERVATIONS:
            raise EvidenceExportLimit("Diagnostic observation limit exceeded; select fewer dates")
        days.append({"local_date": day.isoformat(), "diagnostic": diagnostic,
                     "allocation": {key: allocation.get(key) for key in ("meter_kwh", "categories", "residual_kwh", "classification_coverage_seconds", "classification_expected_seconds", "reset_count", "rejected_count", "quality", "authoritative_counter_source")} if allocation else None,
                     "state_evidence": {key: raw.get(key) for key in ("recorder_observations", "influx_observations", "influx_status", "errors", "limitations")},
                     "state_sources": state_sources,
                     "preceding_state_evidence": boundary,
                     "preview_errors": [{"source": error.get("source"), "error": error.get("error")} for error in preview.get("errors", []) if str(error.get("source", "")).startswith(f"equipment.{equipment_id}.")]})
    # Totals are available only when every selected day produced the corresponding evidence.
    scenario_totals = {}
    for name in ("baseline", "supported_continuity", "fallback_120m", "fallback_180m", "recorder_hvac_action"):
        reports = [(day.get("diagnostic", {}).get("scenario_comparison") or {}).get(name) for day in days]
        fields = ("meter_kwh", "unknown_kwh", "heating_assumed_kwh", "confirmed_heating_kwh", "dhw_kwh", "idle_kwh", "unsupported_transition_kwh", "covered_seconds", "period_seconds")
        totals = {field: sum(report[field] for report in reports) if all(report and report.get(field) is not None for report in reports) else None for field in fields}
        totals["classification_coverage_percent"] = (100 * totals["covered_seconds"] / totals["period_seconds"]) if totals["period_seconds"] else None
        totals["reconciles"] = all(report and report.get("reconciles") is True for report in reports) if all(report and report.get("reconciles") is not None for report in reports) else None
        scenario_totals[name] = totals
    for name, report in scenario_totals.items():
        report["difference_from_baseline_kwh"] = {key: report[key] - scenario_totals["baseline"][key] if report[key] is not None and scenario_totals["baseline"][key] is not None else None for key in ("unknown_kwh", "heating_assumed_kwh", "confirmed_heating_kwh", "dhw_kwh", "unsupported_transition_kwh")}
    continuity_totals = {}
    for key in ("supported_kwh", "plausible_unverified_kwh", "conflicting_or_insufficient_kwh"):
        reports = [((day.get("diagnostic", {}).get("continuity_analysis") or {}).get("totals") or {}).get(key) for day in days]
        continuity_totals[key] = sum(reports) if all(value is not None for value in reports) else None
    power_days = []
    reference_intervals = []
    exclusion_counts = {}
    pooled = {role: {"interval_count": 0, "segments": [], "expected_seconds": 0, "gaps": [], "observations": set()}
              for role in ("confirmed_heating", "dhw", "idle", "cooling")}
    for day in days:
        analysis = day.get("diagnostic", {}).pop("power_pattern_analysis", None)
        if analysis:
            reference_intervals.extend(analysis.pop("_episodes", []))
            for reason, count in analysis.get("reference_exclusions", {}).items():
                exclusion_counts[reason] = exclusion_counts.get(reason, 0) + count
            for role, ref in analysis["references"].items():
                pooled[role]["segments"].extend(ref.pop("_segments"))
                pooled[role]["gaps"].extend(ref.pop("_gaps"))
                pooled[role]["observations"].update(tuple(item) for item in ref.pop("_observations"))
                pooled[role]["expected_seconds"] += ref.pop("_expected_seconds")
                pooled[role]["interval_count"] += ref["interval_count"]
        baseline_unknown = ((day.get("diagnostic", {}).get("scenario_comparison") or {}).get("baseline") or {}).get("unknown_kwh")
        partition = (sum(analysis["candidate_totals"].values()) if analysis and
                     all(value is not None for value in analysis["candidate_totals"].values()) else None)
        power_days.append({"local_date": day["local_date"], "analysis": analysis,
                           "unknown_partition_reconciles": abs(partition-baseline_unknown) < 1e-7 if partition is not None and baseline_unknown is not None else None})
    power_refs = {role: {"interval_count": ref["interval_count"], "sample_count": len(ref["observations"]), "segment_count": len(ref["segments"]),
        "statistics": power_statistics(ref["segments"], ref["expected_seconds"], ref["gaps"]),
        "mapping_quality": "unverified; exploratory only"} for role, ref in pooled.items()}
    # Existing preview mappings are unverified, so only independently verified
    # reference rows may ever reach the held-out validation framework.
    model = validate_power_model(reference_intervals)
    labels = {"confirmed_heating": "heating_compatible", "dhw": "dhw_compatible", "idle": "idle_compatible"}
    for day in power_days:
        analysis = day["analysis"]
        if not analysis or model["verdict"] != "VALIDATED":
            continue
        for candidate in analysis["candidates"]:
            if candidate["power"]["coverage_percent"] != 100 or candidate["potential_conflict"]:
                continue
            category = classify_power(candidate["power"]["mean_w"], model["ranges_w"])
            label = labels.get(category)
            if label:
                amount = candidate["meter_kwh"]
                analysis["candidate_totals"][candidate["candidate"] + "_kwh"] -= amount
                analysis["candidate_totals"][label + "_kwh"] += amount
                candidate.update({"candidate": label, "validation_status": "VALIDATED",
                                  "reason": "unique match to independently validated reference range; diagnostic only"})
    power_totals = {}
    for key in ("heating_compatible_kwh", "dhw_compatible_kwh", "idle_compatible_kwh", "ambiguous_kwh", "insufficient_evidence_kwh"):
        values = [((day["analysis"] or {}).get("candidate_totals") or {}).get(key) for day in power_days]
        power_totals[key] = sum(values) if all(value is not None for value in values) else None
    def overlap(first, second):
        a, b = power_refs[first]["statistics"], power_refs[second]["statistics"]
        if any(value is None for value in (a["p10_w"], a["p90_w"], b["p10_w"], b["p90_w"])):
            return None
        return {"ranges_overlap": max(a["p10_w"], b["p10_w"]) <= min(a["p90_w"], b["p90_w"]),
                "quality": "descriptive only; unverified references"}
    power_validation = {"verdict": model["verdict"], "references": power_refs, "days": power_days,
        "reference_eligibility": {"mapping_verified_intervals": sum(bool(row.get("mapping_verified")) for row in reference_intervals),
            "mapping_unverified_intervals": sum(not row.get("mapping_verified") for row in reference_intervals), "exclusions": exclusion_counts},
        "candidate_totals": power_totals, "unknown_partition_reconciles": all(day["unknown_partition_reconciles"] is True for day in power_days) if all(day["unknown_partition_reconciles"] is not None for day in power_days) else None, "validated_recovery_kwh": 0 if all(day["analysis"] and day["analysis"]["validated_recovery_kwh"] is not None for day in power_days) else None,
        "validation": {**model, "split_method": "chronological, complete consecutive episodes",
            "overlap": {"heating_vs_dhw": overlap("confirmed_heating", "dhw"),
                        "heating_vs_idle": overlap("confirmed_heating", "idle"),
                        "heating_vs_cooling": overlap("confirmed_heating", "cooling")}},
        "limitations": ["All reference intervals are exploratory and mapping validity is unverified.",
            "No candidate can be assigned heating, DHW or idle from power alone.",
            "Power observations require two valid samples within a 15-minute diagnostic gap; missing intervals are never filled.",
            "Commissioning and historical source validity were not independently established."]}
    output = json.dumps({"power_pattern_validation": power_validation, "continuity_totals": continuity_totals, "scenario_totals": scenario_totals, "timezone": coordinator.timezone, "equipment_id": equipment_id,
                         "energy_entity": devices[0].energy_entity, "state_entity": state_entity,
                         "evidence_only": True, "mapping_quality": "unverified historical mapping",
                         "days": days}, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    if len(output.encode("utf-8")) > MAX_TRACE_BYTES:
        raise EvidenceExportLimit("Diagnostic output size limit exceeded; select fewer dates")
    return output

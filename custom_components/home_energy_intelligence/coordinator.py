"""Collection, catch-up, and daily-close orchestration."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, replace
from datetime import UTC, date, datetime, timedelta
from functools import partial
import json
import hashlib
import logging
from math import isfinite
from time import monotonic
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .allocation import (allocate_interval, allocate_historical_counter_increments,
                         configurable_classifier, counter_delta, historical_counter_observations)
from .configuration import (
    equipment_from_config, equipment_temperature_mappings, mapping_revision,
    temperatures_from_config,
)
from .const import (
    CONF_CYCLE_HOURS, CONF_INFLUX_DATABASE, CONF_INFLUX_ENABLED, CONF_INFLUX_HOST,
    CONF_INFLUX_MAPPING, CONF_INFLUX_PASSWORD, CONF_INFLUX_PORT, CONF_INFLUX_USERNAME,
    CONF_SEASONAL_FALLBACK, CONF_TEMPERATURE_FRESHNESS_MINUTES,
)
from .database import EnergyDatabase
from .electricity_recovery import prepare_recovery_day
from .engine import DailyAggregator, DailyInput, DeviceHistory
from .influx import InfluxBackfillClient
from .models import NumericSample, StateSample
from .historical_modes import explain_historical_mode, historical_heat_pump_modes, historical_selected_modes
from .historical_simulation import continuity_candidates, simulate_modes
from .power_pattern_diagnostic import analyze_power
from .recorder import (
    RecorderReader, states_to_modes, states_to_numeric,
    statistics_to_counter_samples, statistics_to_numeric_samples,
)
from .temperature import DEFAULT_LIMITS, time_weighted_temperature
from .timeutils import local_day_bounds

LEGACY_SITE_KEYS = {"pv": "pv_energy", "import": "grid_import", "export": "grid_export"}
_LOGGER = logging.getLogger(__name__)


def _merge_preview_counters(recorder, archive, *, identity_verified):
    """Combine only independently matching observations of the same counter."""
    if not recorder:
        return archive, "influx", None
    if not archive:
        return recorder, "recorder", None
    _, recorder_conflicts = historical_counter_observations(recorder)
    _, archive_conflicts = historical_counter_observations(archive)
    if recorder_conflicts or archive_conflicts:
        return recorder, "recorder", "counter_source_timestamp_conflict"
    left = {s.timestamp: s for s in recorder}
    right = {s.timestamp: s for s in archive}
    overlap = left.keys() & right.keys()
    if not identity_verified:
        return recorder, "recorder", "counter_source_identity_unverified"
    if not overlap:
        return recorder, "recorder", "counter_overlap_unverified"
    if any(left[t].value is None or right[t].value is None or
           abs(left[t].value - right[t].value) > 1e-6 for t in overlap):
        return recorder, "recorder", "counter_overlap_conflict"
    return sorted({**right, **left}.values(), key=lambda s: s.timestamp), "recorder_and_influx", None


def _validated_temperature_coverage(
    samples: list[NumericSample], start: datetime, end: datetime, mapping,
) -> float:
    kind = (
        "outdoor" if mapping.role == "outdoor" else
        "flow" if mapping.role in {"supply", "return"} else
        "dhw" if mapping.role == "dhw" else "indoor"
    )
    configured = mapping.quality
    minimum, maximum = (
        (configured.minimum, configured.maximum) if configured else DEFAULT_LIMITS[kind]
    )
    return time_weighted_temperature(
        samples, start, end, minimum_valid=minimum, maximum_valid=maximum,
        max_carry=(timedelta(minutes=configured.freshness_minutes)
                   if configured else None),
        invalid_values=(configured.invalid_values if configured else (-55.0, 155.0)),
        maximum_change_per_minute=(configured.maximum_change_per_minute
                                   if configured else None),
        source=mapping.source_id,
    ).coverage_seconds


class HomeEnergyCoordinator:
    def __init__(self, hass: HomeAssistant, entry, database: EnergyDatabase,
                 registered_mapping: dict[str, object] | None = None) -> None:
        self.hass = hass
        self.entry = entry
        self.database = database
        self.config = {**entry.data, **entry.options}
        self.mapping_revision = registered_mapping or mapping_revision(self.config)
        self.reader = RecorderReader(hass)
        self.timezone = hass.config.time_zone
        self._temperature_repair_lock = asyncio.Lock()
        self._temperature_backfill_lock = asyncio.Lock()
        self._electricity_recovery_lock = asyncio.Lock()
        self._recovery_preview_key = None

    def _electricity_recovery_fingerprint(self) -> str:
        relevant = {key: self.config.get(key) for key in ("equipment", "site_sources", "influx_mapping_json",
            "influx_enabled", "influx_host", "influx_port", "influx_database", "influx_username",
            "influx_use_ssl", "influx_verify_ssl", "influx_retention_policy", "historical_mode_freshness_minutes",
            "counter_jump_limit_kwh", "ecodan_energy", "ecodan_power", "ecodan_mode", "ac_1_energy", "ac_2_energy", "ac_3_energy")}
        relevant["timezone"] = self.timezone
        relevant["mapping_event_id"] = self.mapping_revision.get("mapping_event_id")
        return hashlib.sha256(json.dumps(relevant, sort_keys=True, default=str).encode()).hexdigest()

    async def async_start_electricity_recovery(self, start: date, end: date, *, evidence_only: bool,
                                               attest_current_mapping: bool, preview_confirmed: bool) -> dict[str, object]:
        """Start only after an explicit administrator request and preview."""
        if not preview_confirmed or end < start or (end-start).days > 30:
            raise ValueError("A preview and an ordered range of at most 31 days are required")
        if evidence_only and attest_current_mapping:
            raise ValueError("evidence-only cannot be combined with attestation")
        if end >= datetime.now(UTC).astimezone(ZoneInfo(self.timezone)).date():
            raise ValueError("recovery requires completed local days")
        mode = "evidence_only" if evidence_only else "attested" if attest_current_mapping else "verified"
        fingerprint = self._electricity_recovery_fingerprint()
        if self._recovery_preview_key != (start.isoformat(), end.isoformat(), evidence_only, attest_current_mapping, fingerprint):
            raise ValueError("Run the recovery preview for this range and configuration first")
        job_id = await self.hass.async_add_executor_job(self.database.create_electricity_recovery_job,
            start.isoformat(), end.isoformat(), self.timezone, mode, fingerprint)
        self._recovery_preview_key = None
        self._schedule_electricity_recovery(job_id)
        return {"job_id": job_id, "status": "queued"}

    def _schedule_electricity_recovery(self, job_id: int) -> None:
        task = self._async_run_electricity_recovery(job_id)
        create = getattr(self.entry, "async_create_background_task", None)
        if create:
            create(self.hass, task, f"{self.entry.entry_id}_electricity_recovery_{job_id}")
        else:
            self.hass.async_create_task(task)

    async def _async_run_electricity_recovery(self, job_id: int) -> None:
        async with self._electricity_recovery_lock:
            try:
                while True:
                    job = await self.hass.async_add_executor_job(self.database.electricity_recovery_job, job_id)
                    if not job or job["status"] not in ("queued", "running"):
                        return
                    details = json.loads(job["details_json"])
                    if details.get("config_fingerprint") != self._electricity_recovery_fingerprint():
                        raise ValueError("configuration_changed_since_start")
                    day = date.fromisoformat(job["cursor_date"])
                    if job["cancel_requested"]:
                        await self.hass.async_add_executor_job(self.database.commit_electricity_recovery_day, job_id, day.isoformat(), [])
                        return
                    preview = await self.async_preview_historical_energy(day, evidence_only=job["mapping_mode"] == "evidence_only",
                        attest_current_mapping=job["mapping_mode"] == "attested")
                    plan = prepare_recovery_day(preview, job["timezone"])
                    outcome = await self.hass.async_add_executor_job(self.database.commit_electricity_recovery_day,
                        job_id, day.isoformat(), plan["records"], len(plan["skipped"]))
                    if outcome.get("cancelled") or outcome.get("status") == "completed":
                        return
            except Exception as error:
                reason = str(error) if isinstance(error, ValueError) else type(error).__name__
                await self.hass.async_add_executor_job(self.database.fail_electricity_recovery_job, job_id, reason)
                _LOGGER.exception("Electricity recovery failed for job %s", job_id)

    def _temperature_mapping_identity(self, item) -> dict[str, object]:
        return {
            "source_id": item.source_id, "entity_id": item.entity_id,
            "attribute": item.attribute, "role": item.role, "enabled": item.enabled,
            "mapping_effective_at": self.mapping_revision.get("effective_at"),
        }

    async def async_preview_historical_energy(self, day: date, *, attest_current_mapping: bool = False,
                                              evidence_only: bool = False,
                                              diagnostic_device: str | None = None) -> dict[str, object]:
        """Read-only single-day preview; no database mutations or automatic execution.

        The WebSocket entry point requires administrator access. Evidence-only
        results use exact configured sources with explicitly unverified mappings.
        """
        start, end = local_day_bounds(day, self.timezone)
        if evidence_only and attest_current_mapping:
            raise ValueError("evidence-only preview cannot be combined with historical mapping attestation")
        try:
            effective = datetime.fromisoformat(str(self.mapping_revision["effective_at"]).replace("Z", "+00:00"))
        except (KeyError, ValueError, TypeError):
            effective = None
        if not evidence_only and not attest_current_mapping and (effective is None or effective.tzinfo is None or effective > start):
            raise ValueError("historical mapping identity requires administrator attestation")
        collection_options = {"store_mapping_revision": False, "include_temperatures": False,
                              "strict_sources": True, "attest_sources": attest_current_mapping}
        if evidence_only:
            collection_options["exact_source_identity"] = True
        source = await self._async_collect_range(start, end, **collection_options)
        try:
            mappings = json.loads(self.config.get(CONF_INFLUX_MAPPING, "{}") or "{}")
            if not isinstance(mappings, dict):
                mappings = {}
        except (TypeError, ValueError):
            mappings = {}
        influx = None
        if self.config.get(CONF_INFLUX_ENABLED) and self.config.get(CONF_INFLUX_HOST) and self.config.get(CONF_INFLUX_DATABASE):
            influx = InfluxBackfillClient(
                host=self.config[CONF_INFLUX_HOST], port=int(self.config.get(CONF_INFLUX_PORT, 8086)),
                database=self.config[CONF_INFLUX_DATABASE], username=self.config.get(CONF_INFLUX_USERNAME),
                password=self.config.get(CONF_INFLUX_PASSWORD), use_ssl=bool(self.config.get("influx_use_ssl", False)),
                verify_ssl=bool(self.config.get("influx_verify_ssl", True)),
                retention_policy=str(self.config.get("influx_retention_policy", "autogen")))
        errors = []
        for source_key in source.provenance.get("influx_backfill_keys", []):
            if source_key.startswith("site.") or (source_key.startswith("equipment.") and source_key.endswith(".energy")):
                source.provenance.setdefault("counter_collection", {})[source_key] = "influx"
        freshness_minutes = int(self.config.get("historical_mode_freshness_minutes", 60))
        if not 1 <= freshness_minutes <= 1440:
            raise ValueError("historical mode freshness must be between 1 and 1440 minutes")
        freshness = timedelta(minutes=freshness_minutes)
        diagnostic_mode_sources = None
        for item in equipment_from_config(self.config):
            if not item.enabled or item.equipment_id not in source.devices:
                continue
            device = source.devices[item.equipment_id]
            base = f"equipment.{item.equipment_id}"
            if evidence_only:
                device.mode = [sample for sample in device.mode if start-freshness <= sample.timestamp < end]
            mode_source = "recorder" if device.mode else "unavailable"

            async def influx_states(key: str, expected_entity: str | None) -> list[StateSample]:
                mapping = mappings.get(key)
                if not influx or not isinstance(mapping, dict) or not expected_entity or mapping.get("entity_id") != expected_entity:
                    return []
                try:
                    domain = expected_entity.split(".", 1)[0]
                    rows = await self.hass.async_add_executor_job(partial(influx.query_states, start, end,
                                                                         entity_id=expected_entity, domain=domain,
                                                                         maximum_age=freshness))
                    return [StateSample(timestamp, state, {"source": "influx"}) for timestamp, state in rows]
                except Exception as error:
                    errors.append({"source": key, "error": type(error).__name__})
                    return []

            archive_modes = await influx_states(base + ".state", item.state_entity)
            if archive_modes:
                recorder_modes = device.mode
                merged = {sample.timestamp: sample for sample in archive_modes}
                merged.update({sample.timestamp: sample for sample in recorder_modes})
                device.mode = sorted(merged.values(), key=lambda sample: sample.timestamp)
                mode_source = "recorder_and_influx" if recorder_modes else "influx"
            fallback_config = mappings.get(base + ".fallback_state")
            fallback_entity = fallback_config.get("entity_id") if isinstance(fallback_config, dict) else None
            fallback = []
            if item.kind == "heat_pump" and fallback_entity:
                try:
                    fallback_history = await self.reader.async_history([fallback_entity], start, end)
                    fallback = states_to_modes(fallback_history.get(fallback_entity, []))
                except Exception as error:
                    errors.append({"source": base + ".fallback_state", "error": type(error).__name__})
                if not fallback:
                    fallback = await influx_states(base + ".fallback_state", fallback_entity)
                if evidence_only:
                    fallback = [sample for sample in fallback if start-freshness <= sample.timestamp < end]
            if item.kind == "heat_pump":
                dhw_config = mappings.get(base + ".actual_dhw_state")
                dhw_entity = (dhw_config.get("entity_id") if isinstance(dhw_config, dict)
                              and dhw_config.get("actual_operation") is True else None)
                actual_dhw = []
                if dhw_entity:
                    try:
                        dhw_history = await self.reader.async_history([dhw_entity], start, end)
                        actual_dhw = states_to_modes(dhw_history.get(dhw_entity, []))
                    except Exception as error:
                        errors.append({"source": base + ".actual_dhw_state", "error": type(error).__name__})
                    if not actual_dhw:
                        actual_dhw = await influx_states(base + ".actual_dhw_state", dhw_entity)
                    if evidence_only:
                        actual_dhw = [sample for sample in actual_dhw if start-freshness <= sample.timestamp < end]
                primary = ([StateSample(sample.timestamp, str(sample.attributes.get(item.state_attribute, "unavailable")),
                                        sample.attributes) for sample in device.mode]
                           if item.state_attribute else device.mode)
                if evidence_only and (item.state_entity or "").startswith("climate."):
                    # A climate selection is not proof of actual compressor operation.
                    if not fallback:
                        fallback = primary
                    primary = []
                if item.equipment_id == diagnostic_device:
                    diagnostic_mode_sources = {
                        "primary": {"entity_id": item.state_entity, "samples": primary},
                        "actual_dhw": {"entity_id": dhw_entity, "samples": actual_dhw},
                        "fallback": {"entity_id": fallback_entity or (item.state_entity if not primary and fallback else None), "samples": fallback},
                    }
                    diagnostic_state_map = item.state_map
                    diagnostic_fallback = fallback
                    diagnostic_fallback_status = "existing_selected_stream"
                    if fallback_entity:
                        try:
                            longer = await self.reader.async_history([fallback_entity], start-timedelta(hours=3), end)
                            recorder_fallback = states_to_modes(longer.get(fallback_entity, []))
                            if recorder_fallback:
                                diagnostic_fallback = [sample for sample in recorder_fallback if start-timedelta(hours=3) <= sample.timestamp < end]
                                diagnostic_fallback_status = "recorder_3h_lookback"
                            elif influx and isinstance(fallback_config, dict) and fallback_config.get("entity_id") == fallback_entity:
                                rows = await self.hass.async_add_executor_job(partial(influx.query_states,
                                    start-timedelta(hours=3), end, entity_id=fallback_entity,
                                    domain=fallback_entity.split(".", 1)[0], maximum_age=timedelta(hours=3)))
                                if rows:
                                    diagnostic_fallback = [StateSample(timestamp, value, {"source": "influx"}) for timestamp, value in rows
                                                           if start-timedelta(hours=3) <= timestamp < end]
                                    diagnostic_fallback_status = "influx_3h_lookback"
                                else:
                                    diagnostic_fallback_status = "extended_sources_unavailable_existing_stream_used"
                            else:
                                diagnostic_fallback_status = "extended_recorder_unavailable_existing_stream_used"
                        except Exception as error:
                            diagnostic_fallback_status = "extended_recorder_error_" + type(error).__name__
                device.mode = historical_heat_pump_modes(start, end, primary=primary, actual_dhw=actual_dhw, fallback=fallback,
                                                         state_map=item.state_map, maximum_age=freshness)
                device.state_map = {name: name for name in ("confirmed_heating", "dhw", "cooling", "idle", "heating_assumed", "unknown")}
            else:
                device.mode = historical_selected_modes(start, end, device.mode, maximum_age=freshness)
                device.state_map = {name: name for name in ("heating", "cooling", "heating_selected", "cooling_selected", "standby", "unknown")}
            device.state_attribute = None
            device.provenance.update({"historical_mode_source": mode_source, "fallback_entity": fallback_entity,
                                      "mapping_attested": bool(attest_current_mapping),
                                      "fallback_observation_count": len(fallback),
                                      "strict_influx_identity": bool(evidence_only),
                                      "historical_mapping_quality": "unverified historical mapping" if evidence_only else "normal"})
            mapping = mappings.get(base + ".energy")
            def counter_coverage(samples):
                valid, _ = historical_counter_observations([s for s in samples if start <= s.timestamp <= end])
                def usable(s):
                    return s.value is not None and isfinite(s.value) and s.value >= 0
                suppressed = set()
                for index, (a, b) in enumerate(zip(valid, valid[1:])):
                    if usable(a) and usable(b) and b.value < a.value:
                        for recovery in range(index+2, len(valid)):
                            if valid[recovery].timestamp-a.timestamp > timedelta(hours=2):
                                break
                            if not usable(valid[recovery]):
                                break
                            if valid[recovery].value >= a.value:
                                suppressed.update(range(index, recovery))
                                break
                return sum((b.timestamp-a.timestamp).total_seconds() for i,(a,b) in enumerate(zip(valid, valid[1:]))
                           if i not in suppressed
                           if usable(a) and usable(b)
                           if timedelta(0) < b.timestamp-a.timestamp <= timedelta(hours=1)
                           and 0 <= b.value-a.value <= float(self.config.get("counter_jump_limit_kwh", 500)))

            recorder_coverage = counter_coverage(device.counter)
            # The normal daily collector rejects counters lacking a midnight baseline.
            # Keep individually observed Recorder increments for this read-only preview.
            if (recorder_coverage < (end-start).total_seconds() and item.energy_entity
                    and getattr(self, "reader", None) is not None):
                try:
                    history = await self.reader.async_history([item.energy_entity], start, end)
                    raw = states_to_numeric(history.get(item.energy_entity, []), dimension="energy")
                    if counter_coverage(raw) > recorder_coverage:
                        device.counter = raw
                        recorder_coverage = counter_coverage(raw)
                        source.provenance.setdefault("counter_collection", {})[base + ".energy"] = "recorder_states_partial"
                except Exception as error:
                    errors.append({"source": base + ".energy", "error": type(error).__name__})
            unit = mapping.get("unit") if isinstance(mapping, dict) else None
            if not unit and item.energy_entity:
                live_state = self.hass.states.get(item.energy_entity)
                unit = live_state.attributes.get("unit_of_measurement") if live_state else None
            mapping_identity_ok = (isinstance(mapping, dict) and
                (mapping.get("entity_id") == item.energy_entity or
                 (not mapping.get("entity_id") and attest_current_mapping)) and
                (not evidence_only or mapping.get("entity_tag") == (item.energy_entity or "").partition(".")[2]))
            if influx and isinstance(mapping, dict) and not mapping_identity_ok:
                errors.append({"source": base + ".energy", "error": "counter_source_identity_unverified"})
            if recorder_coverage < (end-start).total_seconds() and influx and mapping_identity_ok and mapping.get("entity_tag") and unit:
                try:
                    rows = await self.hass.async_add_executor_job(partial(influx.query_counter, mapping["measurement"],
                        mapping.get("field", "value"), start, end + timedelta(microseconds=1), entity_tag=mapping["entity_tag"], include_following=True))
                    converted = states_to_numeric([SimpleNamespace(last_updated=stamp, state=value,
                        attributes={"unit_of_measurement": unit}) for stamp, value in rows], dimension="energy")
                    existing_source = source.provenance.get("counter_collection", {}).get(base + ".energy", "")
                    identity_verified = (mapping_identity_ok
                                         and existing_source != "recorder_statistics")
                    combined, selected_source, merge_error = _merge_preview_counters(
                        device.counter, converted, identity_verified=identity_verified)
                    if merge_error:
                        errors.append({"source": base + ".energy", "error": merge_error})
                    if counter_coverage(combined) > recorder_coverage:
                        device.counter = combined
                        source.provenance.setdefault("counter_collection", {})[base + ".energy"] = selected_source
                except Exception as error:
                    errors.append({"source": base + ".energy", "error": type(error).__name__})
            coverage_seconds = counter_coverage(device.counter)
            _, conflicts = historical_counter_observations([s for s in device.counter if start <= s.timestamp <= end])
            if conflicts:
                errors.append({"source": base + ".energy", "error": "conflicting_counter_timestamps"})
            in_day = sorted((s.timestamp for s in device.counter if start <= s.timestamp <= end))
            missing_intervals = []
            if not in_day:
                missing_intervals.append({"start": start.isoformat(), "end": end.isoformat(), "reason": "no_counter_observations"})
            else:
                if in_day[0] > start:
                    missing_intervals.append({"start": start.isoformat(), "end": in_day[0].isoformat(), "reason": "missing_start_boundary"})
                for left, right in zip(in_day, in_day[1:]):
                    if right-left > timedelta(hours=1):
                        missing_intervals.append({"start": left.isoformat(), "end": right.isoformat(), "reason": "recording_gap"})
                if in_day[-1] < end:
                    missing_intervals.append({"start": in_day[-1].isoformat(), "end": end.isoformat(), "reason": "missing_end_boundary"})
            device.provenance.update({"meter_coverage_seconds": coverage_seconds,
                                      "meter_expected_seconds": (end-start).total_seconds(),
                                      "meter_evidence_status": ("unavailable" if not in_day or coverage_seconds == 0 else
                                          "complete" if coverage_seconds >= (end-start).total_seconds() else "partial_known_subtotal"),
                                      "energy_entity_id": item.energy_entity,
                                      "state_entity_id": item.state_entity,
                                      "first_observation_utc": in_day[0].isoformat() if in_day else None,
                                      "last_observation_utc": in_day[-1].isoformat() if in_day else None,
                                      "conflicting_timestamps_utc": [stamp.isoformat() for stamp in conflicts[:50]],
                                      "missing_intervals": missing_intervals[:50],
                                      "boundary_policy": "observed_whole_increments_only"})
            if coverage_seconds < (end-start).total_seconds():
                errors.append({"source": base + ".energy", "error": "counter_coverage_incomplete"})
        diagnostic_traces = {diagnostic_device: []} if diagnostic_device is not None else None
        report = DailyAggregator(self.timezone, strict_historical=True,
                                 maximum_counter_delta_kwh=float(self.config.get("counter_jump_limit_kwh", 500)),
                                 assumed_heating_reporting=str(self.config.get("assumed_heating_reporting", "separate")),
                                 historical_trace=diagnostic_traces).aggregate_range(day, start, end, source)
        site = {}
        for key in ("pv", "import", "export"):
            site_entity = self._site_sources().get(key) if hasattr(self, "_site_sources") else None
            samples = source.site_counters.get(key, [])
            if not samples and site_entity and getattr(self, "reader", None) is not None:
                try:
                    history = await self.reader.async_history([site_entity], start, end)
                    samples = states_to_numeric(history.get(site_entity, []), dimension="energy")
                    if samples:
                        source.provenance.setdefault("counter_collection", {})[f"site.{key}"] = "recorder_states_partial"
                except Exception as error:
                    errors.append({"source": f"site.{key}", "error": type(error).__name__})
            observed = allocate_historical_counter_increments(
                start=start, end=end, counter_samples=samples,
                mode_samples=[StateSample(start, "measured")], classifier=lambda _: "measured",
                maximum_delta_kwh=float(self.config.get("counter_jump_limit_kwh", 500)))
            site[key] = {"meter_kwh": observed.meter_kwh, "covered_seconds": observed.covered_seconds,
                         "period_seconds": observed.period_seconds, "rejected_count": observed.rejected_count,
                         "reset_count": observed.reset_count,
                         "entity_id": site_entity,
                         "status": ("unavailable" if observed.meter_kwh is None else
                                    "complete" if observed.covered_seconds >= observed.period_seconds and not observed.rejected_count and not observed.reset_count
                                    else "partial_known_subtotal"),
                         "source": source.provenance.get("counter_collection", {}).get(f"site.{key}", "unavailable")}
        result = {"local_date": day.isoformat(), "preview": True, "evidence_only": evidence_only,
                "historical_mapping_quality": "unverified historical mapping" if evidence_only else "normal",
                "allocations": report.allocations, "site": site,
                "errors": errors, "provenance": source.provenance}
        if diagnostic_traces is not None:
            history = source.devices.get(diagnostic_device)
            unknown_summary = {}
            if diagnostic_mode_sources is not None:
                for entry in diagnostic_traces[diagnostic_device]:
                    if entry.get("status") != "accepted" or entry.get("category") != "unknown":
                        continue
                    moment = datetime.fromisoformat(entry["start"])
                    evidence = explain_historical_mode(moment, diagnostic_mode_sources, diagnostic_state_map, freshness)
                    entry["mode_evidence_at_start"] = evidence
                    reason = entry.get("reason", "unknown")
                    detail = ", ".join(f"{role}:{evidence[role]['reason']}" for role in ("primary", "actual_dhw", "fallback"))
                    key = f"{reason} ({detail})"
                    unknown_summary[key] = unknown_summary.get(key, 0.0) + entry["delta_kwh"]
            simulations = None
            continuity = None
            power_analysis = None
            if diagnostic_mode_sources is not None and history:
                simulation_sources = dict(diagnostic_mode_sources)
                simulation_sources["fallback"] = {**diagnostic_mode_sources["fallback"], "samples": diagnostic_fallback}
                simulations = simulate_modes(start, end, history, simulation_sources, diagnostic_state_map,
                    float(self.config.get("counter_jump_limit_kwh", 500)), freshness)
                # The baseline must use the exact originally selected stream, not the longer diagnostic query.
                simulations["baseline"] = simulate_modes(start, end, history, diagnostic_mode_sources,
                    diagnostic_state_map, float(self.config.get("counter_jump_limit_kwh", 500)), freshness)["baseline"]
                continuity = continuity_candidates(diagnostic_traces[diagnostic_device], history,
                    simulation_sources, diagnostic_state_map, freshness)
                power_analysis = analyze_power(diagnostic_traces[diagnostic_device], history,
                    diagnostic_mode_sources, diagnostic_state_map, freshness, continuity)
                simulations["supported_continuity"] = {**simulations["baseline"],
                    "evidence": "No validated historical operating-state mapping and independent continuity evidence; baseline retained."}
            result["increment_diagnostic"] = {"trace": diagnostic_traces[diagnostic_device],
                "period_start_utc": start.isoformat(), "period_end_utc": end.isoformat(),
                "counter_observations": [{"timestamp": s.timestamp.isoformat(), "value_kwh": s.value if s.value is not None and isfinite(s.value) else None, "source": s.source, "reason": s.reason} for s in history.counter] if history else [],
                "mode_transitions": [{"timestamp": s.timestamp.isoformat(), "state": s.state, "source": s.attributes.get("source")} for s in history.mode] if history else [],
                "freshness_seconds": freshness.total_seconds(),
                "mode_source_entities": {role: info["entity_id"] for role, info in diagnostic_mode_sources.items()} if diagnostic_mode_sources else {},
                "selected_source_observations": {role: [{"timestamp": sample.timestamp.isoformat(), "state": sample.state,
                    "source": sample.attributes.get("source", "recorder")} for sample in info["samples"]]
                    for role, info in diagnostic_mode_sources.items()} if diagnostic_mode_sources else {},
                "unknown_kwh_by_reason": unknown_summary,
                "scenario_comparison": simulations,
                "continuity_analysis": continuity,
                "power_pattern_analysis": power_analysis,
                "scenario_fallback_evidence_status": diagnostic_fallback_status if diagnostic_mode_sources else "unavailable",
                "note": "Decision transitions are normalized boundaries; source observations and their ages retain original timestamps. Selected heat is assumed, not confirmed."}
        return result

    def _temperature_sources(self, *, include_disabled: bool = False, equipment=None):
        explicit = list(temperatures_from_config(self.config))
        selected = explicit if include_disabled else [item for item in explicit if item.enabled]
        explicit_keys = {(item.entity_id, item.role) for item in selected}
        if equipment is None:
            equipment = [item for item in equipment_from_config(self.config) if item.enabled]
        selected.extend(
            item for item in equipment_temperature_mappings(equipment)
            if (item.entity_id, item.role) not in explicit_keys
        )
        return selected

    def _site_sources(self) -> dict[str, str | None]:
        configured = self.config.get("site_sources", {})
        if isinstance(configured, str):
            configured = json.loads(configured or "{}")
        return {
            key: configured.get(key) or self.config.get(legacy)
            for key, legacy in LEGACY_SITE_KEYS.items()
        }

    def _aggregator(self) -> DailyAggregator:
        freshness_minutes = int(self.config.get(CONF_TEMPERATURE_FRESHNESS_MINUTES, 0))
        return DailyAggregator(
            self.timezone,
            cycle_hours=int(self.config.get(CONF_CYCLE_HOURS, 6)),
            temperature_freshness=(timedelta(minutes=freshness_minutes)
                                   if freshness_minutes else None),
            seasonal_fallback=bool(self.config.get(CONF_SEASONAL_FALLBACK, False)),
            maximum_power_gap=timedelta(
                minutes=int(self.config.get("maximum_power_gap_minutes", 30))
            ),
            maximum_counter_delta_kwh=float(self.config.get("counter_jump_limit_kwh", 500)),
            assumed_heating_reporting=str(
                self.config.get("assumed_heating_reporting", "separate")
            ),
        )

    async def async_process_day(self, day: date, *, reason: str = "daily_close") -> bool:
        job_id = await self.hass.async_add_executor_job(
            self.database.start_job, "daily", {"date": day.isoformat(), "reason": reason}
        )
        try:
            source = await self._async_collect(day)
            source.checkpoints = await self.hass.async_add_executor_job(
                self.database.checkpoints_for_day, day.isoformat()
            )
            aggregator = self._aggregator()
            record = await self.hass.async_add_executor_job(aggregator.aggregate, day, source)
            changed = await self.hass.async_add_executor_job(
                lambda: self.database.upsert_daily(record, reason=reason)
            )
        except Exception as error:
            await self.hass.async_add_executor_job(
                self.database.finish_job, job_id, "failed", {"error": type(error).__name__}
            )
            raise
        await self.hass.async_add_executor_job(
            self.database.finish_job, job_id, "success", {"changed": changed, "quality": record.quality}
        )
        if changed:
            async_dispatcher_send(self.hass, f"{self.entry.entry_id}_updated")
        return changed

    async def async_process_hour(self, start: datetime, end: datetime) -> bool:
        """Persist one completed elapsed hour; safe to retry after restart."""
        if start.tzinfo is None or end.tzinfo is None or end <= start:
            raise ValueError("hourly interval must be an ordered aware range")
        source = await self._async_collect_range(start, end)
        local_date = start.astimezone(ZoneInfo(self.timezone)).date()
        record = await self.hass.async_add_executor_job(
            self._aggregator().aggregate_range, local_date, start, end, source
        )
        payload = record.as_dict()
        payload["interval_kind"] = "completed_hour"
        payload["collected_at"] = datetime.now(UTC).isoformat()
        changed = await self.hass.async_add_executor_job(self.database.upsert_hourly, payload)
        try:
            await self._async_capture_thermal_counters(start, end)
        except Exception:  # Thermal evidence must never break electrical collection.
            _LOGGER.exception("Automatic Ecodan thermal counter capture failed")
        if changed:
            async_dispatcher_send(self.hass, f"{self.entry.entry_id}_updated")
        return changed

    async def _async_capture_thermal_counters(
        self, start: datetime, end: datetime,
    ) -> None:
        """Capture source-timestamped cumulative thermal observations read-only."""
        if self.config.get("thermal_source_mode", "manual_only") not in {
            "ha_counters", "mixed",
        }:
            return
        equipment_id = self.config.get("ecodan_thermal_equipment_id")
        configured_equipment = {
            item.equipment_id: item for item in equipment_from_config(self.config)
            if item.enabled and item.kind == "heat_pump"
        }
        if equipment_id not in configured_equipment:
            return
        sources = {
            "heating_thermal_kwh": self.config.get("heating_thermal_energy_entity"),
            "dhw_thermal_kwh": self.config.get("dhw_thermal_energy_entity"),
            "vendor_heating_electricity_kwh": self.config.get(
                "vendor_heating_electricity_entity"
            ),
            "vendor_dhw_electricity_kwh": self.config.get(
                "vendor_dhw_electricity_entity"
            ),
        }
        entity_ids = sorted({entity for entity in sources.values() if entity})
        if not entity_ids:
            return
        history = await self.reader.async_history(entity_ids, start, end)
        zone = ZoneInfo(self.timezone)
        for field, entity_id in sources.items():
            if not entity_id:
                continue
            samples = [sample for sample in states_to_numeric(
                history.get(entity_id, []), dimension="energy"
            ) if sample.value is not None and sample.timestamp <= end]
            if not samples:
                continue
            for sample in {item.timestamp: item for item in samples}.values():
                local = sample.timestamp.astimezone(zone)
                thermal_values = {
                    "heating_thermal_kwh": None, "dhw_thermal_kwh": None,
                    "vendor_heating_electricity_kwh": None,
                    "vendor_dhw_electricity_kwh": None,
                }
                thermal_values[field] = float(sample.value)
                payload = {
                    "ecodan_equipment_id": equipment_id,
                    "meter_identity": entity_id,
                    "effective_at_utc": sample.timestamp.astimezone(UTC).isoformat(),
                    "local_date": local.date().isoformat(),
                    "local_time": local.time().isoformat(),
                    "timezone": self.timezone, "fold": local.fold,
                    **thermal_values,
                    "source_label": f"Home Assistant Recorder: {entity_id}",
                    "note": None, "unit": "kWh", "source_entity_id": entity_id,
                    "source_unit": sample.source_unit,
                    "mapping_effective_at": self.mapping_revision.get("effective_at"),
                }
                await self.hass.async_add_executor_job(
                    self.database.save_automatic_thermal_reading, payload
                )

    async def async_backfill_thermal_counters(
        self, start_date: date, end_date: date,
    ) -> dict[str, object]:
        """Bounded, rerunnable Recorder backfill for configured thermal counters."""
        if end_date < start_date or (end_date - start_date).days > 365:
            raise ValueError("thermal backfill must be ordered and at most 366 local dates")
        before = await self.hass.async_add_executor_job(self.database.thermal_reading_count)
        errors: list[dict[str, str]] = []
        cursor = start_date
        processed = 0
        while cursor <= end_date:
            start, end = local_day_bounds(cursor, self.timezone)
            try:
                await self._async_capture_thermal_counters(start, end)
            except Exception as error:
                errors.append({"local_date": cursor.isoformat(),
                               "error": type(error).__name__})
            processed += 1
            cursor += timedelta(days=1)
        after = await self.hass.async_add_executor_job(self.database.thermal_reading_count)
        return {"processed_days": processed, "new_readings": after - before,
                "error_count": len(errors), "errors": errors,
                "idempotent": True, "source": "recorder_read_only"}

    async def async_refresh_hourly(
        self, now: datetime | None = None, *, reprocess_poor: bool = False,
    ) -> int:
        """Recover bounded missing hours for today and yesterday."""
        instant = (now or datetime.now(UTC)).astimezone(UTC)
        completed_end = instant.replace(minute=0, second=0, microsecond=0)
        zone = ZoneInfo(self.timezone)
        local_today = instant.astimezone(zone).date()
        scan_start, _ = local_day_bounds(local_today - timedelta(days=1), self.timezone)
        changed = 0
        cursor = scan_start
        while cursor + timedelta(hours=1) <= completed_end:
            end = cursor + timedelta(hours=1)
            needs_reprocess = await self.hass.async_add_executor_job(
                partial(
                    self.database.hourly_needs_reprocess, cursor, end,
                    include_partial=reprocess_poor,
                )
            )
            if needs_reprocess:
                changed += int(await self.async_process_hour(cursor, end))
            cursor = end
        return changed

    async def async_reprocess_temperature_history(
        self, start_date: date, end_date: date,
    ) -> dict[str, object]:
        """Recover temperature summaries only, preserving valid energy evidence."""
        if end_date < start_date or (end_date - start_date).days > 30:
            raise ValueError("Temperature repair range must be ordered and at most 31 days")
        if self._temperature_repair_lock.locked():
            return {
                "processed_days": 0, "changed_daily_records": 0,
                "changed_hourly_records": 0, "errors": [{
                    "reason": "temperature_repair_already_running",
                }], "error_count": 1, "skipped_repair_count": 0,
                "skipped_repair_reasons": [],
            }
        started = monotonic()
        changed_daily = changed_hourly = processed_days = 0
        sources_inspected = skipped_complete = sources_with_history = 0
        missing_recorder_data = 0
        recorder_query_count = influx_query_count = 0
        peak_normalized_samples_per_day = 0
        errors: list[dict[str, str]] = []
        skipped_repair_reasons: list[dict[str, object]] = []
        configured = {item.source_id: item for item in self._temperature_sources()}

        def complete(summary: object) -> bool:
            if not isinstance(summary, dict):
                return False
            mean = summary.get("mean")
            coverage = float(summary.get("coverage_percent") or 0)
            return (
                isinstance(mean, (int, float)) and isfinite(float(mean))
                and coverage >= 100.0
                and summary.get("quality") not in {
                    "missing", "poor", "partial", "rejected", "unreconciled",
                }
            )

        async with self._temperature_repair_lock:
            day = start_date
            while day <= end_date:
                start, end = local_day_bounds(day, self.timezone)
                try:
                    daily = await self.hass.async_add_executor_job(
                        self.database.daily, day.isoformat()
                    )
                    hours = await self.hass.async_add_executor_job(
                        self.database.hourly_range, start, end
                    )
                    if daily is None and not hours:
                        day += timedelta(days=1)
                        continue
                    needed: set[str] = set()
                    records = ([daily] if daily is not None else []) + hours
                    for source_id in configured:
                        source_complete = all(
                            complete(record.get("temperatures", {}).get(source_id))
                            for record in records
                        )
                        if source_complete:
                            skipped_complete += 1
                        else:
                            needed.add(source_id)
                    if not needed:
                        processed_days += 1
                        day += timedelta(days=1)
                        continue
                    mapping_applicability: dict[str, dict[str, object]] = {}
                    for source_id in sorted(needed):
                        applicability = await self.hass.async_add_executor_job(
                            self.database.temperature_mapping_applicability,
                            source_id, start, end,
                            self._temperature_mapping_identity(configured[source_id]),
                        )
                        if applicability.get("eligible"):
                            mapping_applicability[source_id] = applicability
                        else:
                            skipped_repair_reasons.append({
                                "date": day.isoformat(), "source_id": source_id,
                                **applicability,
                            })
                    needed &= set(mapping_applicability)
                    if not needed:
                        processed_days += 1
                        day += timedelta(days=1)
                        continue
                    sources_inspected += len(needed)
                    source = await self._async_collect_temperatures(start, end, needed)
                    metrics = source.provenance.get("collection_metrics", {})
                    recorder_query_count += int(metrics.get("recorder_query_count", 0))
                    influx_query_count += int(metrics.get("influx_query_count", 0))
                    peak_normalized_samples_per_day = max(
                        peak_normalized_samples_per_day,
                        int(metrics.get("normalized_samples", 0)),
                    )
                    if source.provenance.get("query_errors"):
                        errors.extend({
                            "date": day.isoformat(), **error,
                        } for error in source.provenance["query_errors"])
                    collection = source.provenance.get("temperature_collection", {})
                    for source_id, applicability in mapping_applicability.items():
                        evidence = collection.get(source_id)
                        if isinstance(evidence, dict):
                            # The bounded repair is evidence for the historical mapping
                            # revision, not for whichever unrelated revision is current.
                            evidence["mapping_effective_at"] = applicability.get(
                                "mapping_effective_at"
                            )
                            evidence["mapping_effective_end"] = applicability.get(
                                "mapping_effective_end"
                            )
                    available = {
                        source_id for source_id in needed
                        if collection.get(source_id, {}).get(
                            "validated_coverage_seconds", 0
                        ) > 0
                    }
                    sources_with_history += len(available)
                    missing_recorder_data += len(needed - available)
                    if not available:
                        processed_days += 1
                        day += timedelta(days=1)
                        continue
                    aggregator = self._aggregator()
                    provenance = {
                        "source": "bounded_recorder_temperature_repair",
                        "processed_at": datetime.now(UTC).isoformat(),
                        "period_start_utc": start.isoformat(),
                        "period_end_utc": end.isoformat(),
                        "collection": collection,
                        "mapping_applicability": mapping_applicability,
                    }
                    if daily is not None:
                        recovered = await self.hass.async_add_executor_job(
                            aggregator.aggregate_range, day, start, end, source
                        )
                        changed_daily += int(await self.hass.async_add_executor_job(
                            self.database.merge_daily_temperatures, day.isoformat(),
                            {key: value for key, value in recovered.temperatures.items()
                             if key in available}, provenance,
                        ))
                    for hour in hours:
                        hour_start = datetime.fromisoformat(hour["period_start_utc"])
                        hour_end = datetime.fromisoformat(hour["period_end_utc"])
                        recovered = await self.hass.async_add_executor_job(
                            aggregator.aggregate_range, day, hour_start, hour_end, source
                        )
                        changed_hourly += int(await self.hass.async_add_executor_job(
                            self.database.merge_hourly_temperatures, hour_start, hour_end,
                            {key: value for key, value in recovered.temperatures.items()
                             if key in available}, provenance,
                        ))
                    processed_days += 1
                except Exception as error:  # Isolate one bad day from the bounded repair.
                    _LOGGER.exception("Temperature repair failed for %s", day)
                    errors.append({"date": day.isoformat(), "error": type(error).__name__})
                day += timedelta(days=1)
                await asyncio.sleep(0)
        if changed_daily or changed_hourly:
            async_dispatcher_send(self.hass, f"{self.entry.entry_id}_updated")
        return {
            "processed_days": processed_days,
            "changed_daily_records": changed_daily,
            "changed_hourly_records": changed_hourly,
            "sources_inspected": sources_inspected,
            "sources_skipped_complete": skipped_complete,
            "sources_with_available_history": sources_with_history,
            "missing_recorder_data": missing_recorder_data,
            "recorder_query_count": recorder_query_count,
            "influx_query_count": influx_query_count,
            "peak_normalized_samples_per_day": peak_normalized_samples_per_day,
            "error_count": len(errors),
            "errors": errors,
            "skipped_repair_count": len(skipped_repair_reasons),
            "skipped_repair_reasons": skipped_repair_reasons,
            "elapsed_seconds": round(monotonic() - started, 3),
        }

    async def async_start_temperature_backfill(
        self, start_date: date, end_date: date, *, dry_run: bool = False,
        attest_current_mapping: bool = False,
    ) -> dict[str, object]:
        """Queue a bounded independent temperature-history job."""
        if end_date < start_date or (end_date - start_date).days > 30:
            raise ValueError("Temperature backfill range must be 1–31 inclusive days")
        range_start, _ = local_day_bounds(start_date, self.timezone)
        _, range_end = local_day_bounds(end_date, self.timezone)
        range_end = min(
            range_end, datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
        )
        if attest_current_mapping and not dry_run:
            for item in self._temperature_sources():
                await self.hass.async_add_executor_job(
                    self.database.attest_temperature_mapping,
                    item.source_id, range_start, range_end,
                    self._temperature_mapping_identity(item),
                )
        job_id = await self.hass.async_add_executor_job(
            partial(
                self.database.create_temperature_backfill_job,
                start_date.isoformat(), end_date.isoformat(), dry_run=dry_run,
                details={
                    "attested_current_mapping": attest_current_mapping,
                    "timezone": self.timezone,
                },
            )
        )
        task = self._async_run_temperature_backfill(job_id)
        create_background = getattr(self.entry, "async_create_background_task", None)
        if create_background is not None:
            create_background(
                self.hass, task, f"{self.entry.entry_id}_temperature_backfill_{job_id}"
            )
        else:
            self.hass.async_create_task(task)
        return {
            "job_id": job_id, "status": "queued", "dry_run": dry_run,
            "start_date": start_date.isoformat(), "end_date": end_date.isoformat(),
        }

    async def _async_run_temperature_backfill(self, job_id: int) -> dict[str, object]:
        async with self._temperature_backfill_lock:
            return await self._async_execute_temperature_backfill(job_id)

    async def _async_execute_temperature_backfill(self, job_id: int) -> dict[str, object]:
        """Run or resume one idempotent temperature-only backfill job."""
        job = await self.hass.async_add_executor_job(
            self.database.temperature_backfill_job, job_id
        )
        if job is None:
            raise ValueError("Temperature backfill job does not exist")
        counters = {
            "processed_days": 0, "expected_hourly_intervals": 0,
            "eligible_hourly_summaries": 0, "inserted_hourly": 0,
            "eligible_daily_summaries": 0,
            "updated_hourly": 0, "unchanged_hourly": 0,
            "inserted_daily": 0, "updated_daily": 0, "unchanged_daily": 0,
            "skipped": 0, "failed": 0, "recorder_query_count": 0,
            "recorder_rows": 0, "normalized_samples": 0,
        }
        details = dict(job.get("details") or {})
        counters.update({
            key: int(details.get(key, value) or 0)
            for key, value in counters.items()
        })
        reasons: list[dict[str, object]] = list(details.get("skip_reasons") or [])
        dry_run = bool(job["dry_run"])
        original_start_date = date.fromisoformat(str(job["start_date"]))
        start_date = date.fromisoformat(str(job.get("cursor_date") or job["start_date"]))
        end_date = date.fromisoformat(job["end_date"])
        range_start_utc, _ = local_day_bounds(original_start_date, self.timezone)
        _, range_end_utc = local_day_bounds(end_date, self.timezone)
        before_counts = details.get("temperature_summaries_before")
        if not isinstance(before_counts, dict):
            before_counts = await self.hass.async_add_executor_job(
                self.database.temperature_history_counts,
                range_start_utc, range_end_utc, None,
            )
            details["temperature_summaries_before"] = before_counts
        configured = {item.source_id: item for item in self._temperature_sources()}
        await self.hass.async_add_executor_job(
            partial(self.database.update_temperature_backfill_job, job_id, status="running")
        )
        try:
            day = start_date
            while day <= end_date:
                current_job = await self.hass.async_add_executor_job(
                    self.database.temperature_backfill_job, job_id
                )
                if current_job and current_job.get("cancel_requested"):
                    details = {**details, **counters, "skip_reasons": reasons[-500:]}
                    await self.hass.async_add_executor_job(
                        partial(
                            self.database.update_temperature_backfill_job, job_id,
                            status="cancelled", cursor_date=day.isoformat(), details=details,
                        )
                    )
                    return {"job_id": job_id, "status": "cancelled", **details}
                day_start, day_end = local_day_bounds(day, self.timezone)
                effective_end = min(
                    day_end, datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
                )
                if effective_end <= day_start:
                    reasons.append({
                        "date": day.isoformat(), "reason": "future_interval_not_processed",
                    })
                    day += timedelta(days=1)
                    continue
                hours: list[tuple[datetime, datetime]] = []
                cursor = day_start
                while cursor + timedelta(hours=1) <= effective_end:
                    hours.append((cursor, cursor + timedelta(hours=1)))
                    cursor += timedelta(hours=1)
                counters["expected_hourly_intervals"] += len(hours) * len(configured)
                applicable: dict[str, dict[str, object]] = {}
                historical_mappings = []
                for source_id, item in configured.items():
                    decision = await self.hass.async_add_executor_job(
                        partial(
                            self.database.temperature_mapping_applicability,
                            source_id, day_start, effective_end,
                            self._temperature_mapping_identity(item),
                            allow_historical_identity=True,
                        )
                    )
                    if (
                        not decision.get("eligible") and dry_run
                        and details.get("attested_current_mapping")
                    ):
                        decision = {
                            "eligible": True,
                            "reason": "mapping_user_attested_preview",
                            **self._temperature_mapping_identity(item),
                            "mapping_event_id": None,
                            "mapping_attestation_id": None,
                            "mapping_effective_at": range_start_utc.isoformat(),
                            "mapping_effective_end": range_end_utc.isoformat(),
                        }
                    if not decision.get("eligible"):
                        reason = str(decision.get("reason") or "mapping_not_recorded_for_interval")
                        if reason == "mapping_changes_within_interval":
                            reason = "mapping_changed"
                        counters["skipped"] += len(hours)
                        reasons.append({
                            "date": day.isoformat(), "source_id": source_id,
                            "reason": reason, **decision,
                        })
                        continue
                    applicable[source_id] = decision
                    historical_mappings.append(replace(
                        item, entity_id=str(decision["entity_id"]),
                        attribute=decision.get("attribute"),
                        role=str(decision["role"]), enabled=True,
                    ))
                if not historical_mappings:
                    counters["processed_days"] += 1
                    day += timedelta(days=1)
                    continue
                source = await self._async_collect_temperatures(
                    day_start, effective_end,
                    set(applicable), source_mappings=historical_mappings,
                )
                metrics = source.provenance.get("collection_metrics", {})
                counters["recorder_query_count"] += int(
                    metrics.get("recorder_query_count", 0) or 0
                )
                counters["recorder_rows"] += int(metrics.get("history_rows", 0) or 0)
                counters["normalized_samples"] += int(
                    metrics.get("normalized_samples", 0) or 0
                )
                aggregator = self._aggregator()

                async def persist(
                    kind: str, interval_start: datetime, interval_end: datetime,
                    summary_source_id: str, summary: dict[str, Any],
                ) -> None:
                    decision = applicable[summary_source_id]
                    provenance = dict(summary.get("source_provenance") or {})
                    provenance.update({
                        "source_id": summary_source_id,
                        "entity_id": decision.get("entity_id"),
                        "attribute": decision.get("attribute"),
                        "role": decision.get("role"),
                        "mapping_event_id": decision.get("mapping_event_id"),
                        "mapping_attestation_id": decision.get("mapping_attestation_id"),
                        "mapping_effective_at": decision.get("mapping_effective_at"),
                        "mapping_effective_end": decision.get("mapping_effective_end"),
                    })
                    candidate = {**summary, "source_provenance": provenance}
                    if candidate.get("mean") is None or float(
                        candidate.get("coverage_seconds", 0) or 0
                    ) <= 0:
                        counters["skipped"] += 1
                        collection = source.provenance.get(
                            "temperature_collection", {}
                        ).get(summary_source_id, {})
                        invalid = collection.get("invalid_reasons") or []
                        reasons.append({
                            "date": day.isoformat(), "source_id": summary_source_id,
                            "interval_start_utc": interval_start.isoformat(),
                            "reason": ("invalid_samples" if invalid
                                       else "no_valid_recorder_evidence"),
                            "invalid_reasons": invalid,
                        })
                        return
                    counters["eligible_hourly_summaries"] += int(kind == "hourly")
                    counters["eligible_daily_summaries"] += int(kind == "daily")
                    outcome = await self.hass.async_add_executor_job(
                        partial(
                            self.database.upsert_temperature_history,
                            kind, interval_start, interval_end, day.isoformat(),
                            self.timezone, summary_source_id, candidate, decision,
                            dry_run=dry_run,
                        )
                    )
                    key = f"{outcome}_{kind}"
                    if key in counters:
                        counters[key] += 1
                    elif outcome.startswith("skipped"):
                        counters["skipped"] += 1
                        reasons.append({
                            "date": day.isoformat(), "source_id": summary_source_id,
                            "interval_start_utc": interval_start.isoformat(),
                            "reason": outcome,
                        })

                for hour_start, hour_end in hours:
                    recovered = await self.hass.async_add_executor_job(
                        aggregator.aggregate_range, day, hour_start, hour_end, source
                    )
                    for source_id in applicable:
                        summary = recovered.temperatures.get(source_id)
                        if isinstance(summary, dict):
                            await persist("hourly", hour_start, hour_end, source_id, summary)
                if effective_end == day_end:
                    recovered_day = await self.hass.async_add_executor_job(
                        aggregator.aggregate_range, day, day_start, day_end, source
                    )
                    for source_id in applicable:
                        summary = recovered_day.temperatures.get(source_id)
                        if isinstance(summary, dict):
                            await persist("daily", day_start, day_end, source_id, summary)
                counters["processed_days"] += 1
                next_day = day + timedelta(days=1)
                details = {**details, **counters, "skip_reasons": reasons[-500:]}
                await self.hass.async_add_executor_job(
                    partial(
                        self.database.update_temperature_backfill_job, job_id,
                        cursor_date=next_day.isoformat(), details=details,
                    )
                )
                day = next_day
                await asyncio.sleep(0)
            changes = sum(counters[key] for key in (
                "inserted_hourly", "updated_hourly", "inserted_daily", "updated_daily",
            ))
            if dry_run:
                outcome = (
                    "preview_ready" if counters["eligible_hourly_summaries"]
                    or counters["eligible_daily_summaries"]
                    else "preview_no_recoverable_records_action_required"
                )
            elif changes:
                outcome = "records_recovered"
            elif counters["skipped"] or counters["failed"]:
                outcome = "no_records_recovered_action_required"
            else:
                outcome = "no_changes_needed"
            after_counts = await self.hass.async_add_executor_job(
                self.database.temperature_history_counts,
                range_start_utc, range_end_utc, None,
            )
            details = {
                **details, **counters, "skip_reasons": reasons[-500:],
                "outcome": outcome,
                "temperature_summaries_before": before_counts,
                "temperature_summaries_after": after_counts,
                "net_new_temperature_summaries": {
                    kind: int(after_counts.get(kind, 0)) - int(before_counts.get(kind, 0))
                    for kind in {"hourly", "daily"}
                },
            }
            status = "preview_complete" if dry_run else "completed"
            await self.hass.async_add_executor_job(
                partial(
                    self.database.update_temperature_backfill_job, job_id,
                    status=status, cursor_date=(end_date + timedelta(days=1)).isoformat(),
                    details=details,
                )
            )
            if changes:
                async_dispatcher_send(self.hass, f"{self.entry.entry_id}_updated")
            return {"job_id": job_id, "status": status, **details}
        except asyncio.CancelledError:
            await self.hass.async_add_executor_job(
                partial(
                    self.database.update_temperature_backfill_job, job_id,
                    status="interrupted", details={
                        **details, **counters, "skip_reasons": reasons[-500:],
                    },
                )
            )
            raise
        except Exception as error:
            _LOGGER.exception("Temperature-only backfill job %s failed", job_id)
            counters["failed"] += 1
            details = {**details, **counters, "skip_reasons": reasons[-500:],
                       "error": type(error).__name__, "outcome": "failed"}
            await self.hass.async_add_executor_job(
                partial(
                    self.database.update_temperature_backfill_job, job_id,
                    status="failed", details=details,
                )
            )
            return {"job_id": job_id, "status": "failed", **details}

    async def async_temperature_diagnostics(
        self, start_date: date, end_date: date,
    ) -> dict[str, Any]:
        """Trace configured temperatures through bounded Recorder and storage reads."""
        if end_date < start_date or (end_date - start_date).days > 30:
            raise ValueError("Diagnostic range must be ordered and at most 31 days")
        start, _ = local_day_bounds(start_date, self.timezone)
        _, requested_end = local_day_bounds(end_date, self.timezone)
        end = min(
            requested_end,
            datetime.now(UTC).replace(minute=0, second=0, microsecond=0),
        )
        enabled_ids = {item.source_id for item in self._temperature_sources()}
        source = await self._async_collect_temperatures(start, end, enabled_ids)
        hourly = await self.hass.async_add_executor_job(
            self.database.hourly_range, start, end
        )
        temperature_only_hourly = await self.hass.async_add_executor_job(
            self.database.temperature_history_range, "hourly", start, end
        )
        hourly_by_key = {row["period_start_utc"]: row for row in hourly}
        for row in temperature_only_hourly:
            if row["period_start_utc"] not in hourly_by_key:
                hourly.append(row)
                hourly_by_key[row["period_start_utc"]] = row
        hourly.sort(key=lambda row: row["period_start_utc"])
        daily = await self.hass.async_add_executor_job(
            self.database.history_period, start_date.isoformat(), end_date.isoformat()
        )
        temperature_only_daily = await self.hass.async_add_executor_job(
            self.database.temperature_history_range, "daily", start, end
        )
        energy_counts = await self.hass.async_add_executor_job(
            self.database.energy_record_counts, start, end,
            start_date.isoformat(), end_date.isoformat(),
        )
        configured = self._temperature_sources(include_disabled=True)
        collection = source.provenance.get("temperature_collection", {})
        expected_hours = []
        cursor = start
        while cursor + timedelta(hours=1) <= end:
            expected_hours.append((cursor, cursor + timedelta(hours=1)))
            cursor += timedelta(hours=1)
        hourly_by_start = {
            item.get("period_start_utc"): item for item in hourly
        }
        sources = []
        for item in configured:
            live = self.hass.states.get(item.entity_id)
            raw = collection.get(item.source_id, {})
            mapping_decision = await self.hass.async_add_executor_job(
                partial(
                    self.database.temperature_mapping_applicability,
                    item.source_id, start, end,
                    self._temperature_mapping_identity(item),
                    allow_historical_identity=True,
                )
            )
            persisted_hourly = [
                row.get("temperatures", {}).get(item.source_id)
                for row in hourly
                if isinstance(row.get("temperatures", {}).get(item.source_id), dict)
            ]
            persisted_daily = [
                row.get("temperatures", {}).get(item.source_id)
                for row in daily
                if isinstance(row.get("temperatures", {}).get(item.source_id), dict)
            ]
            missing_intervals = []
            recoverable_intervals = []
            nonrecoverable_intervals = []
            recoverable_interval_count = 0
            nonrecoverable_interval_count = 0
            for interval_start, _interval_end in expected_hours:
                row = hourly_by_start.get(interval_start.isoformat())
                summary = (row.get("temperatures", {}).get(item.source_id)
                           if row else None)
                if row is None:
                    reason = "hourly_record_not_processed"
                elif not isinstance(summary, dict):
                    reason = "temperature_not_persisted"
                elif not summary.get("coverage_seconds"):
                    reasons = [
                        rejection.get("reason", "rejected")
                        for rejection in summary.get("rejections", [])
                    ]
                    reason = ",".join(sorted(set(reasons))) or "no_valid_coverage"
                else:
                    continue
                target = recoverable_intervals if (
                    mapping_decision.get("eligible")
                    and raw.get("validated_coverage_seconds", 0) > 0
                ) else nonrecoverable_intervals
                if target is recoverable_intervals:
                    recoverable_interval_count += 1
                else:
                    nonrecoverable_interval_count += 1
                if len(target) < 200:
                    target.append({
                        "start": interval_start.isoformat(),
                        "reason": (
                            reason if mapping_decision.get("eligible") else
                            mapping_decision.get("reason", "mapping_not_recorded_for_interval")
                        ),
                    })
                if len(missing_intervals) < 200:
                    missing_intervals.append({
                        "start": interval_start.isoformat(), "reason": reason,
                    })
            rejections = []
            for summary in [*persisted_hourly, *persisted_daily]:
                rejections.extend(summary.get("rejections", []))
            valid_hourly = sum(
                1 for summary in persisted_hourly
                if summary.get("mean") is not None
                and float(summary.get("coverage_seconds", 0) or 0) > 0
            )
            valid_daily = sum(
                1 for summary in persisted_daily
                if summary.get("mean") is not None
                and float(summary.get("coverage_seconds", 0) or 0) > 0
            )
            live_unit = (None if live is None else (
                live.attributes.get("unit_of_measurement")
                or live.attributes.get("temperature_unit")
            ))
            unit_status = (
                "unknown" if live is None else
                "compatible" if live_unit in {"°C", "°F"} else "incompatible_or_missing"
            )
            coverage_seconds = sum(
                float(summary.get("coverage_seconds", 0) or 0)
                for summary in persisted_hourly
            )
            expected_seconds = max(0.0, (end - start).total_seconds())
            raw_samples = source.temperatures.get(item.source_id, [])
            raw_reason_counts: dict[str, int] = {}
            for sample in raw_samples:
                reason = str(sample.reason or (
                    "valid" if sample.value is not None else "invalid"
                ))
                raw_reason_counts[reason] = raw_reason_counts.get(reason, 0) + 1
            if not item.enabled:
                stage = "disabled"
            elif live is None:
                stage = "live_entity_not_found"
            elif str(live.state).lower() in {"unknown", "unavailable", "none", ""}:
                stage = "live_entity_unavailable"
            elif item.attribute and item.attribute not in live.attributes:
                stage = "configured_attribute_missing"
            elif unit_status != "compatible":
                stage = "incompatible_live_unit"
            elif not raw.get("normalized_samples"):
                stage = "recorder_history_unavailable"
            elif not raw.get("valid_samples"):
                stage = "recorder_samples_invalid"
            elif not hourly:
                stage = "hourly_processing_missing"
            elif not persisted_hourly:
                stage = "hourly_temperature_not_persisted"
            elif not valid_hourly:
                stage = "hourly_temperature_rejected_or_uncovered"
            elif daily and not persisted_daily:
                stage = "daily_temperature_not_persisted"
            elif persisted_daily and not valid_daily:
                stage = "daily_temperature_rejected_or_uncovered"
            elif expected_seconds and coverage_seconds / expected_seconds < 0.95:
                stage = "low_persisted_coverage"
            else:
                stage = "available"
            live_value = None
            if live is not None:
                live_value = (
                    live.attributes.get(item.attribute) if item.attribute else live.state
                )
            sources.append({
                **raw,
                "configured": True,
                "enabled": item.enabled,
                "display_name": item.display_name,
                "role": item.role,
                "selected_representative": item.source_id in {
                    self.config.get("representative_indoor_source_id"),
                    self.config.get("representative_outdoor_source_id"),
                },
                "mapping_effective_at": self.mapping_revision.get("effective_at"),
                "mapping_eligibility": mapping_decision,
                "persisted_source_provenance": next((
                    summary.get("source_provenance")
                    for summary in reversed([*persisted_hourly, *persisted_daily])
                    if summary.get("source_provenance")
                ), None),
                "live_state": live_value,
                "live_last_updated": (
                    live.last_updated.isoformat() if live is not None else None
                ),
                "live_unit": live_unit,
                "unit_validation": unit_status,
                "attribute_available": bool(
                    live is not None and (
                        not item.attribute or item.attribute in live.attributes
                    )
                ),
                "persisted_hourly_summaries": len(persisted_hourly),
                "persisted_valid_hourly_summaries": valid_hourly,
                "persisted_daily_summaries": len(persisted_daily),
                "persisted_valid_daily_summaries": valid_daily,
                "persisted_hourly_coverage_seconds": coverage_seconds,
                "unvalidated_or_stale_seconds": max(
                    0.0, expected_seconds
                    - float(raw.get("validated_coverage_seconds", 0) or 0)
                ),
                "raw_sample_reason_counts": raw_reason_counts,
                "persisted_hourly_coverage_percent": round(
                    coverage_seconds / expected_seconds * 100.0, 2
                ) if expected_seconds else 0.0,
                "complete_through_cutoff": bool(
                    expected_seconds and coverage_seconds + 1e-6 >= expected_seconds
                ),
                "complete_calendar_range": bool(
                    requested_end <= end and expected_seconds
                    and coverage_seconds + 1e-6 >= expected_seconds
                ),
                "rejected_samples": rejections[-100:],
                "missing_interval_count": len(expected_hours) - valid_hourly,
                "missing_intervals": missing_intervals,
                "recoverable_missing_interval_count": recoverable_interval_count,
                "recoverable_missing_intervals": recoverable_intervals,
                "nonrecoverable_interval_count": nonrecoverable_interval_count,
                "nonrecoverable_intervals": nonrecoverable_intervals,
                "temperature_only_hourly_summaries": sum(
                    1 for row in temperature_only_hourly
                    if isinstance(row.get("temperatures", {}).get(item.source_id), dict)
                ),
                "temperature_only_daily_summaries": sum(
                    1 for row in temperature_only_daily
                    if isinstance(row.get("temperatures", {}).get(item.source_id), dict)
                ),
                "pipeline_stage": stage,
            })
        return {
            "start_utc": start.isoformat(), "end_utc": end.isoformat(),
            "requested_start": start_date.isoformat(),
            "requested_end": end_date.isoformat(),
            "bounded_days": (end_date - start_date).days + 1,
            "expected_hourly_intervals": len(expected_hours),
            "query_errors": source.provenance.get("query_errors", []),
            "collection_metrics": source.provenance.get("collection_metrics", {}),
            "energy_history_records": energy_counts,
            "temperature_only_history_records": {
                "hourly": len(temperature_only_hourly),
                "daily": len(temperature_only_daily),
            },
            "estimated_recorder_query_count": int(
                source.provenance.get("collection_metrics", {}).get(
                    "recorder_query_count", 0
                ) or 0
            ),
            "configuration_status": "configured" if configured else "not_configured",
            "representative_sources": {
                "indoor": self.config.get("representative_indoor_source_id"),
                "outdoor": self.config.get("representative_outdoor_source_id"),
            },
            "sources": sources,
        }

    async def async_process_checkpoint(
        self, scheduled_at: datetime, equipment_ids: set[str] | None = None
    ) -> int:
        """Persist the last completed wall-clock cycle using UTC instants."""
        zone = ZoneInfo(self.timezone)
        local_end = scheduled_at.astimezone(zone).replace(minute=0, second=0, microsecond=0)
        local_start = local_end - timedelta(hours=int(self.config.get(CONF_CYCLE_HOURS, 6)))
        start, end = local_start.astimezone(UTC), local_end.astimezone(UTC)
        job_id = await self.hass.async_add_executor_job(
            self.database.start_job, "checkpoint", {"start": start.isoformat(), "end": end.isoformat()}
        )
        try:
            source = await self._async_collect_range(start, end, equipment_ids=equipment_ids)
            changed = 0
            for device, history in source.devices.items():
                classifier = configurable_classifier(
                    history.state_map, kind=history.kind,
                    state_attribute=history.state_attribute,
                    seasonal_fallback=bool(self.config.get(CONF_SEASONAL_FALLBACK, False)),
                )
                result = await self.hass.async_add_executor_job(partial(
                    allocate_interval, start=start, end=end, mode_samples=history.mode,
                    power_samples=history.power, counter_samples=history.counter,
                    classifier=classifier,
                    maximum_power_gap=timedelta(minutes=int(self.config.get("maximum_power_gap_minutes", 30))),
                    maximum_counter_delta_kwh=float(self.config.get("counter_jump_limit_kwh", 500)),
                ))
                payload = {
                    **asdict(result), "coverage_percent": result.coverage_percent,
                    "source_provenance": history.provenance,
                    "collected_at": datetime.now(UTC).isoformat(),
                }
                changed += int(await self.hass.async_add_executor_job(
                    self.database.upsert_checkpoint, start, end, local_start.date().isoformat(),
                    device, payload,
                ))
        except Exception as error:
            await self.hass.async_add_executor_job(
                self.database.finish_job, job_id, "failed", {"error": type(error).__name__}
            )
            raise
        await self.hass.async_add_executor_job(
            self.database.finish_job, job_id, "success", {"changed_devices": changed}
        )
        return changed

    async def async_catch_up(self) -> None:
        local_today = datetime.now(UTC).astimezone(ZoneInfo(self.hass.config.time_zone)).date()
        recovery_reader = getattr(self.database, "resumable_electricity_recovery_jobs", None)
        for job in (await self.hass.async_add_executor_job(recovery_reader) if recovery_reader else []):
            self._schedule_electricity_recovery(int(job["id"]))
        resumable_reader = getattr(
            self.database, "resumable_temperature_backfill_jobs", None
        )
        resumable_jobs = (
            await self.hass.async_add_executor_job(resumable_reader)
            if resumable_reader is not None else []
        )
        for job in resumable_jobs:
            task = self._async_run_temperature_backfill(int(job["id"]))
            create_background = getattr(self.entry, "async_create_background_task", None)
            if create_background is not None:
                create_background(
                    self.hass, task,
                    f"{self.entry.entry_id}_temperature_backfill_{job['id']}",
                )
            else:
                self.hass.async_create_task(task)
        async def startup_temperature_repair() -> None:
            try:
                async with asyncio.timeout(120):
                    await self.async_reprocess_temperature_history(
                        local_today - timedelta(days=1), local_today,
                    )
            except Exception:  # Never prevent hourly/checkpoint/daily recovery.
                _LOGGER.exception("Bounded startup temperature repair failed")

        task_name = f"{self.entry.entry_id}_temperature_repair"
        create_background = getattr(self.entry, "async_create_background_task", None)
        if create_background is not None:
            create_background(self.hass, startup_temperature_repair(), task_name)
        else:  # Test harness and older HA compatibility.
            self.hass.async_create_task(startup_temperature_repair())
        try:
            await self.async_refresh_hourly()
        except Exception:
            _LOGGER.exception("Hourly startup recovery failed")
        try:
            await self.async_recover_checkpoints()
        except Exception:
            _LOGGER.exception("Checkpoint startup recovery failed")
        yesterday = local_today - timedelta(days=1)
        latest = await self.hass.async_add_executor_job(self.database.latest)
        next_day = yesterday if latest is None else date.fromisoformat(latest["local_date"]) + timedelta(days=1)
        next_day = max(next_day, yesterday - timedelta(days=30))
        while next_day <= yesterday:
            try:
                await self.async_process_day(next_day, reason="startup_catchup")
            except Exception:
                _LOGGER.exception("Daily startup recovery failed for %s", next_day)
            next_day += timedelta(days=1)

    async def async_recover_checkpoints(self) -> None:
        """Recreate missed completed cycles after restart, without duplicates."""
        zone = ZoneInfo(self.timezone)
        now = datetime.now(UTC).astimezone(zone)
        latest_hour = max(hour for hour in (0, 6, 12, 18) if hour <= now.hour)
        end = now.replace(hour=latest_hour, minute=0, second=0, microsecond=0)
        if end > now:
            end -= timedelta(hours=6)
        for offset in range(4, -1, -1):
            cycle_end = end - timedelta(hours=6 * offset)
            cycle_start = cycle_end - timedelta(hours=6)
            enabled = {
                item.equipment_id for item in equipment_from_config(self.config) if item.enabled
            }
            missing = await self.hass.async_add_executor_job(
                self.database.missing_checkpoint_devices,
                cycle_start.astimezone(UTC), cycle_end.astimezone(UTC), enabled,
            )
            if missing:
                await self.async_process_checkpoint(cycle_end, equipment_ids=missing)

    async def _async_collect(self, day: date) -> DailyInput:
        start, end = local_day_bounds(day, self.timezone)
        return await self._async_collect_range(start, end)

    async def _async_collect_temperatures(
        self, start: datetime, end: datetime, source_ids: set[str] | None = None,
        *, source_mappings=None,
    ) -> DailyInput:
        """Read only bounded temperature evidence; never query energy or HVAC modes."""
        sources = list(source_mappings) if source_mappings is not None else self._temperature_sources()
        if source_ids is not None:
            sources = [item for item in sources if item.source_id in source_ids]
        entity_ids = sorted({item.entity_id for item in sources if item.entity_id})
        query_errors: list[dict[str, str]] = []
        try:
            history = await self.reader.async_history(entity_ids, start, end) if entity_ids else {}
        except Exception as error:  # Recorder may be temporarily unavailable at startup.
            history = {}
            query_errors.append({"query": "history", "error": type(error).__name__})
        statistic_entities = set()
        for item in sources:
            if not item.entity_id or item.attribute is not None:
                continue
            raw_preview = [
                NumericSample(sample.timestamp, sample.value, sample.reason, item.entity_id,
                              sample.source_unit, sample.normalized_unit)
                for sample in states_to_numeric(
                    history.get(item.entity_id, []), dimension="temperature"
                )
            ]
            if _validated_temperature_coverage(raw_preview, start, end, item) <= 0:
                statistic_entities.add(item.entity_id)
        try:
            statistics = (
                await self.reader.async_statistics(statistic_entities, start, end)
                if statistic_entities else {}
            )
        except Exception as error:
            statistics = {}
            query_errors.append({"query": "statistics", "error": type(error).__name__})

        try:
            influx_mapping = json.loads(self.config.get(CONF_INFLUX_MAPPING, "{}") or "{}")
        except (TypeError, json.JSONDecodeError):
            influx_mapping = {}
        influx = None
        if (
            self.config.get(CONF_INFLUX_ENABLED)
            and self.config.get(CONF_INFLUX_HOST)
            and self.config.get(CONF_INFLUX_DATABASE)
        ):
            influx = InfluxBackfillClient(
                host=self.config[CONF_INFLUX_HOST],
                port=int(self.config.get(CONF_INFLUX_PORT, 8086)),
                database=self.config[CONF_INFLUX_DATABASE],
                username=self.config.get(CONF_INFLUX_USERNAME),
                password=self.config.get(CONF_INFLUX_PASSWORD),
                use_ssl=bool(self.config.get("influx_use_ssl", False)),
                verify_ssl=bool(self.config.get("influx_verify_ssl", True)),
                retention_policy=str(self.config.get("influx_retention_policy", "autogen")),
            )

        def source_unit(entity_id: str) -> str | None:
            state = self.hass.states.get(entity_id)
            return state.attributes.get("unit_of_measurement") if state else None

        temperatures: dict[str, list[NumericSample]] = {}
        quality = {}
        roles = {}
        collection = {}
        used_influx: list[str] = []
        for item in sources:
            state_rows = history.get(item.entity_id, [])
            samples = [
                NumericSample(sample.timestamp, sample.value, sample.reason, item.entity_id,
                              sample.source_unit, sample.normalized_unit)
                for sample in states_to_numeric(
                    state_rows, attribute=item.attribute, dimension="temperature",
                )
            ]
            def validated_coverage(candidate: list[NumericSample]) -> float:
                return _validated_temperature_coverage(candidate, start, end, item)

            candidate_coverage = {"recorder_states": validated_coverage(samples)}
            selected_source = "recorder_states"
            fallback_reason = None
            # Recorder state rows are evidence, not proof of usable coverage.
            # A single carry-in state may cover the whole interval, while many
            # invalid/unavailable rows may cover none of it.  Never replace any
            # valid raw-state segment with a lower-fidelity aggregate source;
            # when safe segment merging cannot be proven the uncovered remainder
            # stays missing.
            if candidate_coverage["recorder_states"] <= 0 and item.attribute is None:
                statistic_samples = statistics_to_numeric_samples(
                    statistics.get(item.entity_id, []),
                    source_unit=source_unit(item.entity_id) or "°C",
                )
                normalized_statistics = [
                    NumericSample(sample.timestamp, sample.value, sample.reason, item.entity_id,
                                  sample.source_unit, sample.normalized_unit)
                    for sample in statistic_samples
                ]
                candidate_coverage["recorder_statistics"] = validated_coverage(
                    normalized_statistics
                )
                if candidate_coverage["recorder_statistics"] > 0:
                    samples = normalized_statistics
                    selected_source = "recorder_statistics"
                    fallback_reason = "recorder_states_no_validated_coverage"
            mapping_key = f"temperature.{item.source_id}"
            mapping = influx_mapping.get(mapping_key) if isinstance(influx_mapping, dict) else None
            if (validated_coverage(samples) <= 0 and influx is not None
                    and isinstance(mapping, dict)):
                try:
                    rows = await self.hass.async_add_executor_job(partial(
                        influx.query, mapping["measurement"], mapping.get("field", "value"),
                        start, end, entity_tag=mapping.get("entity_tag"),
                    ))
                except Exception as error:  # One fallback must not abort other sources.
                    rows = []
                    query_errors.append({
                        "query": "influx", "source_id": item.source_id,
                        "entity_id": item.entity_id, "error": type(error).__name__,
                    })
                if rows:
                    try:
                        if any(
                            not isinstance(row, (tuple, list)) or len(row) != 2
                            or not isinstance(row[0], datetime) for row in rows
                        ):
                            raise ValueError("malformed Influx temperature row")
                        converted = states_to_numeric([
                            SimpleNamespace(
                                last_updated=timestamp, state=value,
                                attributes={"unit_of_measurement": mapping.get("unit")},
                            ) for timestamp, value in rows
                        ], dimension="temperature")
                        if any(sample.value is None for sample in converted):
                            query_errors.append({
                                "query": "influx", "source_id": item.source_id,
                                "entity_id": item.entity_id,
                                "error": "MalformedFallbackData",
                            })
                    except (TypeError, ValueError) as error:
                        converted = []
                        query_errors.append({
                            "query": "influx", "source_id": item.source_id,
                            "entity_id": item.entity_id, "error": type(error).__name__,
                        })
                    influx_samples = [
                        NumericSample(sample.timestamp, sample.value,
                                      (sample.reason or "point_sample"), item.entity_id,
                                      sample.source_unit, sample.normalized_unit)
                        for sample in converted
                    ]
                    candidate_coverage["influx"] = validated_coverage(influx_samples)
                    if candidate_coverage["influx"] > 0:
                        used_influx.append(mapping_key)
                        samples = influx_samples
                        selected_source = "influx"
                        fallback_reason = "recorder_sources_no_validated_coverage"
            selected_coverage = validated_coverage(samples)
            temperatures[item.source_id] = samples
            quality[item.source_id] = item.quality
            roles[item.source_id] = item.role
            valid = [sample for sample in samples if sample.value is not None]
            collection[item.source_id] = {
                "source_id": item.source_id,
                "entity_id": item.entity_id,
                "attribute": item.attribute,
                "role": item.role,
                "mapping_effective_at": self.mapping_revision.get("effective_at"),
                "configured": True,
                "enabled": item.enabled,
                "recorder_state_rows": len(state_rows),
                "recorder_statistics_rows": len(statistics.get(item.entity_id, [])),
                "normalized_samples": len(samples),
                "valid_samples": len(valid),
                "selected_history_source": selected_source,
                "fallback_reason": fallback_reason,
                "candidate_validated_coverage_seconds": candidate_coverage,
                "validated_coverage_seconds": selected_coverage,
                "validated_coverage_percent": (
                    100 * selected_coverage / max((end - start).total_seconds(), 1)
                ),
                "query_errors": [
                    error for error in query_errors
                    if error.get("source_id") == item.source_id
                ],
                "available_start": (
                    min(sample.timestamp for sample in valid).isoformat() if valid else None
                ),
                "available_end": (
                    max(sample.timestamp for sample in valid).isoformat() if valid else None
                ),
                "invalid_reasons": sorted({
                    str(sample.reason) for sample in samples
                    if sample.value is None and sample.reason
                }),
                "source_units": sorted({
                    str(sample.source_unit) for sample in samples if sample.source_unit
                }),
                "normalized_unit": "°C",
            }
        return DailyInput({}, {}, temperatures, provenance={
            "primary_source": "home_assistant_recorder",
            "collector": "temperature_only",
            "period_start_utc": start.isoformat(),
            "period_end_utc": end.isoformat(),
            "raw_history_includes_preceding_state": True,
            "long_term_statistics_fallback": True,
            "influx_backfill_keys": used_influx,
            "query_errors": query_errors,
            "collection_metrics": {
                "requested_source_count": len(sources),
                "requested_entity_count": len(entity_ids),
                "recorder_query_count": int(bool(entity_ids))
                + int(bool(statistic_entities)),
                "influx_query_count": len(used_influx),
                "history_rows": sum(len(rows) for rows in history.values()),
                "statistics_rows": sum(len(rows) for rows in statistics.values()),
                "normalized_samples": sum(
                    int(item["normalized_samples"]) for item in collection.values()
                ),
            },
            "temperature_collection": collection,
        }, temperature_quality=quality, temperature_roles=roles)

    async def _async_collect_range(
        self, start: datetime, end: datetime, *, equipment_ids: set[str] | None = None,
        store_mapping_revision: bool = True, include_temperatures: bool = True, strict_sources: bool = False,
        attest_sources: bool = False, exact_source_identity: bool = False,
    ) -> DailyInput:
        equipment = [item for item in equipment_from_config(self.config) if item.enabled]
        if equipment_ids is not None:
            equipment = [item for item in equipment if item.equipment_id in equipment_ids]
        temperature_sources = self._temperature_sources(equipment=equipment) if include_temperatures else []
        site_sources = self._site_sources()
        entity_ids = [
            entity for item in equipment
            for entity in (item.energy_entity, item.power_entity, item.state_entity)
            if entity
        ]
        entity_ids += [entity for entity in site_sources.values() if entity]
        entity_ids += [item.entity_id for item in temperature_sources]
        history = await self.reader.async_history(sorted(set(entity_ids)), start, end)

        statistic_entities = {item.energy_entity for item in equipment if item.energy_entity}
        statistic_entities |= {entity for entity in site_sources.values() if entity}
        statistic_entities |= {
            item.entity_id for item in temperature_sources
            if item.attribute is None and _validated_temperature_coverage(
                [
                    NumericSample(sample.timestamp, sample.value, sample.reason, item.entity_id,
                                  sample.source_unit, sample.normalized_unit)
                    for sample in states_to_numeric(
                        history.get(item.entity_id, []), dimension="temperature"
                    )
                ], start, end, item,
            ) <= 0
        }
        statistics = await self.reader.async_statistics(statistic_entities, start, end)
        used_influx: list[str] = []
        query_errors: list[dict[str, str]] = []
        try:
            influx_mapping = json.loads(self.config.get(CONF_INFLUX_MAPPING, "{}") or "{}")
        except (TypeError, json.JSONDecodeError):
            influx_mapping = {}
        influx = None
        if self.config.get(CONF_INFLUX_ENABLED) and self.config.get(CONF_INFLUX_HOST) and self.config.get(CONF_INFLUX_DATABASE):
            influx = InfluxBackfillClient(
                host=self.config[CONF_INFLUX_HOST], port=int(self.config.get(CONF_INFLUX_PORT, 8086)),
                database=self.config[CONF_INFLUX_DATABASE], username=self.config.get(CONF_INFLUX_USERNAME),
                password=self.config.get(CONF_INFLUX_PASSWORD),
                use_ssl=bool(self.config.get("influx_use_ssl", False)),
                verify_ssl=bool(self.config.get("influx_verify_ssl", True)),
                retention_policy=str(self.config.get("influx_retention_policy", "autogen")),
            )

        def source_unit(entity_id: str | None) -> str | None:
            state = self.hass.states.get(entity_id) if entity_id else None
            return state.attributes.get("unit_of_measurement") if state else None

        numeric_evidence: dict[str, dict[str, object]] = {}
        counter_sources: dict[str, str] = {}
        counter_fallback_reasons: dict[str, str] = {}

        async def numeric(source_key: str, entity_id: str | None, *, statistics_type: str | None = None,
                          attribute: str | None = None, dimension: str | None = None,
                          temperature_mapping=None) -> list[NumericSample]:
            if not entity_id:
                return []
            raw = [NumericSample(item.timestamp, item.value, item.reason, entity_id,
                                 item.source_unit, item.normalized_unit)
                   for item in states_to_numeric(history.get(entity_id, []), attribute=attribute,
                                                 dimension=dimension)]
            raw_coverage = (
                _validated_temperature_coverage(raw, start, end, temperature_mapping)
                if temperature_mapping is not None else None
            )
            candidates = ({"recorder_states": raw_coverage}
                          if raw_coverage is not None else {})
            raw_usable = len(raw) >= 2
            suspicious_counter = False
            if statistics_type == "counter":
                delta, _resets, rejected = counter_delta(
                    raw, start, end, maximum_delta_kwh=float(self.config.get("counter_jump_limit_kwh", 500))
                )
                raw_usable = delta is not None and not rejected
                suspicious_counter = delta is None and rejected > 0
                if not raw_usable:
                    counter_sources[source_key] = "recorder_states_unreliable"
                    counter_fallback_reasons[source_key] = (
                        "suspicious_reset_or_rejected_change" if rejected else "missing_counter_baseline"
                    )
            if (raw_coverage > 0 if raw_coverage is not None else raw_usable):
                if statistics_type == "counter":
                    counter_sources[source_key] = "recorder_states"
                if temperature_mapping is not None:
                    numeric_evidence[source_key] = {
                        "selected_history_source": "recorder_states",
                        "fallback_reason": None,
                        "candidate_validated_coverage_seconds": candidates,
                    }
                return raw
            if statistics_type:
                statistic_rows = statistics.get(entity_id, [])
                if statistics_type == "counter":
                    # The synthetic baseline is valid only for a complete sequence of
                    # nonnegative hourly changes, not gaps or clamped corrections.
                    cursor = start
                    for row in sorted(statistic_rows, key=lambda item: item.get("start", 0)):
                        stamp, stop, change = row.get("start"), row.get("end"), row.get("change")
                        if isinstance(stamp, (int, float)):
                            stamp = datetime.fromtimestamp(stamp, UTC)
                        if isinstance(stop, (int, float)):
                            stop = datetime.fromtimestamp(stop, UTC)
                        try:
                            valid = (stamp == cursor and stop == cursor + timedelta(hours=1)
                                     and isfinite(float(change)) and float(change) >= 0)
                        except (TypeError, ValueError):
                            valid = False
                        if not valid:
                            break
                        cursor = stop
                    if cursor != end:
                        statistic_rows = []
                from_statistics = (
                    statistics_to_counter_samples(
                        statistic_rows, start,
                        source_unit=source_unit(entity_id) or "kWh",
                    )
                    if statistics_type == "counter"
                    else statistics_to_numeric_samples(
                        statistics.get(entity_id, []), source_unit=source_unit(entity_id) or "°C"
                    )
                )
                if len(from_statistics) >= 2:
                    normalized_statistics = [
                        NumericSample(item.timestamp, item.value, item.reason, entity_id,
                                      item.source_unit, item.normalized_unit)
                        for item in from_statistics
                    ]
                    statistics_coverage = (
                        _validated_temperature_coverage(
                            normalized_statistics, start, end, temperature_mapping
                        ) if temperature_mapping is not None else None
                    )
                    if statistics_coverage is not None:
                        candidates["recorder_statistics"] = statistics_coverage
                    if (statistics_coverage > 0 if statistics_coverage is not None else True):
                        if statistics_type == "counter":
                            delta, _resets, rejected = counter_delta(
                                normalized_statistics, start, end,
                                maximum_delta_kwh=float(self.config.get("counter_jump_limit_kwh", 500)),
                            )
                            if delta is None or rejected:
                                return []
                            counter_sources[source_key] = "recorder_statistics"
                        if temperature_mapping is not None:
                            numeric_evidence[source_key] = {
                                "selected_history_source": "recorder_statistics",
                                "fallback_reason": "recorder_states_no_validated_coverage",
                                "candidate_validated_coverage_seconds": candidates,
                            }
                        return normalized_statistics
            mapping = influx_mapping.get(source_key) if isinstance(influx_mapping, dict) else None
            if (strict_sources and isinstance(mapping, dict) and
                    ((mapping.get("entity_id") != entity_id and
                      (mapping.get("entity_id") is not None or not attest_sources)) or not mapping.get("entity_tag") or
                     (exact_source_identity and mapping.get("entity_tag") != entity_id.partition(".")[2]))):
                query_errors.append({"query": "influx", "source_key": source_key,
                                     "entity_id": entity_id, "error": "SourceIdentityUnverified"})
                mapping = None
            if influx is not None and isinstance(mapping, dict) and not suspicious_counter:
                try:
                    rows = await self.hass.async_add_executor_job(partial(
                        influx.query, mapping["measurement"], mapping.get("field", "value"),
                        start, end, entity_tag=mapping.get("entity_tag"),
                    ))
                except Exception as error:
                    rows = []
                    query_errors.append({
                        "query": "influx", "source_key": source_key,
                        "entity_id": entity_id, "error": type(error).__name__,
                    })
                if rows:
                    influx_unit = mapping.get("unit")
                    try:
                        if any(
                            not isinstance(row, (tuple, list)) or len(row) != 2
                            or not isinstance(row[0], datetime) for row in rows
                        ):
                            raise ValueError("malformed Influx row")
                        converted = states_to_numeric([
                            SimpleNamespace(
                                last_updated=timestamp, state=value,
                                attributes={"unit_of_measurement": influx_unit},
                            )
                            for timestamp, value in rows
                        ], dimension=dimension)
                        if any(sample.value is None for sample in converted):
                            query_errors.append({
                                "query": "influx", "source_key": source_key,
                                "entity_id": entity_id,
                                "error": "MalformedFallbackData",
                            })
                    except (TypeError, ValueError) as error:
                        converted = []
                        query_errors.append({
                            "query": "influx", "source_key": source_key,
                            "entity_id": entity_id, "error": type(error).__name__,
                        })
                    influx_samples = [
                        NumericSample(item.timestamp, item.value,
                                      ((item.reason or "point_sample")
                                       if temperature_mapping is not None else item.reason),
                                      entity_id, item.source_unit, item.normalized_unit)
                        for item in converted
                    ]
                    influx_coverage = (
                        _validated_temperature_coverage(
                            influx_samples, start, end, temperature_mapping
                        ) if temperature_mapping is not None else None
                    )
                    if influx_coverage is not None:
                        candidates["influx"] = influx_coverage
                    if influx_coverage is None or influx_coverage > 0:
                        used_influx.append(source_key)
                        if temperature_mapping is not None:
                            numeric_evidence[source_key] = {
                                "selected_history_source": "influx",
                                "fallback_reason": "recorder_sources_no_validated_coverage",
                                "candidate_validated_coverage_seconds": candidates,
                            }
                        return influx_samples
            if temperature_mapping is not None:
                numeric_evidence[source_key] = {
                    "selected_history_source": "recorder_states",
                    "fallback_reason": "no_candidate_has_validated_coverage",
                    "candidate_validated_coverage_seconds": candidates,
                }
            return [] if statistics_type == "counter" and not raw_usable else raw

        devices = {}
        for item in equipment:
            devices[item.equipment_id] = DeviceHistory(
                counter=await numeric(f"equipment.{item.equipment_id}.energy", item.energy_entity,
                                      statistics_type="counter", dimension="energy"),
                power=await numeric(f"equipment.{item.equipment_id}.power", item.power_entity,
                                    dimension="power"),
                mode=states_to_modes(history.get(item.state_entity, [])) if item.state_entity else [],
                kind=item.kind, state_map=item.state_map, state_attribute=item.state_attribute,
                provenance={
                    "energy_entity": item.energy_entity, "power_entity": item.power_entity,
                    "state_entity": item.state_entity, "equipment_id": item.equipment_id,
                    "source_units": {
                        "energy": source_unit(item.energy_entity),
                        "power": source_unit(item.power_entity),
                    },
                    "normalized_units": {"energy": "kWh", "power": "W"},
                },
            )
        site_counters = {
            name: await numeric(f"site.{name}", entity_id, statistics_type="counter",
                                dimension="energy")
            for name, entity_id in site_sources.items()
        }
        temperatures = {}
        temperature_quality = {}
        temperature_roles = {}
        temperature_collection = {}
        for item in temperature_sources:
            samples = await numeric(
                f"temperature.{item.source_id}", item.entity_id,
                statistics_type="mean" if item.attribute is None else None,
                attribute=item.attribute, dimension="temperature",
                temperature_mapping=item,
            )
            temperatures[item.source_id] = samples
            temperature_quality[item.source_id] = item.quality
            temperature_roles[item.source_id] = item.role
            valid = [sample for sample in samples if sample.value is not None]
            validated_seconds = _validated_temperature_coverage(samples, start, end, item)
            temperature_collection[item.source_id] = {
                "source_id": item.source_id,
                "entity_id": item.entity_id,
                "attribute": item.attribute,
                "role": item.role,
                "mapping_effective_at": self.mapping_revision.get("effective_at"),
                "configured": True,
                "enabled": item.enabled,
                "recorder_state_rows": len(history.get(item.entity_id, [])),
                "recorder_statistics_rows": len(statistics.get(item.entity_id, [])),
                "normalized_samples": len(samples),
                "valid_samples": len(valid),
                "validated_coverage_seconds": validated_seconds,
                "validated_coverage_percent": (
                    100 * validated_seconds / max((end - start).total_seconds(), 1)
                ),
                "available_start": min(
                    (sample.timestamp for sample in valid), default=None
                ).isoformat() if valid else None,
                "available_end": max(
                    (sample.timestamp for sample in valid), default=None
                ).isoformat() if valid else None,
                "invalid_reasons": sorted({
                    str(sample.reason) for sample in samples
                    if sample.value is None and sample.reason
                }),
                "source_units": sorted({
                    str(sample.source_unit) for sample in samples if sample.source_unit
                }),
                "normalized_unit": "°C",
                "query_errors": [
                    error for error in query_errors
                    if error.get("source_key") == f"temperature.{item.source_id}"
                ],
                **numeric_evidence.get(f"temperature.{item.source_id}", {}),
            }
        revision = self.mapping_revision
        if store_mapping_revision:
            await self.hass.async_add_executor_job(
                self.database.store_mapping_revision, revision
            )
        return DailyInput(devices, site_counters, temperatures, provenance={
            "primary_source": "home_assistant_recorder",
            "raw_history_includes_preceding_state": True,
            "long_term_statistics_fallback": True,
            "influx_backfill_keys": used_influx,
            "query_errors": query_errors,
            "collected_at": datetime.now(UTC).isoformat(),
            "mapping_effective_at": revision["effective_at"],
            "temperature_collection": temperature_collection,
            "counter_collection": counter_sources,
            "counter_fallback_reasons": counter_fallback_reasons,
        }, temperature_quality=temperature_quality, temperature_roles=temperature_roles,
            site_configured={key for key, entity in site_sources.items() if entity})

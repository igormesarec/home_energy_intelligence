"""Collection, catch-up, and daily-close orchestration."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from functools import partial
import json
import logging
from math import isfinite
from time import monotonic
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .allocation import allocate_interval, configurable_classifier
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
from .engine import DailyAggregator, DailyInput, DeviceHistory
from .influx import InfluxBackfillClient
from .models import NumericSample
from .recorder import (
    RecorderReader, states_to_modes, states_to_numeric,
    statistics_to_counter_samples, statistics_to_numeric_samples,
)
from .timeutils import local_day_bounds

LEGACY_SITE_KEYS = {"pv": "pv_energy", "import": "grid_import", "export": "grid_export"}
_LOGGER = logging.getLogger(__name__)


class HomeEnergyCoordinator:
    def __init__(self, hass: HomeAssistant, entry, database: EnergyDatabase) -> None:
        self.hass = hass
        self.entry = entry
        self.database = database
        self.config = {**entry.data, **entry.options}
        self.reader = RecorderReader(hass)
        self.timezone = hass.config.time_zone
        self._temperature_repair_lock = asyncio.Lock()

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
        if changed:
            async_dispatcher_send(self.hass, f"{self.entry.entry_id}_updated")
        return changed

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
                }], "error_count": 1,
            }
        started = monotonic()
        changed_daily = changed_hourly = processed_days = 0
        sources_inspected = skipped_complete = sources_with_history = 0
        missing_recorder_data = 0
        recorder_query_count = influx_query_count = 0
        peak_normalized_samples_per_day = 0
        errors: list[dict[str, str]] = []
        configured = {item.source_id: item for item in self._temperature_sources()}

        def complete(summary: object) -> bool:
            if not isinstance(summary, dict):
                return False
            mean = summary.get("mean")
            coverage = float(summary.get("coverage_percent") or 0)
            return (
                isinstance(mean, (int, float)) and isfinite(float(mean))
                and coverage >= 95.0
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
                    available = {
                        source_id for source_id in needed
                        if collection.get(source_id, {}).get("valid_samples", 0) > 0
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
            "elapsed_seconds": round(monotonic() - started, 3),
        }

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
        daily = await self.hass.async_add_executor_job(
            self.database.history_period, start_date.isoformat(), end_date.isoformat()
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
            for interval_start, _interval_end in expected_hours:
                row = hourly_by_start.get(interval_start.isoformat())
                summary = (row.get("temperatures", {}).get(item.source_id)
                           if row else None)
                if row is None:
                    reason = "hourly_record_not_processed"
                elif not isinstance(summary, dict):
                    reason = "temperature_summary_not_persisted"
                elif not summary.get("coverage_seconds"):
                    reasons = [
                        rejection.get("reason", "rejected")
                        for rejection in summary.get("rejections", [])
                    ]
                    reason = ",".join(sorted(set(reasons))) or "no_valid_coverage"
                else:
                    continue
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
                "mapping_effective_at": self.config.get("mapping_effective_at"),
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
                "persisted_hourly_coverage_percent": round(
                    coverage_seconds / expected_seconds * 100.0, 2
                ) if expected_seconds else 0.0,
                "rejected_samples": rejections[-100:],
                "missing_interval_count": len(expected_hours) - valid_hourly,
                "missing_intervals": missing_intervals,
                "pipeline_stage": stage,
            })
        return {
            "start_utc": start.isoformat(), "end_utc": end.isoformat(),
            "bounded_days": (end_date - start_date).days + 1,
            "expected_hourly_intervals": len(expected_hours),
            "query_errors": source.provenance.get("query_errors", []),
            "collection_metrics": source.provenance.get("collection_metrics", {}),
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
    ) -> DailyInput:
        """Read only bounded temperature evidence; never query energy or HVAC modes."""
        sources = self._temperature_sources()
        if source_ids is not None:
            sources = [item for item in sources if item.source_id in source_ids]
        entity_ids = sorted({item.entity_id for item in sources if item.entity_id})
        query_errors: list[dict[str, str]] = []
        try:
            history = await self.reader.async_history(entity_ids, start, end) if entity_ids else {}
        except Exception as error:  # Recorder may be temporarily unavailable at startup.
            history = {}
            query_errors.append({"query": "history", "error": type(error).__name__})
        statistic_entities = {
            item.entity_id for item in sources if item.entity_id and item.attribute is None
        }
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
            if len(samples) < 2 and item.attribute is None:
                statistic_samples = statistics_to_numeric_samples(
                    statistics.get(item.entity_id, []),
                    source_unit=source_unit(item.entity_id) or "°C",
                )
                if len(statistic_samples) >= 2:
                    samples = [
                        NumericSample(sample.timestamp, sample.value, sample.reason, item.entity_id,
                                      sample.source_unit, sample.normalized_unit)
                        for sample in statistic_samples
                    ]
            mapping_key = f"temperature.{item.source_id}"
            mapping = influx_mapping.get(mapping_key) if isinstance(influx_mapping, dict) else None
            if len(samples) < 2 and influx is not None and isinstance(mapping, dict):
                rows = await self.hass.async_add_executor_job(partial(
                    influx.query, mapping["measurement"], mapping.get("field", "value"),
                    start, end, entity_tag=mapping.get("entity_tag"),
                ))
                if rows:
                    used_influx.append(mapping_key)
                    converted = states_to_numeric([
                        SimpleNamespace(
                            last_updated=timestamp, state=value,
                            attributes={"unit_of_measurement": mapping.get("unit")},
                        ) for timestamp, value in rows
                    ], dimension="temperature")
                    samples = [
                        NumericSample(sample.timestamp, sample.value, sample.reason, item.entity_id,
                                      sample.source_unit, sample.normalized_unit)
                        for sample in converted
                    ]
            temperatures[item.source_id] = samples
            quality[item.source_id] = item.quality
            roles[item.source_id] = item.role
            valid = [sample for sample in samples if sample.value is not None]
            collection[item.source_id] = {
                "source_id": item.source_id,
                "entity_id": item.entity_id,
                "attribute": item.attribute,
                "role": item.role,
                "configured": True,
                "enabled": item.enabled,
                "recorder_state_rows": len(state_rows),
                "recorder_statistics_rows": len(statistics.get(item.entity_id, [])),
                "normalized_samples": len(samples),
                "valid_samples": len(valid),
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
        store_mapping_revision: bool = True,
    ) -> DailyInput:
        equipment = [item for item in equipment_from_config(self.config) if item.enabled]
        if equipment_ids is not None:
            equipment = [item for item in equipment if item.equipment_id in equipment_ids]
        temperature_sources = self._temperature_sources(equipment=equipment)
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
        statistic_entities |= {item.entity_id for item in temperature_sources if item.attribute is None}
        statistics = await self.reader.async_statistics(statistic_entities, start, end)
        used_influx: list[str] = []
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

        async def numeric(source_key: str, entity_id: str | None, *, statistics_type: str | None = None,
                          attribute: str | None = None, dimension: str | None = None) -> list[NumericSample]:
            if not entity_id:
                return []
            raw = [NumericSample(item.timestamp, item.value, item.reason, entity_id,
                                 item.source_unit, item.normalized_unit)
                   for item in states_to_numeric(history.get(entity_id, []), attribute=attribute,
                                                 dimension=dimension)]
            if len(raw) >= 2:
                return raw
            if statistics_type:
                from_statistics = (
                    statistics_to_counter_samples(
                        statistics.get(entity_id, []), start,
                        source_unit=source_unit(entity_id) or "kWh",
                    )
                    if statistics_type == "counter"
                    else statistics_to_numeric_samples(
                        statistics.get(entity_id, []), source_unit=source_unit(entity_id) or "°C"
                    )
                )
                if len(from_statistics) >= 2:
                    return [NumericSample(item.timestamp, item.value, item.reason, entity_id,
                                          item.source_unit, item.normalized_unit)
                            for item in from_statistics]
            mapping = influx_mapping.get(source_key) if isinstance(influx_mapping, dict) else None
            if influx is not None and isinstance(mapping, dict):
                rows = await self.hass.async_add_executor_job(partial(
                    influx.query, mapping["measurement"], mapping.get("field", "value"),
                    start, end, entity_tag=mapping.get("entity_tag"),
                ))
                if rows:
                    used_influx.append(source_key)
                    influx_unit = mapping.get("unit")
                    converted = states_to_numeric([
                        SimpleNamespace(
                            last_updated=timestamp, state=value,
                            attributes={"unit_of_measurement": influx_unit},
                        )
                        for timestamp, value in rows
                    ], dimension=dimension)
                    return [NumericSample(item.timestamp, item.value, item.reason, entity_id,
                                          item.source_unit, item.normalized_unit)
                            for item in converted]
            return raw

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
            )
            temperatures[item.source_id] = samples
            temperature_quality[item.source_id] = item.quality
            temperature_roles[item.source_id] = item.role
            valid = [sample for sample in samples if sample.value is not None]
            temperature_collection[item.source_id] = {
                "source_id": item.source_id,
                "entity_id": item.entity_id,
                "attribute": item.attribute,
                "configured": True,
                "enabled": item.enabled,
                "recorder_state_rows": len(history.get(item.entity_id, [])),
                "recorder_statistics_rows": len(statistics.get(item.entity_id, [])),
                "normalized_samples": len(samples),
                "valid_samples": len(valid),
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
            }
        revision = mapping_revision(self.config)
        if store_mapping_revision:
            await self.hass.async_add_executor_job(
                self.database.store_mapping_revision, revision
            )
        return DailyInput(devices, site_counters, temperatures, provenance={
            "primary_source": "home_assistant_recorder",
            "raw_history_includes_preceding_state": True,
            "long_term_statistics_fallback": True,
            "influx_backfill_keys": used_influx,
            "collected_at": datetime.now(UTC).isoformat(),
            "mapping_effective_at": revision["effective_at"],
            "temperature_collection": temperature_collection,
        }, temperature_quality=temperature_quality, temperature_roles=temperature_roles,
            site_configured={key for key, entity in site_sources.items() if entity})

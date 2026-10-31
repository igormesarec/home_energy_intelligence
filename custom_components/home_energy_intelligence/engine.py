"""Pure aggregation engine shared by Home Assistant jobs and tests."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from .allocation import allocate_interval, configurable_classifier, counter_delta
from .configuration import TemperatureQuality
from .models import DailyRecord, NumericSample, StateSample
from .temperature import DEFAULT_LIMITS, time_weighted_temperature
from .timeutils import cycle_boundaries, elapsed_seconds, local_day_bounds


@dataclass(slots=True)
class DeviceHistory:
    counter: list[NumericSample] = field(default_factory=list)
    power: list[NumericSample] = field(default_factory=list)
    mode: list[StateSample] = field(default_factory=list)
    kind: str = "air_conditioner"
    state_map: dict[str, str] = field(default_factory=dict)
    state_attribute: str | None = None
    provenance: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class DailyInput:
    devices: dict[str, DeviceHistory]
    site_counters: dict[str, list[NumericSample]]
    temperatures: dict[str, list[NumericSample]]
    provenance: dict[str, Any] = field(default_factory=dict)
    checkpoints: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    temperature_quality: dict[str, TemperatureQuality] = field(default_factory=dict)
    temperature_roles: dict[str, str] = field(default_factory=dict)
    site_configured: set[str] = field(default_factory=set)


def _sum_optional(values: list[float | None]) -> float | None:
    return None if any(value is None for value in values) else sum(value for value in values if value is not None)


class DailyAggregator:
    """Generate one authoritative record for a local calendar day."""

    def __init__(self, timezone: str, *, cycle_hours: int = 6,
                 temperature_freshness: timedelta | None = timedelta(hours=6),
                 seasonal_fallback: bool = False,
                 maximum_power_gap: timedelta = timedelta(minutes=30),
                 maximum_counter_delta_kwh: float = 500.0,
                 assumed_heating_reporting: str = "separate") -> None:
        self.timezone = timezone
        self.cycle_hours = cycle_hours
        self.temperature_freshness = temperature_freshness
        self.seasonal_fallback = seasonal_fallback
        self.maximum_power_gap = maximum_power_gap
        self.maximum_counter_delta_kwh = maximum_counter_delta_kwh
        self.assumed_heating_reporting = assumed_heating_reporting

    def aggregate(self, day: date, source: DailyInput) -> DailyRecord:
        start, end = local_day_bounds(day, self.timezone)
        boundaries = cycle_boundaries(day, self.timezone, self.cycle_hours)
        return self._aggregate(day, start, end, boundaries, source)

    def aggregate_range(
        self, local_date: date, start: datetime, end: datetime, source: DailyInput,
    ) -> DailyRecord:
        """Aggregate an arbitrary completed interval using the daily contract."""
        return self._aggregate(local_date, start, end, [start, end], source)

    def _aggregate(
        self, day: date, start: datetime, end: datetime,
        boundaries: list[datetime], source: DailyInput,
    ) -> DailyRecord:
        allocations: dict[str, dict[str, Any]] = {}
        meter_totals: list[float | None] = []

        for device, history in source.devices.items():
            categories: dict[str, float] = {}
            residual = 0.0
            residual_known = True
            covered = 0.0
            resets = 0
            rejected = 0
            qualities: list[str] = []
            meter_total = 0.0
            meter_known = True
            classifier = configurable_classifier(
                history.state_map, kind=history.kind,
                state_attribute=history.state_attribute,
                seasonal_fallback=self.seasonal_fallback,
            )
            stored = {
                (item.get("interval_start_utc"), item.get("interval_end_utc")): item
                for item in source.checkpoints.get(device, [])
            }
            checkpoint_intervals = 0
            checkpoint_sources: list[dict[str, Any]] = []
            for interval_start, interval_end in zip(
                boundaries, boundaries[1:], strict=False
            ):
                checkpoint = stored.get((interval_start.isoformat(), interval_end.isoformat()))
                if checkpoint is not None:
                    checkpoint_intervals += 1
                    if checkpoint.get("source_provenance"):
                        checkpoint_sources.append(checkpoint["source_provenance"])
                    interval_meter = checkpoint.get("meter_kwh")
                    interval_residual = checkpoint.get("residual_kwh")
                    interval_categories = checkpoint.get("allocations_kwh", {})
                    interval_covered = float(checkpoint.get("covered_seconds", 0))
                    interval_resets = int(checkpoint.get("reset_count", 0))
                    interval_rejected = int(checkpoint.get("rejected_count", 0))
                    interval_quality = str(checkpoint.get("quality", "missing"))
                else:
                    result = allocate_interval(
                        start=interval_start,
                        end=interval_end,
                        mode_samples=history.mode,
                        power_samples=history.power,
                        counter_samples=history.counter,
                        classifier=classifier,
                        maximum_power_gap=self.maximum_power_gap,
                        maximum_counter_delta_kwh=self.maximum_counter_delta_kwh,
                    )
                    interval_meter = result.meter_kwh
                    interval_residual = result.residual_kwh
                    interval_categories = result.allocations_kwh
                    interval_covered = result.covered_seconds
                    interval_resets = result.reset_count
                    interval_rejected = result.rejected_count
                    interval_quality = result.quality
                if interval_meter is None:
                    meter_known = False
                else:
                    meter_total += float(interval_meter)
                if interval_residual is None:
                    residual_known = False
                else:
                    residual += float(interval_residual)
                for category, kwh in interval_categories.items():
                    categories[category] = categories.get(category, 0.0) + kwh
                covered += interval_covered
                resets += interval_resets
                rejected += interval_rejected
                qualities.append(interval_quality)
            authoritative, daily_resets, daily_rejected = counter_delta(
                history.counter, start, end,
                maximum_delta_kwh=self.maximum_counter_delta_kwh,
            )
            meter_value = authoritative if authoritative is not None else (meter_total if meter_known else None)
            if meter_value is not None:
                # Categories remain checkpoint-derived. Any correction or missing
                # evidence is visible as residual instead of being forced into a
                # confirmed category.
                residual = meter_value - sum(categories.values())
                residual_known = True
            positive_residual = (
                max(0.0, residual)
                if residual_known and history.kind == "heat_pump" else 0.0
            )
            assumed_from_modes = (
                categories.get("heating_assumed", 0.0)
                + (categories.get("unknown", 0.0) if history.kind == "heat_pump" else 0.0)
            )
            reporting_assumed = assumed_from_modes + positive_residual
            remaining_unassigned = 0.0 if residual_known else None
            meter_totals.append(meter_value)
            allocations[device] = {
                "meter_kwh": meter_value,
                "categories": categories,
                "residual_kwh": residual if residual_known else None,
                "coverage_percent": 100 * covered / elapsed_seconds(start, end),
                "covered_seconds": covered,
                "period_seconds": elapsed_seconds(start, end),
                "quality": "good" if qualities and all(q == "good" for q in qualities) else "partial",
                "reset_count": resets + daily_resets,
                "rejected_count": rejected + daily_rejected,
                "measured": True,
                "checkpoint_intervals": checkpoint_intervals,
                "classification_source": "persisted_checkpoints" if checkpoint_intervals else "history_recovery",
                "authoritative_counter_source": "daily_recorder" if authoritative is not None else "checkpoints",
                "source_provenance": history.provenance,
                "checkpoint_source_provenance": checkpoint_sources,
                "assumed_heating_reporting": self.assumed_heating_reporting,
                "reported_assumed_heating_kwh": reporting_assumed,
                "uncertain_residual_kwh": residual if residual_known else None,
                "shelly_total_kwh": meter_value,
                "heating_confirmed_kwh": categories.get("confirmed_heating", 0.0),
                "dhw_confirmed_kwh": categories.get("dhw", 0.0),
                "idle_other_kwh": (
                    categories.get("idle", 0.0) + categories.get("cooling", 0.0)
                ),
                "heating_assumed_unknown_mode_kwh": assumed_from_modes,
                "positive_reconciliation_residual_kwh": positive_residual,
                "heating_assumed_residual_kwh": positive_residual,
                "raw_unassigned_before_fallback_kwh": (
                    assumed_from_modes + positive_residual
                ),
                "remaining_unassigned_after_fallback_kwh": remaining_unassigned,
                "signed_overallocation_or_reconciliation_error_kwh": (
                    residual if residual_known and residual < 0 else 0.0
                ),
                "classification_coverage_seconds": covered,
                "classification_expected_seconds": elapsed_seconds(start, end),
                "reported_heating_kwh": None if meter_value is None else (
                    categories.get("confirmed_heating", 0.0) + categories.get("heating", 0.0)
                    + (reporting_assumed
                       if self.assumed_heating_reporting == "include_in_heating_total" else 0.0)
                ),
            }

        site_values: dict[str, float | None] = {}
        for key in ("pv", "import", "export"):
            site_values[key] = counter_delta(
                source.site_counters.get(key, []), start, end,
                maximum_delta_kwh=self.maximum_counter_delta_kwh,
            )[0]
        configured_site = source.site_configured or {
            key for key, samples in source.site_counters.items() if samples
        }
        pv_term = 0.0 if "pv" not in configured_site else site_values["pv"]
        household = _sum_optional([
            pv_term, site_values["import"],
            None if site_values["export"] is None else -site_values["export"],
        ])
        hvac_total = _sum_optional(meter_totals)
        non_hvac = None if household is None or hvac_total is None else household - hvac_total

        temperatures: dict[str, dict[str, Any]] = {}
        for key, samples in source.temperatures.items():
            role = source.temperature_roles.get(key, key)
            kind = "outdoor" if role == "outdoor" else "flow" if role in {"supply", "return"} else "dhw" if role == "dhw" else "indoor"
            configured = source.temperature_quality.get(key)
            minimum, maximum = (
                (configured.minimum, configured.maximum) if configured else DEFAULT_LIMITS[kind]
            )
            result = time_weighted_temperature(
                samples, start, end, minimum_valid=minimum, maximum_valid=maximum,
                max_carry=(timedelta(minutes=configured.freshness_minutes)
                           if configured else self.temperature_freshness),
                invalid_values=configured.invalid_values if configured else (-55.0, 155.0),
                maximum_change_per_minute=(configured.maximum_change_per_minute if configured else None),
                source=key,
            )
            serialized = asdict(result)
            serialized["rejections"] = [
                {**item, "timestamp": item["timestamp"].isoformat()}
                for item in serialized["rejections"]
            ]
            missing_seconds = result.period_seconds - result.coverage_seconds
            missing_threshold_exceeded = bool(
                configured and missing_seconds >= configured.missing_after_minutes * 60
            )
            temperatures[key] = {
                **serialized, "coverage_percent": result.coverage_percent,
                "full_temporal_coverage": result.coverage_seconds >= result.period_seconds,
                "missing_seconds": missing_seconds,
                "missing_threshold_exceeded": missing_threshold_exceeded,
                "quality": "missing" if missing_threshold_exceeded else result.quality,
                "period_start_utc": start.isoformat(),
                "period_end_utc": end.isoformat(),
                "source_provenance": (
                    source.provenance.get("temperature_collection", {}).get(key)
                    if isinstance(source.provenance, dict) else None
                ),
            }

        quality_flags = [data["quality"] for data in allocations.values()]
        quality_flags.extend(data["quality"] for data in temperatures.values())
        if "recorder_statistics" in source.provenance.get("counter_collection", {}).values():
            quality_flags.append("partial")
        if household is None:
            quality = "missing"
        elif non_hvac is not None and non_hvac < -0.05:
            quality = "unreconciled"
        elif any(flag in {"missing", "poor", "partial"} for flag in quality_flags):
            quality = "partial"
        else:
            quality = "good"

        return DailyRecord(
            local_date=day.isoformat(), timezone=self.timezone,
            period_start_utc=start.isoformat(), period_end_utc=end.isoformat(),
            day_seconds=int(elapsed_seconds(start, end)), household_kwh=household,
            pv_kwh=site_values["pv"], import_kwh=site_values["import"],
            export_kwh=site_values["export"], non_hvac_kwh=non_hvac,
            quality=quality, temperatures=temperatures, allocations=allocations,
            provenance=source.provenance,
        )

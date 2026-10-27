"""Pure aggregation engine shared by Home Assistant jobs and tests."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from functools import partial
from typing import Any

from .allocation import ac_category, allocate_interval, counter_delta, ecodan_category
from .models import DailyRecord, NumericSample, StateSample
from .temperature import DEFAULT_LIMITS, time_weighted_temperature
from .timeutils import cycle_boundaries, elapsed_seconds, local_day_bounds


@dataclass(slots=True)
class DeviceHistory:
    counter: list[NumericSample] = field(default_factory=list)
    power: list[NumericSample] = field(default_factory=list)
    mode: list[StateSample] = field(default_factory=list)


@dataclass(slots=True)
class DailyInput:
    devices: dict[str, DeviceHistory]
    site_counters: dict[str, list[NumericSample]]
    temperatures: dict[str, list[NumericSample]]
    provenance: dict[str, Any] = field(default_factory=dict)


def _sum_optional(values: list[float | None]) -> float | None:
    return None if any(value is None for value in values) else sum(value for value in values if value is not None)


class DailyAggregator:
    """Generate one authoritative record for a local calendar day."""

    def __init__(self, timezone: str, *, cycle_hours: int = 6,
                 temperature_freshness: timedelta | None = timedelta(hours=6),
                 seasonal_fallback: bool = False) -> None:
        self.timezone = timezone
        self.cycle_hours = cycle_hours
        self.temperature_freshness = temperature_freshness
        self.seasonal_fallback = seasonal_fallback

    def aggregate(self, day: date, source: DailyInput) -> DailyRecord:
        start, end = local_day_bounds(day, self.timezone)
        boundaries = cycle_boundaries(day, self.timezone, self.cycle_hours)
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
            classifier = ecodan_category if device == "ecodan" else partial(
                ac_category, seasonal_fallback=self.seasonal_fallback
            )
            for interval_start, interval_end in zip(boundaries, boundaries[1:]):
                result = allocate_interval(
                    start=interval_start,
                    end=interval_end,
                    mode_samples=history.mode,
                    power_samples=history.power,
                    counter_samples=history.counter,
                    classifier=classifier,
                )
                if result.meter_kwh is None:
                    meter_known = False
                else:
                    meter_total += result.meter_kwh
                if result.residual_kwh is None:
                    residual_known = False
                else:
                    residual += result.residual_kwh
                for category, kwh in result.allocations_kwh.items():
                    categories[category] = categories.get(category, 0.0) + kwh
                covered += result.covered_seconds
                resets += result.reset_count
                rejected += result.rejected_count
                qualities.append(result.quality)
            meter_value = meter_total if meter_known else None
            meter_totals.append(meter_value)
            allocations[device] = {
                "meter_kwh": meter_value,
                "categories": categories,
                "residual_kwh": residual if residual_known else None,
                "coverage_percent": 100 * covered / elapsed_seconds(start, end),
                "quality": "good" if qualities and all(q == "good" for q in qualities) else "partial",
                "reset_count": resets,
                "rejected_count": rejected,
                "measured": True,
            }

        site_values: dict[str, float | None] = {}
        for key in ("pv", "import", "export"):
            site_values[key] = counter_delta(source.site_counters.get(key, []), start, end)[0]
        household = _sum_optional([
            site_values["pv"], site_values["import"],
            None if site_values["export"] is None else -site_values["export"],
        ])
        hvac_total = _sum_optional(meter_totals)
        non_hvac = None if household is None or hvac_total is None else household - hvac_total

        temperatures: dict[str, dict[str, Any]] = {}
        for key, samples in source.temperatures.items():
            kind = (
                "outdoor" if key == "outdoor" else
                "flow" if key == "flow" else
                "dhw" if key == "dhw_tank" else "indoor"
            )
            minimum, maximum = DEFAULT_LIMITS[kind]
            result = time_weighted_temperature(
                samples, start, end, minimum_valid=minimum, maximum_valid=maximum,
                max_carry=self.temperature_freshness,
            )
            temperatures[key] = {**asdict(result), "coverage_percent": result.coverage_percent,
                                 "quality": result.quality}

        quality_flags = [data["quality"] for data in allocations.values()]
        quality_flags.extend(data["quality"] for data in temperatures.values())
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

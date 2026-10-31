"""Six-hour meter reconciliation and HVAC mode allocation."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from math import isfinite

from .models import AllocationResult, NumericSample, StateSample
from .timeutils import elapsed_seconds

ECODAN_MODE_MAP = {
    "heating_zones": "confirmed_heating",
    "heating_water": "dhw",
    "cooling": "cooling",
    "idle": "idle",
}

VALID_HEAT_PUMP_CATEGORIES = {
    "confirmed_heating", "heating_assumed", "dhw", "cooling", "idle", "unknown"
}
VALID_AC_CATEGORIES = {
    "heating", "cooling", "standby", "dry_fan", "unknown",
    "heating_selected", "cooling_selected", "heating_estimated", "cooling_estimated",
}


def ecodan_category(state: StateSample) -> str:
    """Map Ecodan operation without hiding uncertainty."""
    return ECODAN_MODE_MAP.get(state.state.lower(), "heating_assumed")


def configurable_classifier(
    state_map: dict[str, str],
    *,
    kind: str,
    state_attribute: str | None = None,
    seasonal_fallback: bool = False,
) -> Callable[[StateSample], str]:
    """Build a classifier from UI configuration without vendor assumptions."""
    normalized = {str(key).lower(): str(value) for key, value in state_map.items()}
    allowed = VALID_HEAT_PUMP_CATEGORIES if kind == "heat_pump" else VALID_AC_CATEGORIES
    if not set(normalized.values()).issubset(allowed):
        raise ValueError("state mapping contains unsupported allocation categories")

    def classify(sample: StateSample) -> str:
        raw = sample.attributes.get(state_attribute) if state_attribute else sample.state
        mapped = normalized.get(str(raw).lower())
        if mapped:
            return mapped
        if kind == "heat_pump":
            return "heating_assumed"
        return ac_category(sample, seasonal_fallback=seasonal_fallback)

    return classify


def ac_category(state: StateSample, *, seasonal_fallback: bool = False) -> str:
    """Classify AC electricity from selected mode before transient action."""
    action = str(state.attributes.get("hvac_action", "")).lower()
    selected = state.state.lower()
    selected_map = {
        "heat": "heating_selected", "heating": "heating_selected",
        "cool": "cooling_selected", "cooling": "cooling_selected",
        "off": "standby", "idle": "standby",
        "dry": "dry_fan", "fan_only": "dry_fan", "fan": "dry_fan",
    }
    if selected in selected_map:
        return selected_map[selected]
    action_map = {
        "heating": "heating",
        "cooling": "cooling",
        "drying": "dry_fan",
        "fan": "dry_fan",
        "idle": "standby",
        "off": "standby",
    }
    if action in action_map:
        return action_map[action]
    if seasonal_fallback:
        return "cooling_estimated" if 5 <= state.timestamp.month <= 9 else "heating_estimated"
    return "unknown"


def counter_delta(
    samples: list[NumericSample],
    start: datetime,
    end: datetime,
    *,
    maximum_delta_kwh: float = 500.0,
) -> tuple[float | None, int, int]:
    """Calculate a cumulative-counter delta with reset and spike handling."""
    valid = sorted(
        (
            sample
            for sample in samples
            if sample.value is not None
            and isfinite(sample.value)
            and sample.value >= 0
            and sample.timestamp <= end
        ),
        key=lambda item: item.timestamp,
    )
    before = [sample for sample in valid if sample.timestamp <= start]
    if not before:
        return None, 0, 0
    observations = [before[-1], *[sample for sample in valid if start < sample.timestamp <= end]]
    if len(observations) < 2:
        return None, 0, 0

    # A drop followed by a return to the pre-drop cumulative level is ambiguous:
    # treating it as a reset counts the old lifetime reading as new energy.
    # Leave it unavailable so the collector can use complete hourly statistics.
    for index in range(1, len(observations) - 1):
        previous_value = observations[index - 1].value
        current_value = observations[index].value
        if (previous_value is not None and current_value is not None
                and current_value < previous_value
                and any(item.value is not None and item.value >= previous_value
                        for item in observations[index + 1:])):
            return None, 1, 1

    total = 0.0
    resets = 0
    rejected = 0
    previous = observations[0].value
    assert previous is not None
    for sample in observations[1:]:
        current = sample.value
        assert current is not None
        change = current - previous
        if change < 0:
            resets += 1
            change = current
        if change > maximum_delta_kwh:
            rejected += 1
        else:
            total += change
        previous = current
    return total, resets, rejected


def _at_or_before(samples, instant):
    candidates = [sample for sample in samples if sample.timestamp <= instant]
    return max(candidates, key=lambda item: item.timestamp) if candidates else None


def allocate_interval(
    *,
    start: datetime,
    end: datetime,
    mode_samples: list[StateSample],
    power_samples: list[NumericSample],
    counter_samples: list[NumericSample],
    classifier: Callable[[StateSample], str],
    maximum_power_w: float = 25_000.0,
    maximum_power_gap: timedelta = timedelta(minutes=30),
    maximum_counter_delta_kwh: float = 500.0,
    reconciliation_tolerance_kwh: float = 0.05,
) -> AllocationResult:
    """Allocate energy by transitions, retaining any reconciliation residual."""
    period_seconds = elapsed_seconds(start, end)
    if period_seconds <= 0:
        raise ValueError("end must be after start")

    modes = sorted((sample for sample in mode_samples if sample.timestamp < end), key=lambda x: x.timestamp)
    powers = sorted((sample for sample in power_samples if sample.timestamp < end), key=lambda x: x.timestamp)
    boundaries = {start, end}
    boundaries.update(sample.timestamp for sample in modes if start < sample.timestamp < end)
    boundaries.update(sample.timestamp for sample in powers if start < sample.timestamp < end)
    instants = sorted(boundaries)
    allocations: dict[str, float] = {}
    covered_seconds = 0.0
    rejected = 0

    for segment_start, segment_end in zip(instants, instants[1:], strict=False):
        mode = _at_or_before(modes, segment_start)
        power = _at_or_before(powers, segment_start)
        if mode is None or power is None or power.value is None:
            continue
        if segment_start - power.timestamp > maximum_power_gap:
            continue
        if not isfinite(power.value) or power.value < 0 or power.value > maximum_power_w:
            rejected += 1
            continue
        usable_end = min(segment_end, power.timestamp + maximum_power_gap)
        if usable_end <= segment_start:
            continue
        seconds = elapsed_seconds(segment_start, usable_end)
        category = classifier(mode)
        allocations[category] = allocations.get(category, 0.0) + power.value * seconds / 3_600_000
        covered_seconds += seconds

    meter_kwh, reset_count, counter_rejected = counter_delta(
        counter_samples, start, end, maximum_delta_kwh=maximum_counter_delta_kwh
    )
    allocated_total = sum(allocations.values())
    residual = None if meter_kwh is None else meter_kwh - allocated_total
    if residual is not None and abs(residual) <= reconciliation_tolerance_kwh:
        residual = 0.0
        if allocated_total > 0:
            scale = meter_kwh / allocated_total
            allocations = {key: value * scale for key, value in allocations.items()}

    coverage = 100.0 * covered_seconds / period_seconds
    total_rejected = rejected + counter_rejected
    if meter_kwh is None:
        quality = "missing_counter"
    elif coverage < 50:
        quality = "poor"
    elif coverage < 95 or (residual is not None and abs(residual) > reconciliation_tolerance_kwh):
        quality = "partial"
    elif reset_count or total_rejected:
        quality = "filtered"
    else:
        quality = "good"
    uncertain_categories = {
        "heating_assumed", "unknown", "heating_selected", "cooling_selected",
        "heating_estimated", "cooling_estimated",
    }
    if quality == "good" and uncertain_categories.intersection(allocations):
        quality = "uncertain"
    return AllocationResult(
        meter_kwh,
        allocations,
        residual,
        covered_seconds,
        period_seconds,
        reset_count,
        total_rejected,
        quality,
    )

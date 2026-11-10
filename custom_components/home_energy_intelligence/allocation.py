"""Six-hour meter reconciliation and HVAC mode allocation."""

from __future__ import annotations

from collections.abc import Callable
from bisect import bisect_right, bisect_left
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
    strict_historical: bool = False,
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
        if strict_historical:
            return "unknown"
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


def historical_counter_observations(samples: list[NumericSample]) -> tuple[list[NumericSample], list[datetime]]:
    """Turn conflicting equal-timestamp readings into gaps, never choose a value."""
    grouped: dict[datetime, NumericSample] = {}
    conflicts: set[datetime] = set()
    for sample in samples:
        previous = grouped.get(sample.timestamp)
        if previous is not None and previous.value != sample.value:
            conflicts.add(sample.timestamp)
        else:
            grouped[sample.timestamp] = sample
    return ([NumericSample(stamp, None) if stamp in conflicts else grouped[stamp]
             for stamp in sorted(grouped)], sorted(conflicts))


def allocate_historical_counter_increments(*, start: datetime, end: datetime,
                                           mode_samples: list[StateSample],
                                           counter_samples: list[NumericSample],
                                           classifier: Callable[[StateSample], str],
                                           power_samples: list[NumericSample] | None = None,
                                           maximum_gap: timedelta = timedelta(hours=1),
                                           maximum_delta_kwh: float = 500.0,
                                           trace: list[dict] | None = None) -> AllocationResult:
    """Allocate whole observed increments only; never interpolate across a boundary."""
    samples, conflicts = historical_counter_observations(
        [item for item in counter_samples if start <= item.timestamp <= end])
    def valid_value(item):
        return item.value is not None and isfinite(item.value) and item.value >= 0
    modes = sorted((item for item in mode_samples if item.timestamp < end), key=lambda item: item.timestamp)
    mode_times = [item.timestamp for item in modes]
    allocations: dict[str, float] = {}
    measured = covered = 0.0
    accepted = resets = 0
    rejected = len(conflicts)
    if trace is not None:
        for stamp in conflicts:
            trace.append({"status": "rejected", "timestamp": stamp.isoformat(), "reason": "conflicting_counter_timestamp"})
    suppressed: set[int] = set()
    for index, (left, right) in enumerate(zip(samples, samples[1:])):
        if valid_value(left) and valid_value(right) and right.value < left.value:
            for recovery in range(index + 2, len(samples)):
                if samples[recovery].timestamp - left.timestamp > maximum_gap * 2:
                    break
                if not valid_value(samples[recovery]):
                    break
                if samples[recovery].value >= left.value:
                    suppressed.update(range(index, recovery))
                    break
    for pair_index, (left, right) in enumerate(zip(samples, samples[1:])):
        duration = right.timestamp-left.timestamp
        entry = {"start": left.timestamp.isoformat(), "end": right.timestamp.isoformat(),
                 "delta_kwh": right.value-left.value if valid_value(left) and valid_value(right) else None} if trace is not None else None
        def rejected_trace(reason):
            if trace is not None:
                trace.append({**entry, "status": "rejected", "reason": reason})
        if not valid_value(left) or not valid_value(right):
            rejected += 1
            rejected_trace("invalid_or_conflicting_counter")
            continue
        delta = right.value-left.value
        if pair_index in suppressed:
            resets += int(delta < 0)
            rejected += 1
            rejected_trace("transient_reset_sequence")
            continue
        if duration <= timedelta(0) or duration > maximum_gap:
            rejected += 1
            rejected_trace("counter_gap_or_nonpositive_duration")
            continue
        if delta < 0:
            resets += 1
            rejected_trace("counter_reset")
            continue
        if delta > maximum_delta_kwh:
            rejected += 1
            rejected_trace("counter_jump_limit")
            continue
        index = bisect_right(mode_times, left.timestamp)
        state = modes[index-1] if index else None
        category = classifier(state) if state else "unknown"
        changes = [classifier(item) for item in modes[index:bisect_left(mode_times, right.timestamp)]]
        transition = any(item != category for item in changes)
        split_allocations = None
        if transition:
            refined = allocate_interval(start=left.timestamp, end=right.timestamp,
                mode_samples=modes, power_samples=power_samples or [], counter_samples=[left, right],
                classifier=classifier, maximum_counter_delta_kwh=maximum_delta_kwh)
            supported = (refined.covered_seconds >= duration.total_seconds()
                         and "unknown" not in refined.allocations_kwh
                         and refined.residual_kwh is not None
                         and abs(refined.residual_kwh) <= 0.05
                         and sum(refined.allocations_kwh.values()) > 0
                         and abs(sum(refined.allocations_kwh.values()) - delta) <= 1e-9)
            if supported:
                for name, amount in refined.allocations_kwh.items():
                    key = name + "_power_estimate"
                    allocations[key] = allocations.get(key, 0.0) + amount
                split_allocations = {name + "_power_estimate": amount for name, amount in refined.allocations_kwh.items()}
                category = "power_estimate"
            else:
                category = "unknown"
        if category != "power_estimate":
            allocations[category] = allocations.get(category, 0.0) + delta
        measured += delta
        if category != "unknown":
            covered += duration.total_seconds()
        if trace is not None:
            trace.append({**entry, "status": "accepted", "category": category,
                          "reason": ("power_supported_transition" if split_allocations is not None else
                                     "unsupported_transition" if transition else
                                     "no_mode_evidence" if state is None else
                                     "unknown_mode" if category == "unknown" else "mode_classification"),
                          "mode_transition": transition, "power_supported": split_allocations is not None,
                          "split_allocations_kwh": split_allocations,
                          "state": None if state is None else {"timestamp": state.timestamp.isoformat(), "value": state.state,
                              "source": state.attributes.get("source"), "timestamp_basis": "normalized_mode_transition_not_observation"}})
        accepted += 1
    period = elapsed_seconds(start, end)
    quality = "missing_counter" if not accepted else "partial" if covered < period or resets or rejected else "uncertain" if any(key in allocations or any(k.endswith("_power_estimate") for k in allocations) for key in ("unknown", "heating_assumed", "heating_selected", "cooling_selected")) else "good"
    return AllocationResult(measured if accepted else None, allocations, 0.0 if accepted else None,
                            covered, period, resets, rejected, quality)

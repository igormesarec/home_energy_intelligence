"""Time-weighted temperature calculations with explicit missing coverage."""

from __future__ import annotations

from datetime import datetime, timedelta

from .models import NumericSample, RejectedReading, TemperatureResult
from .timeutils import elapsed_seconds


DEFAULT_LIMITS = {
    "outdoor": (-50.0, 60.0),
    "indoor": (-10.0, 60.0),
    "flow": (-10.0, 110.0),
    "dhw": (0.0, 100.0),
}


def _rejection_reason(
    sample: NumericSample,
    *,
    minimum_valid: float,
    maximum_valid: float,
    invalid_values: tuple[float, ...],
    prior_valid: NumericSample | None,
    maximum_change_per_minute: float | None,
) -> str | None:
    if sample.value is None:
        return None
    if sample.value in invalid_values:
        return "invalid_sentinel"
    if not minimum_valid <= sample.value <= maximum_valid:
        return "out_of_range"
    if maximum_change_per_minute is not None and prior_valid is not None and prior_valid.value is not None:
        minutes = max(elapsed_seconds(prior_valid.timestamp, sample.timestamp) / 60.0, 1 / 60)
        if abs(sample.value - prior_valid.value) / minutes > maximum_change_per_minute:
            return "temporal_spike"
    return None


def time_weighted_temperature(
    samples: list[NumericSample],
    start: datetime,
    end: datetime,
    *,
    minimum_valid: float,
    maximum_valid: float,
    max_carry: timedelta | None = None,
    invalid_values: tuple[float, ...] = (-55.0, 155.0),
    maximum_change_per_minute: float | None = None,
    source: str = "unknown",
) -> TemperatureResult:
    """Calculate mean/min/max weighted by elapsed seconds.

    The input should include the last observation at or before ``start``. A None
    value is an explicit unavailable marker and ends coverage until a new valid
    reading arrives. Outliers are rejected without altering the source history.
    """
    period_seconds = elapsed_seconds(start, end)
    if period_seconds <= 0:
        raise ValueError("end must be after start")

    ordered = sorted((item for item in samples if item.timestamp < end), key=lambda item: item.timestamp)
    preceding = [item for item in ordered if item.timestamp <= start]
    active = preceding[-1:] + [item for item in ordered if start < item.timestamp < end]
    if not active:
        return TemperatureResult(None, None, None, 0.0, period_seconds, 0, (), ())

    weighted_sum = 0.0
    coverage = 0.0
    accepted_values: list[float] = []
    rejected_reasons: list[str] = []
    rejections: list[RejectedReading] = []
    prior_valid: NumericSample | None = None

    for index, sample in enumerate(active):
        segment_start = max(start, sample.timestamp)
        segment_end = end if index + 1 == len(active) else min(end, active[index + 1].timestamp)
        # Recorder state history contains state changes, not polling samples. A
        # value that remains unchanged is still the active HA state and must not
        # become stale merely because its value did not change. Carry limits are
        # only applied to explicitly point-in-time/backfill observations.
        if max_carry is not None and sample.reason == "point_sample":
            segment_end = min(segment_end, sample.timestamp + max_carry)
        if segment_end <= segment_start:
            continue
        if sample.value is None:
            prior_valid = None
            if sample.reason and sample.reason not in {"unknown", "unavailable", "statistics_interval_end"}:
                rejected_reasons.append(sample.reason)
            continue
        rejection = _rejection_reason(
            sample,
            minimum_valid=minimum_valid,
            maximum_valid=maximum_valid,
            invalid_values=invalid_values,
            prior_valid=prior_valid,
            maximum_change_per_minute=maximum_change_per_minute,
        )
        if rejection is None and sample.reason and sample.reason.startswith("statistics_bounds:"):
            try:
                _prefix, bound_minimum, bound_maximum = sample.reason.split(":", 2)
                bounds = (float(bound_minimum), float(bound_maximum))
                if any(
                    value in invalid_values or not minimum_valid <= value <= maximum_valid
                    for value in bounds
                ):
                    rejection = "contaminated_statistics_bounds"
            except ValueError:
                rejection = "invalid_statistics_bounds"
        if rejection:
            rejected_reasons.append(f"{rejection}:{sample.value:g}")
            rejections.append(RejectedReading(
                timestamp=sample.timestamp,
                value=sample.value,
                duration_seconds=elapsed_seconds(segment_start, segment_end),
                reason=rejection,
                source=sample.source or source,
            ))
            continue
        seconds = elapsed_seconds(segment_start, segment_end)
        weighted_sum += sample.value * seconds
        coverage += seconds
        accepted_values.append(sample.value)
        prior_valid = sample
        if sample.reason and sample.reason.startswith("statistics_bounds:"):
            try:
                _prefix, minimum, maximum = sample.reason.split(":", 2)
                accepted_values.extend((float(minimum), float(maximum)))
            except ValueError:
                # Malformed bounds are rejected above and cannot reach here.
                pass

    if coverage == 0:
        return TemperatureResult(
            None, None, None, 0.0, period_seconds, len(rejected_reasons),
            tuple(rejected_reasons), tuple(rejections)
        )
    return TemperatureResult(
        weighted_sum / coverage,
        min(accepted_values),
        max(accepted_values),
        coverage,
        period_seconds,
        len(rejected_reasons),
        tuple(rejected_reasons),
        tuple(rejections),
    )

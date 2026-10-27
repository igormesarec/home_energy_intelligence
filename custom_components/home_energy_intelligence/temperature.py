"""Time-weighted temperature calculations with explicit missing coverage."""

from __future__ import annotations

from datetime import datetime, timedelta

from .models import NumericSample, TemperatureResult
from .timeutils import elapsed_seconds


DEFAULT_LIMITS = {
    "outdoor": (-60.0, 70.0),
    "indoor": (-10.0, 60.0),
    "flow": (-10.0, 110.0),
    "dhw": (0.0, 100.0),
}


def time_weighted_temperature(
    samples: list[NumericSample],
    start: datetime,
    end: datetime,
    *,
    minimum_valid: float,
    maximum_valid: float,
    max_carry: timedelta | None = None,
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
        return TemperatureResult(None, None, None, 0.0, period_seconds, 0, ())

    weighted_sum = 0.0
    coverage = 0.0
    accepted_values: list[float] = []
    rejected_reasons: list[str] = []

    for index, sample in enumerate(active):
        segment_start = max(start, sample.timestamp)
        segment_end = end if index + 1 == len(active) else min(end, active[index + 1].timestamp)
        if max_carry is not None:
            segment_end = min(segment_end, sample.timestamp + max_carry)
        if segment_end <= segment_start:
            continue
        if sample.value is None:
            if sample.reason and sample.reason not in {"unknown", "unavailable", "statistics_interval_end"}:
                rejected_reasons.append(sample.reason)
            continue
        if not minimum_valid <= sample.value <= maximum_valid:
            rejected_reasons.append(f"out_of_range:{sample.value:g}")
            continue
        seconds = elapsed_seconds(segment_start, segment_end)
        weighted_sum += sample.value * seconds
        coverage += seconds
        accepted_values.append(sample.value)
        if sample.reason and sample.reason.startswith("statistics_bounds:"):
            try:
                _prefix, minimum, maximum = sample.reason.split(":", 2)
                accepted_values.extend((float(minimum), float(maximum)))
            except ValueError:
                rejected_reasons.append("invalid_statistics_bounds")

    if coverage == 0:
        return TemperatureResult(
            None, None, None, 0.0, period_seconds, len(rejected_reasons), tuple(rejected_reasons)
        )
    return TemperatureResult(
        weighted_sum / coverage,
        min(accepted_values),
        max(accepted_values),
        coverage,
        period_seconds,
        len(rejected_reasons),
        tuple(rejected_reasons),
    )

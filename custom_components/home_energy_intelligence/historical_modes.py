"""Pure historical state-priority logic; no collection or persistence side effects."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Mapping

from .models import StateSample


def historical_heat_pump_modes(
    start: datetime, end: datetime, *, primary: list[StateSample],
    actual_dhw: list[StateSample], fallback: list[StateSample],
    state_map: Mapping[str, str], maximum_age: timedelta = timedelta(hours=1),
) -> list[StateSample]:
    """Return explicit state changes with fresh primary > actual DHW > selected heat.

    The caller must establish historical mapping identity and ensure actual_dhw
    is an observed operation source, never a command or inferred tank warming.
    """
    if start.tzinfo is None or end.tzinfo is None or end <= start or maximum_age <= timedelta(0):
        raise ValueError("a bounded aware interval and positive freshness are required")
    allowed = {"confirmed_heating", "dhw", "cooling", "idle"}
    mapping = {str(k).lower(): str(v) for k, v in state_map.items() if v in allowed}
    streams = [sorted((s for s in stream if s.timestamp.tzinfo is not None and s.timestamp < end),
                      key=lambda sample: sample.timestamp) for stream in (primary, actual_dhw, fallback)]
    for stream in streams:
        if any(a.timestamp == b.timestamp and a.state != b.state for a, b in zip(stream, stream[1:])):
            raise ValueError("conflicting states at one timestamp")
    boundaries = {start}
    for stream in streams:
        for sample in stream:
            if start < sample.timestamp < end:
                boundaries.add(sample.timestamp)
            expiry = sample.timestamp + maximum_age
            if start < expiry < end:
                boundaries.add(expiry)

    result = []
    indexes = [0, 0, 0]
    last: list[StateSample | None] = [None, None, None]
    for moment in sorted(boundaries):
        for index, stream in enumerate(streams):
            while indexes[index] < len(stream) and stream[indexes[index]].timestamp <= moment:
                last[index] = stream[indexes[index]]
                indexes[index] += 1
        active, dhw, selected = (sample if sample and moment - sample.timestamp < maximum_age else None
                                 for sample in last)
        category = mapping.get(active.state.lower()) if active else None
        source = "primary" if category else None
        if category is None and dhw and dhw.state.lower() == "heating_water":
            category, source = "dhw", "actual_dhw"
        if category is None and selected and selected.state.lower() == "heat":
            category, source = "heating_assumed", "selected_mode"
        category, source = category or "unknown", source or "unavailable"
        if not result or result[-1].state != category or result[-1].attributes.get("source") != source:
            result.append(StateSample(moment, category, {"source": source}))
    return result


def historical_selected_modes(start: datetime, end: datetime, samples: list[StateSample],
                              *, maximum_age: timedelta = timedelta(hours=1)) -> list[StateSample]:
    """Keep selected AC modes distinct from confirmed compressor operation."""
    mapping = {"heat": "heating_selected", "cool": "cooling_selected", "off": "standby"}
    if start.tzinfo is None or end.tzinfo is None or end <= start or maximum_age <= timedelta(0):
        raise ValueError("a bounded aware interval and positive freshness are required")
    ordered = sorted((s for s in samples if s.timestamp.tzinfo is not None and s.timestamp < end),
                     key=lambda s: s.timestamp)
    if any(a.timestamp == b.timestamp and a.state != b.state for a, b in zip(ordered, ordered[1:])):
        raise ValueError("conflicting states at one timestamp")
    boundaries = {start}
    for sample in ordered:
        for moment in (sample.timestamp, sample.timestamp + maximum_age):
            if start < moment < end:
                boundaries.add(moment)
    cursor, latest, result = 0, None, []
    for moment in sorted(boundaries):
        while cursor < len(ordered) and ordered[cursor].timestamp <= moment:
            latest = ordered[cursor]
            cursor += 1
        category = "unknown"
        if latest and moment-latest.timestamp < maximum_age:
            selected = latest.state.lower()
            action = str(latest.attributes.get("hvac_action", "")).lower()
            if selected == "off" and action in {"heating", "cooling"}:
                category = "unknown"
            elif selected not in {"unknown", "unavailable"} and action in {"heating", "cooling"}:
                category = action
            else:
                category = mapping.get(selected, "unknown")
        if not result or result[-1].state != category:
            result.append(StateSample(moment, category, {"source": "selected_mode"}))
    return result

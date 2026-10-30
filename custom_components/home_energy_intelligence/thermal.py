"""Measured Ecodan thermal counter intervals and COP calculations.

This module deliberately operates on cumulative observations.  It never turns
sparse readings into invented daily heat and never averages individual COPs.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, time
import math
from typing import Any, Iterable
from zoneinfo import ZoneInfo

_ENERGY_FACTORS = {"Wh": 0.001, "kWh": 1.0, "MWh": 1000.0}
_COUNTER_FIELDS = (
    "heating_thermal_kwh", "dhw_thermal_kwh",
    "vendor_heating_electricity_kwh", "vendor_dhw_electricity_kwh",
)


def _number(value: Any, *, name: str, required: bool = False) -> float | None:
    if value in (None, ""):
        if required:
            raise ValueError(f"{name} is required")
        return None
    try:
        result = float(value)
    except (TypeError, ValueError) as err:
        raise ValueError(f"{name} must be numeric") from err
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{name} must be a finite cumulative value at least zero")
    return result


def resolve_local_timestamp(
    local_date: str, local_time: str, timezone: str, fold: int | None,
) -> datetime:
    """Resolve an operator-entered wall time and reject DST gaps/ambiguity."""
    day = date.fromisoformat(local_date)
    wall = time.fromisoformat(local_time)
    zone = ZoneInfo(timezone)
    candidates = []
    for candidate_fold in (0, 1):
        candidate = datetime.combine(day, wall, zone).replace(fold=candidate_fold)
        if candidate.astimezone(UTC).astimezone(zone).replace(tzinfo=None) == datetime.combine(day, wall):
            candidates.append(candidate)
    distinct = {item.utcoffset() for item in candidates}
    if not candidates:
        raise ValueError("effective local time does not exist because of a DST transition")
    if len(distinct) > 1 and fold not in (0, 1):
        raise ValueError("effective local time is ambiguous; select the DST occurrence")
    chosen_fold = int(fold or 0)
    chosen = next((item for item in candidates if item.fold == chosen_fold), candidates[0])
    return chosen.astimezone(UTC)


def normalize_manual_reading(data: dict[str, Any], timezone: str) -> dict[str, Any]:
    """Validate a manual cumulative observation and normalize all energy to kWh."""
    unit = str(data.get("unit") or "kWh")
    factor = _ENERGY_FACTORS.get(unit)
    if factor is None:
        raise ValueError("unit must be Wh, kWh, or MWh")
    equipment_id = str(data.get("ecodan_equipment_id") or "").strip()
    meter_identity = str(data.get("meter_identity") or "").strip()
    source_label = str(data.get("source_label") or "").strip()
    if not equipment_id or not meter_identity or not source_label:
        raise ValueError("equipment, meter identity, and source label are required")
    local_date = str(data.get("local_date") or "")
    local_time = str(data.get("local_time") or "")
    fold = data.get("fold")
    effective = resolve_local_timestamp(local_date, local_time, timezone, fold)
    normalized: dict[str, Any] = {
        "ecodan_equipment_id": equipment_id,
        "meter_identity": meter_identity,
        "effective_at_utc": effective.isoformat(),
        "local_date": local_date,
        "local_time": local_time,
        "timezone": timezone,
        "fold": int(fold or 0),
        "source_label": source_label,
        "note": str(data.get("note") or "").strip() or None,
        "unit": "kWh",
    }
    for field in _COUNTER_FIELDS:
        value = _number(data.get(field), name=field)
        normalized[field] = value * factor if value is not None else None
    if normalized["heating_thermal_kwh"] is None and normalized["dhw_thermal_kwh"] is None:
        raise ValueError("at least one cumulative thermal reading is required")
    return normalized


def cumulative_intervals(readings: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Difference consecutive readings without clamping resets or filling gaps."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for reading in readings:
        groups[(str(reading["ecodan_equipment_id"]), str(reading["meter_identity"]))].append(reading)
    result: list[dict[str, Any]] = []
    for (equipment_id, meter_identity), items in sorted(groups.items()):
        items.sort(key=lambda item: (item["effective_at_utc"], int(item.get("id", 0))))
        for previous, current in zip(items, items[1:]):
            start = datetime.fromisoformat(previous["effective_at_utc"]).astimezone(UTC)
            end = datetime.fromisoformat(current["effective_at_utc"]).astimezone(UTC)
            if end <= start:
                continue
            interval: dict[str, Any] = {
                "ecodan_equipment_id": equipment_id,
                "meter_identity": meter_identity,
                "start_utc": start.isoformat(), "end_utc": end.isoformat(),
                "elapsed_seconds": (end - start).total_seconds(),
                "start_reading_id": previous.get("id"),
                "end_reading_id": current.get("id"),
                "source_label": current.get("source_label"),
                "quality": "measured", "issues": [],
            }
            for field in _COUNTER_FIELDS:
                old = previous.get(field)
                new = current.get(field)
                output = field.removesuffix("_kwh") + "_delta_kwh"
                if old is None or new is None:
                    interval[output] = None
                    interval["issues"].append(f"{field}:missing_endpoint")
                    continue
                delta = float(new) - float(old)
                if delta < 0:
                    interval[output] = None
                    interval["issues"].append(f"{field}:counter_reset_or_correction")
                else:
                    interval[output] = delta
            if interval["issues"]:
                interval["quality"] = "partial"
            result.append(interval)
    return result


def ratio_of_sums(thermal_kwh: Iterable[float], electricity_kwh: Iterable[float]) -> float | None:
    """Compute an aggregate COP as ratio of sums; never average COP values."""
    thermal = sum(float(value) for value in thermal_kwh)
    electricity = sum(float(value) for value in electricity_kwh)
    return thermal / electricity if electricity > 0 else None


def interval_cop(interval: dict[str, Any], category: str) -> float | None:
    """Return COP only for heating or DHW with exactly matched interval evidence."""
    if category not in {"heating", "dhw"}:
        raise ValueError("COP is supported only for Ecodan heating and DHW")
    thermal = interval.get(f"{category}_thermal_delta_kwh")
    electricity = interval.get(f"vendor_{category}_electricity_delta_kwh")
    if thermal is None or electricity is None or float(electricity) <= 0:
        return None
    return float(thermal) / float(electricity)


def match_classified_electricity(
    interval: dict[str, Any], hourly_records: Iterable[dict[str, Any]],
    equipment: list[dict[str, Any]],
) -> dict[str, Any]:
    """Match classified Ecodan electricity to the exact thermal interval.

    Records must tile the interval without a boundary gap and every selected
    Ecodan allocation must be complete.  Unknown/assumed heating is retained as
    evidence but is not promoted into measured COP electricity.
    """
    from .analytics import canonical_energy  # Avoid a module import cycle.

    start = datetime.fromisoformat(interval["start_utc"]).astimezone(UTC)
    end = datetime.fromisoformat(interval["end_utc"]).astimezone(UTC)
    records = sorted(
        (
            record for record in hourly_records
            if record.get("period_start_utc") and record.get("period_end_utc")
            and datetime.fromisoformat(record["period_start_utc"]).astimezone(UTC) >= start
            and datetime.fromisoformat(record["period_end_utc"]).astimezone(UTC) <= end
        ),
        key=lambda record: record["period_start_utc"],
    )
    cursor = start
    heating = dhw = 0.0
    issues: list[str] = []
    equipment_id = interval["ecodan_equipment_id"]
    for record in records:
        row_start = datetime.fromisoformat(record["period_start_utc"]).astimezone(UTC)
        row_end = datetime.fromisoformat(record["period_end_utc"]).astimezone(UTC)
        if row_start != cursor:
            issues.append("hourly_gap")
            break
        cursor = row_end
        device = next(
            (item for item in canonical_energy(record, equipment).get("equipment", [])
             if item.get("equipment_id") == equipment_id),
            None,
        )
        if not device or not device.get("classification_complete"):
            issues.append("classification_incomplete")
            continue
        assumed = float(device.get("known_assumed_heating_kwh") or 0)
        if assumed > 0:
            issues.append("assumed_heating_excluded")
        heating += float(device.get("known_confirmed_heating_kwh") or 0)
        dhw += float(device.get("known_dhw_kwh") or 0)
    if cursor != end:
        issues.append("interval_boundaries_not_tiled")
    exact = not issues
    return {
        "exact_match": exact,
        "heating_electricity_kwh": heating if exact else None,
        "dhw_electricity_kwh": dhw if exact else None,
        "source": "classified_primary_meter_hourly",
        "issues": sorted(set(issues)),
    }


def thermal_performance_points(
    intervals: Iterable[dict[str, Any]], hourly_records: Iterable[dict[str, Any]],
    equipment: list[dict[str, Any]], outdoor_by_interval: dict[tuple[str, str], float] | None = None,
) -> list[dict[str, Any]]:
    """Build Ecodan-only heat/DHW performance points with explicit evidence."""
    records = list(hourly_records)
    points: list[dict[str, Any]] = []
    for interval in intervals:
        match = match_classified_electricity(interval, records, equipment)
        start, end = interval["start_utc"], interval["end_utc"]
        point = {**interval, **match}
        point["outdoor_mean_c"] = (outdoor_by_interval or {}).get((start, end))
        for category in ("heating", "dhw"):
            thermal = interval.get(f"{category}_thermal_delta_kwh")
            electricity = match.get(f"{category}_electricity_kwh")
            point[f"{category}_cop"] = (
                float(thermal) / float(electricity)
                if thermal is not None and electricity is not None and float(electricity) > 0
                else None
            )
            point[f"{category}_quality"] = (
                "measured_exact_interval"
                if point[f"{category}_cop"] is not None else "unavailable"
            )
        points.append(point)
    return points

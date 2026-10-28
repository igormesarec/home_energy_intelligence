"""Canonical, equipment-neutral analytics shared by every presentation surface."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _category(allocation: dict[str, Any], names: Iterable[str]) -> float:
    categories = allocation.get("categories") or {}
    return sum(float(categories.get(name, 0) or 0) for name in names)


def canonical_energy(
    record: dict[str, Any] | None,
    equipment: Iterable[dict[str, Any]],
) -> dict[str, Any] | None:
    """Return the sole public interpretation of an energy record.

    Raw allocations remain untouched.  Assumed/selected/estimated energy is
    reported explicitly and is never promoted to a confirmed category.
    """
    if record is None:
        return None
    allocations = record.get("allocations") or {}
    enabled = [item for item in equipment if item.get("enabled", True)]
    breakdown: list[dict[str, Any]] = []
    totals = {
        "confirmed_heat_pump_heating_kwh": 0.0,
        "assumed_heat_pump_heating_kwh": 0.0,
        "confirmed_ac_heating_kwh": 0.0,
        "selected_ac_heating_kwh": 0.0,
        "estimated_ac_heating_kwh": 0.0,
        "dhw_kwh": 0.0,
        "confirmed_cooling_kwh": 0.0,
        "selected_cooling_kwh": 0.0,
        "estimated_cooling_kwh": 0.0,
        "idle_kwh": 0.0,
        "other_hvac_kwh": 0.0,
        "unknown_kwh": 0.0,
        "hvac_meter_kwh": 0.0,
    }
    period_complete = (
        record.get("period_coverage") or record.get("interval_coverage") or {}
    ).get("complete", True)
    complete = bool(period_complete)
    heat_pump_complete = bool(period_complete)
    ac_complete = bool(period_complete)
    residual_evidence = 0.0
    for item in enabled:
        equipment_id = str(item["equipment_id"])
        allocation = allocations.get(equipment_id)
        if not isinstance(allocation, dict):
            complete = False
            if item.get("kind") == "heat_pump":
                heat_pump_complete = False
            else:
                ac_complete = False
            breakdown.append({**item, "available": False, "quality": "missing"})
            continue
        meter = _number(allocation.get("meter_kwh"))
        if meter is None:
            complete = False
            if item.get("kind") == "heat_pump":
                heat_pump_complete = False
            else:
                ac_complete = False
        else:
            totals["hvac_meter_kwh"] += meter
        kind = item.get("kind")
        if kind == "heat_pump":
            confirmed = _category(allocation, ("confirmed_heating", "heating"))
            assumed = _number(allocation.get("reported_assumed_heating_kwh"))
            if assumed is None:
                assumed = _category(allocation, ("heating_assumed",))
                residual = _number(allocation.get("residual_kwh"))
                if residual is not None:
                    assumed += max(0.0, residual)
            dhw = _category(allocation, ("dhw",))
            cooling = _category(allocation, ("cooling",))
            idle = _category(allocation, ("idle",))
            unknown = _category(allocation, ("unknown",))
            selected = estimated = 0.0
            selected_cooling = estimated_cooling = 0.0
            totals["confirmed_heat_pump_heating_kwh"] += confirmed
            totals["assumed_heat_pump_heating_kwh"] += assumed
            totals["dhw_kwh"] += dhw
            totals["confirmed_cooling_kwh"] += cooling
            totals["idle_kwh"] += idle
            totals["unknown_kwh"] += unknown
        else:
            confirmed = _category(allocation, ("heating",))
            selected = _category(allocation, ("heating_selected",))
            estimated = _category(allocation, ("heating_estimated",))
            dhw = 0.0
            cooling = _category(allocation, ("cooling",))
            selected_cooling = _category(allocation, ("cooling_selected",))
            estimated_cooling = _category(allocation, ("cooling_estimated",))
            idle = _category(allocation, ("standby",))
            other = _category(allocation, ("dry_fan",))
            unknown = _category(allocation, ("unknown",))
            totals["confirmed_ac_heating_kwh"] += confirmed
            totals["selected_ac_heating_kwh"] += selected
            totals["estimated_ac_heating_kwh"] += estimated
            totals["confirmed_cooling_kwh"] += cooling
            totals["selected_cooling_kwh"] += selected_cooling
            totals["estimated_cooling_kwh"] += estimated_cooling
            totals["idle_kwh"] += idle
            totals["other_hvac_kwh"] += other
            totals["unknown_kwh"] += unknown
        residual = _number(allocation.get("uncertain_residual_kwh"))
        if residual is None:
            residual = _number(allocation.get("residual_kwh"))
        residual_evidence += residual or 0.0
        device_complete = meter is not None
        breakdown.append({
            **item, "available": device_complete, "meter_kwh": meter,
            "known_confirmed_heating_kwh": confirmed,
            "known_assumed_heating_kwh": assumed if kind == "heat_pump" else 0.0,
            "known_selected_heating_kwh": selected,
            "known_estimated_heating_kwh": estimated,
            "known_dhw_kwh": dhw, "known_confirmed_cooling_kwh": cooling,
            "known_selected_cooling_kwh": selected_cooling,
            "known_estimated_cooling_kwh": estimated_cooling,
            "known_cooling_kwh": cooling + selected_cooling + estimated_cooling,
            "known_idle_kwh": idle,
            "confirmed_heating_kwh": confirmed if device_complete else None,
            "assumed_heating_kwh": (
                assumed if kind == "heat_pump" and device_complete else
                (0.0 if kind != "heat_pump" and device_complete else None)
            ),
            "selected_heating_kwh": selected if device_complete else None,
            "estimated_heating_kwh": estimated if device_complete else None,
            "dhw_kwh": dhw if device_complete else None,
            "confirmed_cooling_kwh": cooling if device_complete else None,
            "selected_cooling_kwh": selected_cooling if device_complete else None,
            "estimated_cooling_kwh": estimated_cooling if device_complete else None,
            "cooling_kwh": (
                cooling + selected_cooling + estimated_cooling if device_complete else None
            ),
            "idle_kwh": idle if device_complete else None,
            "unknown_kwh": unknown, "residual_kwh": residual,
            "coverage_percent": allocation.get("coverage_percent"),
            "quality": allocation.get("quality", "missing"),
        })

    known_space_heating = (
        totals["confirmed_heat_pump_heating_kwh"]
        + totals["assumed_heat_pump_heating_kwh"]
        + totals["confirmed_ac_heating_kwh"]
        + totals["selected_ac_heating_kwh"]
        + totals["estimated_ac_heating_kwh"]
    )
    known_dhw = totals["dhw_kwh"]
    known_cooling = (
        totals["confirmed_cooling_kwh"] + totals["selected_cooling_kwh"]
        + totals["estimated_cooling_kwh"]
    )
    known_idle = totals["idle_kwh"]
    known_hvac_meter = totals["hvac_meter_kwh"]
    heating_complete = heat_pump_complete and ac_complete
    cooling_complete = heat_pump_complete and ac_complete
    category_requirements = {
        "confirmed_heat_pump_heating_kwh": heat_pump_complete,
        "assumed_heat_pump_heating_kwh": heat_pump_complete,
        "confirmed_ac_heating_kwh": ac_complete,
        "selected_ac_heating_kwh": ac_complete,
        "estimated_ac_heating_kwh": ac_complete,
        "confirmed_cooling_kwh": cooling_complete,
        "selected_cooling_kwh": ac_complete,
        "estimated_cooling_kwh": ac_complete,
    }
    for key, is_complete in category_requirements.items():
        totals[f"known_{key}"] = totals[key]
        if not is_complete:
            totals[key] = None
    totals.update({
        "known_space_heating_kwh": known_space_heating,
        "known_dhw_kwh": known_dhw,
        "known_cooling_kwh": known_cooling,
        "known_idle_kwh": known_idle,
        "known_hvac_meter_kwh": known_hvac_meter,
        "space_heating_kwh": known_space_heating if heating_complete else None,
        "dhw_kwh": known_dhw if heat_pump_complete else None,
        "cooling_kwh": known_cooling if cooling_complete else None,
        "idle_kwh": known_idle if complete else None,
        "hvac_meter_kwh": known_hvac_meter if complete else None,
        "heating_and_dhw_kwh": (
            known_space_heating + known_dhw
            if heating_complete and heat_pump_complete else None
        ),
    })
    household = _number(record.get("household_kwh"))
    pv = _number(record.get("pv_kwh"))
    imported = _number(record.get("import_kwh"))
    exported = _number(record.get("export_kwh"))
    other_household = _number(record.get("non_hvac_kwh"))
    classified_hvac = (
        known_space_heating + known_dhw + known_cooling
        + known_idle + totals["other_hvac_kwh"] + totals["unknown_kwh"]
    )
    discrepancy = None if not complete else known_hvac_meter - classified_hvac
    consumed_pv = (
        None if pv is None or exported is None or pv < 0 or exported < 0 or exported > pv
        else pv - exported
    )
    self_consumption = None if pv in (None, 0) or consumed_pv is None else 100 * consumed_pv / pv
    self_sufficiency = (
        None if household in (None, 0) or consumed_pv is None
        else 100 * consumed_pv / household
    )
    return {
        **totals,
        "household_kwh": household,
        "other_household_kwh": other_household,
        "unallocated_discrepancy_kwh": discrepancy,
        "raw_residual_evidence_kwh": residual_evidence,
        "pv_kwh": pv, "import_kwh": imported, "export_kwh": exported,
        "pv_consumed_on_site_kwh": consumed_pv,
        "pv_self_consumption_percent": self_consumption,
        "self_sufficiency_percent": self_sufficiency,
        "quality": record.get("quality", "missing") if complete else "partial",
        "complete": complete,
        "completeness": {
            "space_heating": heating_complete, "dhw": heat_pump_complete,
            "cooling": cooling_complete, "idle": complete, "hvac_meter": complete,
        },
        "equipment": breakdown,
    }


def attach_canonical(record: dict[str, Any], equipment: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Copy a stored record and attach its canonical presentation contract."""
    return {**record, "energy": canonical_energy(record, equipment)}


def interval_coverage(
    records: list[dict[str, Any]], start: datetime, end: datetime,
) -> dict[str, Any]:
    """Validate exact elapsed-hour coverage, including internal gaps and DST."""
    expected: list[tuple[str, str]] = []
    cursor = start
    while cursor < end:
        interval_end = min(cursor + timedelta(hours=1), end)
        expected.append((cursor.isoformat(), interval_end.isoformat()))
        cursor = interval_end
    present = {
        (str(item.get("period_start_utc")), str(item.get("period_end_utc")))
        for item in records
    }
    missing = [item for item in expected if item not in present]
    poor = [
        (str(item.get("period_start_utc")), str(item.get("period_end_utc")))
        for item in records
        if item.get("quality") in {"missing", "poor", "missing_counter", "unreconciled"}
    ]
    expected_seconds = max(0.0, (end - start).total_seconds())
    present_seconds = sum(
        max(0.0, (datetime.fromisoformat(item[1]) - datetime.fromisoformat(item[0])).total_seconds())
        for item in expected if item in present
    )
    return {
        "expected_intervals": len(expected), "present_intervals": len(expected) - len(missing),
        "missing_intervals": [{"start": item[0], "end": item[1]} for item in missing],
        "poor_quality_intervals": [{"start": item[0], "end": item[1]} for item in poor],
        "coverage_seconds": present_seconds, "expected_seconds": expected_seconds,
        "coverage_percent": 100 * present_seconds / expected_seconds if expected_seconds else 100.0,
        "complete": not missing and not poor,
    }


def combine_interval_records(
    records: list[dict[str, Any]], *, expected_start: datetime | None = None,
    expected_end: datetime | None = None,
) -> dict[str, Any] | None:
    """Combine contiguous compact intervals without inventing missing values."""
    if not records:
        return None
    records = sorted(records, key=lambda item: item["period_start_utc"])

    def optional_sum(key: str) -> float | None:
        values = [_number(item.get(key)) for item in records]
        return None if any(value is None for value in values) else sum(values)  # type: ignore[arg-type]

    def record_seconds(item: dict[str, Any]) -> float:
        seconds = _number(item.get("day_seconds"))
        if seconds is not None:
            return max(0.0, seconds)
        try:
            return max(0.0, (
                datetime.fromisoformat(str(item["period_end_utc"]))
                - datetime.fromisoformat(str(item["period_start_utc"]))
            ).total_seconds())
        except (KeyError, TypeError, ValueError):
            return 0.0

    equipment_ids = set().union(*(set(item.get("allocations", {})) for item in records))
    allocations: dict[str, dict[str, Any]] = {}
    for equipment_id in equipment_ids:
        parts = [item.get("allocations", {}).get(equipment_id) for item in records]
        available = [part for part in parts if isinstance(part, dict)]
        categories = set().union(*(set(part.get("categories", {})) for part in available))
        seconds = sum(
            float(part.get("period_seconds", 0) or 0)
            if isinstance(part, dict) else record_seconds(record)
            for record, part in zip(records, parts, strict=True)
        )
        covered = sum(float(part.get("covered_seconds", 0) or 0) for part in available)
        allocations[equipment_id] = {
            "meter_kwh": (None if len(available) != len(records) or any(
                _number(part.get("meter_kwh")) is None for part in available
            ) else sum(float(part["meter_kwh"]) for part in available)),
            "categories": {name: sum(float(part.get("categories", {}).get(name, 0) or 0)
                                      for part in available) for name in categories},
            "residual_kwh": (None if len(available) != len(records) or any(
                _number(part.get("residual_kwh")) is None for part in available
            ) else sum(float(part["residual_kwh"]) for part in available)),
            "uncertain_residual_kwh": sum(float(part.get("uncertain_residual_kwh", 0) or 0)
                                            for part in available),
            "reported_assumed_heating_kwh": sum(
                float(part.get("reported_assumed_heating_kwh", 0) or 0) for part in available
            ),
            "coverage_percent": 100 * covered / seconds if seconds else 0.0,
            "covered_seconds": covered, "period_seconds": seconds,
            "quality": "good" if len(available) == len(records) and all(
                part.get("quality") == "good" for part in available
            ) else "partial",
        }
    source_ids = set().union(*(set(item.get("temperatures", {})) for item in records))
    temperatures: dict[str, dict[str, Any]] = {}
    for source_id in source_ids:
        parts = [item.get("temperatures", {}).get(source_id) for item in records]
        available = [part for part in parts if isinstance(part, dict)]
        coverage = sum(float(part.get("coverage_seconds", 0) or 0) for part in available)
        period = sum(
            float(part.get("period_seconds", 0) or 0)
            if isinstance(part, dict) else record_seconds(record)
            for record, part in zip(records, parts, strict=True)
        )
        means = [(float(part["mean"]), float(part.get("coverage_seconds", 0) or 0))
                 for part in available if _number(part.get("mean")) is not None]
        temperatures[source_id] = {
            "mean": sum(value * weight for value, weight in means) / coverage if coverage else None,
            "minimum": min((float(part["minimum"]) for part in available
                            if _number(part.get("minimum")) is not None), default=None),
            "maximum": max((float(part["maximum"]) for part in available
                            if _number(part.get("maximum")) is not None), default=None),
            "coverage_seconds": coverage, "period_seconds": period,
            "coverage_percent": 100 * coverage / period if period else 0.0,
            "quality": "good" if len(available) == len(records) and all(
                part.get("quality") == "good" for part in available
            ) else ("missing" if not coverage else "partial"),
            "rejected_count": sum(int(part.get("rejected_count", 0)) for part in available),
            "latest_value": next((part.get("latest_value") for part in reversed(available)
                                  if part.get("latest_value") is not None), None),
            "latest_timestamp": next((part.get("latest_timestamp") for part in reversed(available)
                                      if part.get("latest_timestamp")), None),
        }
    coverage = (
        interval_coverage(records, expected_start, expected_end)
        if expected_start is not None and expected_end is not None else None
    )
    household = optional_sum("household_kwh")
    pv = optional_sum("pv_kwh")
    imported = optional_sum("import_kwh")
    exported = optional_sum("export_kwh")
    non_hvac = optional_sum("non_hvac_kwh")
    interval_complete = coverage is None or coverage["complete"]
    return {
        "local_date": records[0]["local_date"], "timezone": records[0]["timezone"],
        "period_start_utc": (expected_start.isoformat() if expected_start else records[0]["period_start_utc"]),
        "period_end_utc": (expected_end.isoformat() if expected_end else records[-1]["period_end_utc"]),
        "day_seconds": int((expected_end - expected_start).total_seconds())
        if expected_start and expected_end else sum(int(item.get("day_seconds", 0)) for item in records),
        "known_household_kwh": household, "known_pv_kwh": pv,
        "known_import_kwh": imported, "known_export_kwh": exported,
        "known_non_hvac_kwh": non_hvac,
        "household_kwh": household if interval_complete else None,
        "pv_kwh": pv if interval_complete else None,
        "import_kwh": imported if interval_complete else None,
        "export_kwh": exported if interval_complete else None,
        "non_hvac_kwh": non_hvac if interval_complete else None,
        "quality": "good" if interval_complete and all(
            item.get("quality") == "good" for item in records
        ) else "partial",
        "interval_coverage": coverage,
        "updated_at": records[-1].get("updated_at") or records[-1].get("collected_at"),
        "temperatures": temperatures, "allocations": allocations,
        "provenance": {"source": "persisted_hourly_records", "interval_count": len(records)},
    }


def daily_coverage(
    records: list[dict[str, Any]], start_date: str, end_date: str,
) -> dict[str, Any]:
    """Return explicit complete/missing daily coverage for a local-date range."""
    start = datetime.fromisoformat(start_date).date()
    end = datetime.fromisoformat(end_date).date()
    expected: list[str] = []
    cursor = start
    while cursor <= end:
        expected.append(cursor.isoformat())
        cursor += timedelta(days=1)
    present = {str(item.get("local_date")) for item in records}
    missing = [day for day in expected if day not in present]
    poor = [str(item["local_date"]) for item in records
            if item.get("quality") in {"missing", "poor", "unreconciled"}]
    return {
        "expected_days": len(expected), "present_days": len(expected) - len(missing),
        "missing_dates": missing, "poor_quality_dates": poor,
        "coverage_percent": 100 * (len(expected) - len(missing)) / len(expected)
        if expected else 100.0,
        "complete": not missing and not poor,
    }


def combine_daily_records(
    records: list[dict[str, Any]], start_date: str, end_date: str,
) -> dict[str, Any] | None:
    """Combine the full requested daily range with explicit missing dates."""
    if not records:
        return None
    combined = combine_interval_records(records)
    assert combined is not None
    coverage = daily_coverage(records, start_date, end_date)
    combined["period_coverage"] = coverage
    if not coverage["complete"]:
        for key in ("household_kwh", "pv_kwh", "import_kwh", "export_kwh", "non_hvac_kwh"):
            combined[key] = None
    combined["quality"] = "good" if coverage["complete"] and all(
        item.get("quality") == "good" for item in records
    ) else "partial"
    return combined

"""Canonical, equipment-neutral analytics shared by every presentation surface."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

from .timeutils import local_day_bounds


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
    coverage = record.get("period_coverage") or record.get("interval_coverage") or {}
    configured_ids = {str(item["equipment_id"]) for item in enabled}
    interval_complete = bool(coverage.get("complete", True))
    meter_complete = bool((coverage.get("meter_coverage") or {}).get(
        "complete", interval_complete and _number(record.get("household_kwh")) is not None,
    ))
    allocation_complete = bool((coverage.get("allocation_coverage") or {}).get(
        "complete", interval_complete and bool(configured_ids)
        and configured_ids.issubset(set(allocations)),
    ))
    complete = meter_complete and allocation_complete
    temporal_complete = bool(
        (coverage.get("date_coverage") or {}).get("complete", interval_complete)
    )

    def kind_complete(kind: str) -> bool:
        items = [item for item in enabled if item.get("kind") == kind]
        return temporal_complete and all(
            isinstance(allocations.get(str(item["equipment_id"])), dict)
            and _number(allocations[str(item["equipment_id"])].get("meter_kwh")) is not None
            and (
                _number(allocations[str(item["equipment_id"])].get("coverage_percent"))
                in (None, 100.0)
                or _number(allocations[str(item["equipment_id"])].get("coverage_percent"))
                >= 99.999
            )
            and allocations[str(item["equipment_id"])].get("quality")
            not in {"missing", "poor", "missing_counter", "unreconciled"}
            for item in items
        )

    heat_pump_complete = kind_complete("heat_pump")
    ac_complete = kind_complete("air_conditioner")
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
            other = 0.0
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
        device_coverage = _number(allocation.get("coverage_percent"))
        classification_complete = (
            device_complete
            and (device_coverage is None or device_coverage >= 99.999)
            and allocation.get("quality")
            not in {"missing", "poor", "missing_counter", "unreconciled"}
        )
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
            "known_other_hvac_kwh": other,
            "classification_complete": classification_complete,
            "confirmed_heating_kwh": confirmed if classification_complete else None,
            "assumed_heating_kwh": (
                assumed if kind == "heat_pump" and classification_complete else
                (0.0 if kind != "heat_pump" and classification_complete else None)
            ),
            "selected_heating_kwh": selected if classification_complete else None,
            "estimated_heating_kwh": estimated if classification_complete else None,
            "dhw_kwh": dhw if classification_complete else None,
            "confirmed_cooling_kwh": cooling if classification_complete else None,
            "selected_cooling_kwh": selected_cooling if classification_complete else None,
            "estimated_cooling_kwh": estimated_cooling if classification_complete else None,
            "cooling_kwh": (
                cooling + selected_cooling + estimated_cooling
                if classification_complete else None
            ),
            "idle_kwh": idle if classification_complete else None,
            "other_hvac_kwh": other if classification_complete else None,
            "unknown_kwh": unknown, "residual_kwh": residual,
            "coverage_percent": allocation.get("coverage_percent"),
            "covered_seconds": allocation.get("covered_seconds"),
            "expected_seconds": allocation.get("expected_seconds"),
            "classification_source": allocation.get("classification_source"),
            "source": allocation.get("source"),
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
    known_other_hvac = totals["other_hvac_kwh"]
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
        "other_hvac_kwh": ac_complete,
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
        "known_other_hvac_kwh": known_other_hvac,
        "known_hvac_meter_kwh": known_hvac_meter,
        "space_heating_kwh": known_space_heating if heating_complete else None,
        "dhw_kwh": known_dhw if heat_pump_complete else None,
        "cooling_kwh": known_cooling if cooling_complete else None,
        "idle_kwh": known_idle if allocation_complete else None,
        "hvac_meter_kwh": known_hvac_meter if allocation_complete else None,
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
        + known_idle + known_other_hvac + totals["unknown_kwh"]
    )
    discrepancy = None if not allocation_complete else known_hvac_meter - classified_hvac
    consumed_pv = (
        None if pv is None or exported is None or pv < 0 or exported < 0 or exported > pv
        else pv - exported
    )
    self_consumption = None if pv in (None, 0) or consumed_pv is None else 100 * consumed_pv / pv
    self_sufficiency = (
        None if household in (None, 0) or consumed_pv is None
        else 100 * consumed_pv / household
    )
    calculated_household = (
        None if consumed_pv is None or imported is None or imported < 0
        else consumed_pv + imported
    )
    household_balance_discrepancy = (
        None if household is None or calculated_household is None
        else household - calculated_household
    )
    return {
        **totals,
        "household_kwh": household,
        "other_household_kwh": other_household,
        "unallocated_discrepancy_kwh": discrepancy,
        "raw_residual_evidence_kwh": residual_evidence,
        "pv_kwh": pv, "import_kwh": imported, "export_kwh": exported,
        "pv_consumed_on_site_kwh": consumed_pv,
        "calculated_household_kwh": calculated_household,
        "household_balance_discrepancy_kwh": household_balance_discrepancy,
        "pv_self_consumption_percent": self_consumption,
        "self_sufficiency_percent": self_sufficiency,
        "quality": record.get("quality", "missing") if complete else "partial",
        "complete": complete,
        "completeness": {
            "date": bool((coverage.get("date_coverage") or {}).get("complete", interval_complete)),
            "meter": meter_complete, "allocation": allocation_complete,
            "space_heating": heating_complete, "dhw": heat_pump_complete,
            "cooling": cooling_complete, "idle": allocation_complete,
            "hvac_meter": allocation_complete,
            "temperature": coverage.get("temperature_coverage", {}),
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
    if coverage is not None:
        temporal_complete = bool(coverage["complete"])
        meter_complete = temporal_complete and household is not None
        allocation_expected = sum(
            float(item.get("period_seconds", 0) or 0) for item in allocations.values()
        )
        allocation_covered = sum(
            min(float(item.get("covered_seconds", 0) or 0),
                float(item.get("period_seconds", 0) or 0))
            for item in allocations.values()
        )
        allocation_processing_complete = bool(allocations) and all(
            item.get("meter_kwh") is not None for item in allocations.values()
        )
        allocation_complete = temporal_complete and bool(allocations) and all(
            allocation.get("meter_kwh") is not None
            and allocation.get("quality")
            not in {"partial", "poor", "missing", "missing_counter", "unreconciled"}
            and float(allocation.get("coverage_percent", 0) or 0) >= 99.999
            for allocation in allocations.values()
        )
        allocation_uncertain = allocation_complete and any(
            allocation.get("quality") in {"uncertain", "filtered"}
            for allocation in allocations.values()
        )
        coverage.update({
            "date_coverage": {"complete": temporal_complete,
                              "coverage_percent": coverage["coverage_percent"]},
            "meter_coverage": {"complete": meter_complete,
                               "coverage_percent": coverage["coverage_percent"]
                               if household is not None else 0.0},
            "allocation_coverage": {
                "complete": allocation_complete,
                "processing_complete": allocation_processing_complete,
                "classified_seconds": allocation_covered,
                "expected_seconds": allocation_expected,
                "coverage_percent": (
                    100 * allocation_covered / allocation_expected
                    if allocation_expected else None
                ),
                "quality": (
                    "unavailable" if not allocation_expected else
                    ("uncertain" if allocation_uncertain else
                     ("good" if allocation_complete else "partial"))
                ),
            },
            "temperature_coverage": {
                source_id: {
                    "complete": item.get("quality") == "good",
                    "coverage_percent": item.get("coverage_percent", 0.0),
                    "quality": item.get("quality", "missing"),
                }
                for source_id, item in temperatures.items()
            },
        })
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
    *, equipment_ids: Iterable[str] = (), timezone: str | None = None,
) -> dict[str, Any]:
    """Return explicit complete/missing daily coverage for a local-date range."""
    start = datetime.fromisoformat(start_date).date()
    end = datetime.fromisoformat(end_date).date()
    expected: list[str] = []
    cursor = start
    while cursor <= end:
        expected.append(cursor.isoformat())
        cursor += timedelta(days=1)
    by_date = {str(item.get("local_date")): item for item in records}
    present = set(by_date)
    missing = [day for day in expected if day not in present]
    partial_quality = [
        day for day, item in by_date.items() if item.get("quality", "missing") != "good"
    ]
    temporal_partial = [
        day for day, item in by_date.items()
        if item.get("partial") is True or item.get("finalized") is False
    ]

    zone_name = timezone or next(
        (str(item.get("timezone")) for item in records if item.get("timezone")), "UTC"
    )

    def expected_seconds(day: str) -> float:
        item = by_date.get(day)
        if item and (item.get("partial") is True or item.get("finalized") is False):
            return max(0.0, float(item.get("day_seconds", 0) or 0))
        start_utc, end_utc = local_day_bounds(datetime.fromisoformat(day).date(), zone_name)
        return (end_utc - start_utc).total_seconds()

    def field_coverage(field: str) -> dict[str, Any]:
        unavailable = [day for day in expected if _number(by_date.get(day, {}).get(field)) is None]
        total_seconds = sum(expected_seconds(day) for day in expected)
        available_seconds = sum(
            expected_seconds(day) for day in expected if day not in unavailable
        )
        return {
            "available_days": len(expected) - len(unavailable),
            "unavailable_dates": unavailable,
            "available_seconds": available_seconds, "expected_seconds": total_seconds,
            "coverage_percent": 100 * available_seconds / total_seconds
            if total_seconds else None,
            "complete": not unavailable,
        }

    configured_equipment = set(str(item) for item in equipment_ids)
    if not configured_equipment:
        configured_equipment = set().union(*(
            set(item.get("allocations", {})) for item in records
        )) if records else set()
    allocation_missing: list[str] = []
    allocation_partial: list[str] = []
    allocation_uncertain: list[str] = []
    allocation_missing_equipment: list[dict[str, str]] = []
    allocation_expected_seconds = 0.0
    allocation_classified_seconds = 0.0
    for day in expected:
        item = by_date.get(day)
        allocations = item.get("allocations") if item else None
        day_seconds = expected_seconds(day)
        allocation_expected_seconds += day_seconds * len(configured_equipment)
        if not configured_equipment:
            continue
        if not isinstance(allocations, dict) or not allocations:
            allocation_missing.append(day)
            allocation_missing_equipment.extend(
                {"date": day, "equipment_id": equipment_id}
                for equipment_id in sorted(configured_equipment)
            )
            continue
        day_partial = False
        for equipment_id in configured_equipment:
            value = allocations.get(equipment_id)
            if not isinstance(value, dict):
                allocation_missing_equipment.append({
                    "date": day, "equipment_id": equipment_id,
                })
                day_partial = True
                continue
            period = float(value.get("period_seconds", day_seconds) or day_seconds)
            covered_value = value.get("covered_seconds")
            if covered_value is None and value.get("coverage_percent") is not None:
                covered_value = period * float(value["coverage_percent"]) / 100
            covered = min(float(covered_value or 0), period)
            allocation_classified_seconds += covered
            quality = value.get("quality")
            if (
                _number(value.get("meter_kwh")) is None
                or covered + 1e-6 < period
                or quality in {
                    "partial", "poor", "missing", "missing_counter", "unreconciled",
                }
            ):
                day_partial = True
            elif quality in {"uncertain", "filtered"}:
                allocation_uncertain.append(day)
        if day_partial:
            allocation_partial.append(day)
    meter_fields = {
        field: field_coverage(field)
        for field in ("household_kwh", "pv_kwh", "import_kwh", "export_kwh")
    }
    date_layer = {
        "expected_days": len(expected), "available_days": len(expected) - len(missing),
        "missing_dates": missing,
        "coverage_percent": 100 * (len(expected) - len(missing)) / len(expected)
        if expected else 100.0,
        "complete": not missing,
    }
    allocation_layer = {
        "missing_dates": allocation_missing, "partial_dates": allocation_partial,
        "uncertain_dates": sorted(set(allocation_uncertain)),
        "missing_equipment": allocation_missing_equipment,
        "classified_seconds": allocation_classified_seconds,
        "expected_seconds": allocation_expected_seconds,
        "coverage_percent": (
            100 * allocation_classified_seconds / allocation_expected_seconds
            if allocation_expected_seconds else None
        ),
        "processing_complete": bool(configured_equipment) and not allocation_missing_equipment,
        "complete": bool(configured_equipment) and not allocation_missing and not allocation_partial,
        "quality": (
            "unavailable" if not configured_equipment else
            ("uncertain" if not allocation_missing and not allocation_partial
             and allocation_uncertain else
             ("good" if not allocation_missing and not allocation_partial else "partial"))
        ),
    }
    return {
        "expected_days": len(expected), "present_days": len(expected) - len(missing),
        "missing_dates": missing, "poor_quality_dates": partial_quality,
        "partial_dates": temporal_partial,
        "coverage_percent": date_layer["coverage_percent"],
        "date_coverage": date_layer,
        "meter_coverage": {"fields": meter_fields, **meter_fields["household_kwh"]},
        "allocation_coverage": allocation_layer,
        "temperature_coverage": {},
        "complete": not missing and not partial_quality and not temporal_partial,
        "quality": (
            "missing" if len(missing) == len(expected) and expected
            else ("good" if not missing and not partial_quality and not temporal_partial else "partial")
        ),
    }


def combine_daily_records(
    records: list[dict[str, Any]], start_date: str, end_date: str,
    *, temperature_source_ids: Iterable[str] = (), equipment_ids: Iterable[str] = (),
    timezone: str | None = None,
) -> dict[str, Any] | None:
    """Combine the full requested daily range with explicit missing dates."""
    if not records:
        return None
    combined = combine_interval_records(records)
    assert combined is not None
    coverage = daily_coverage(
        records, start_date, end_date,
        equipment_ids=equipment_ids, timezone=timezone,
    )
    combined["period_coverage"] = coverage
    meter_fields = coverage["meter_coverage"]["fields"]
    for key in ("household_kwh", "pv_kwh", "import_kwh", "export_kwh"):
        if not meter_fields[key]["complete"]:
            combined[key] = None
    if not meter_fields["household_kwh"]["complete"]:
        combined["non_hvac_kwh"] = None

    timezone = str(combined.get("timezone") or records[0].get("timezone") or "UTC")
    record_by_date = {str(item.get("local_date")): item for item in records}
    start = datetime.fromisoformat(start_date).date()
    end = datetime.fromisoformat(end_date).date()
    expected_seconds = 0.0
    cursor = start
    while cursor <= end:
        record = record_by_date.get(cursor.isoformat())
        if record and (record.get("partial") is True or record.get("finalized") is False):
            expected_seconds += max(0.0, float(record.get("day_seconds", 0) or 0))
        else:
            day_start, day_end = local_day_bounds(cursor, timezone)
            expected_seconds += (day_end - day_start).total_seconds()
        cursor += timedelta(days=1)

    source_ids = set(temperature_source_ids) | set(combined.get("temperatures", {}))
    for source_id in source_ids:
        item = combined.setdefault("temperatures", {}).setdefault(source_id, {
            "mean": None, "minimum": None, "maximum": None,
            "coverage_seconds": 0.0, "rejected_count": 0,
            "latest_value": None, "latest_timestamp": None,
        })
        valid_seconds = float(item.get("coverage_seconds", 0) or 0)
        item["period_seconds"] = expected_seconds
        item["coverage_percent"] = (
            100 * valid_seconds / expected_seconds if expected_seconds else 0.0
        )
        item["quality"] = (
            "missing" if not valid_seconds
            else ("good" if valid_seconds >= expected_seconds and item.get("quality") == "good"
                  else "partial")
        )
        coverage["temperature_coverage"][source_id] = {
            "valid_seconds": valid_seconds, "expected_seconds": expected_seconds,
            "coverage_percent": item["coverage_percent"],
            "complete": bool(expected_seconds and valid_seconds >= expected_seconds
                             and item["quality"] == "good"),
            "quality": item["quality"],
        }
    if coverage["temperature_coverage"] and not all(
        item["complete"] for item in coverage["temperature_coverage"].values()
    ):
        coverage["complete"] = False
        coverage["quality"] = "partial"
    combined["quality"] = coverage["quality"]
    return combined


def replace_with_partial_day(
    records: Iterable[dict[str, Any]], partial_record: dict[str, Any] | None,
    *, local_date: str | None = None,
) -> list[dict[str, Any]]:
    """Replace, never persist, a current-date finalized row with its hourly partial."""
    result = list(records)
    replace_date = str(partial_record["local_date"]) if partial_record is not None else local_date
    if replace_date is not None:
        result = [item for item in result if str(item.get("local_date")) != replace_date]
    if partial_record is None:
        return result
    result.append({**partial_record, "partial": True, "finalized": False})
    return sorted(result, key=lambda item: str(item["local_date"]))

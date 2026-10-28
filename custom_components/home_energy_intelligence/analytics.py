"""Canonical, equipment-neutral analytics shared by every presentation surface."""

from __future__ import annotations

from collections.abc import Iterable
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
    complete = True
    residual_evidence = 0.0
    for item in enabled:
        equipment_id = str(item["equipment_id"])
        allocation = allocations.get(equipment_id)
        if not isinstance(allocation, dict):
            complete = False
            breakdown.append({**item, "available": False, "quality": "missing"})
            continue
        meter = _number(allocation.get("meter_kwh"))
        if meter is None:
            complete = False
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
        breakdown.append({
            **item, "available": meter is not None, "meter_kwh": meter,
            "confirmed_heating_kwh": confirmed,
            "assumed_heating_kwh": assumed if kind == "heat_pump" else 0.0,
            "selected_heating_kwh": selected, "estimated_heating_kwh": estimated,
            "dhw_kwh": dhw, "cooling_kwh": cooling, "idle_kwh": idle,
            "unknown_kwh": unknown, "residual_kwh": residual,
            "coverage_percent": allocation.get("coverage_percent"),
            "quality": allocation.get("quality", "missing"),
        })

    totals["space_heating_kwh"] = (
        totals["confirmed_heat_pump_heating_kwh"]
        + totals["assumed_heat_pump_heating_kwh"]
        + totals["confirmed_ac_heating_kwh"]
        + totals["selected_ac_heating_kwh"]
        + totals["estimated_ac_heating_kwh"]
    )
    totals["heating_and_dhw_kwh"] = totals["space_heating_kwh"] + totals["dhw_kwh"]
    totals["cooling_kwh"] = (
        totals["confirmed_cooling_kwh"] + totals["selected_cooling_kwh"]
        + totals["estimated_cooling_kwh"]
    )
    household = _number(record.get("household_kwh"))
    pv = _number(record.get("pv_kwh"))
    imported = _number(record.get("import_kwh"))
    exported = _number(record.get("export_kwh"))
    other_household = _number(record.get("non_hvac_kwh"))
    classified_hvac = (
        totals["space_heating_kwh"] + totals["dhw_kwh"] + totals["cooling_kwh"]
        + totals["idle_kwh"] + totals["other_hvac_kwh"] + totals["unknown_kwh"]
    )
    discrepancy = None if not complete else totals["hvac_meter_kwh"] - classified_hvac
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
        "quality": record.get("quality", "missing"),
        "complete": complete,
        "equipment": breakdown,
    }


def attach_canonical(record: dict[str, Any], equipment: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Copy a stored record and attach its canonical presentation contract."""
    return {**record, "energy": canonical_energy(record, equipment)}


def combine_interval_records(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Combine contiguous compact intervals without inventing missing values."""
    if not records:
        return None
    records = sorted(records, key=lambda item: item["period_start_utc"])

    def optional_sum(key: str) -> float | None:
        values = [_number(item.get(key)) for item in records]
        return None if any(value is None for value in values) else sum(values)  # type: ignore[arg-type]

    equipment_ids = set().union(*(set(item.get("allocations", {})) for item in records))
    allocations: dict[str, dict[str, Any]] = {}
    for equipment_id in equipment_ids:
        parts = [item.get("allocations", {}).get(equipment_id) for item in records]
        available = [part for part in parts if isinstance(part, dict)]
        categories = set().union(*(set(part.get("categories", {})) for part in available))
        seconds = sum(float(part.get("period_seconds", 0) or 0) for part in available)
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
        period = sum(float(part.get("period_seconds", 0) or 0) for part in available)
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
    return {
        "local_date": records[0]["local_date"], "timezone": records[0]["timezone"],
        "period_start_utc": records[0]["period_start_utc"],
        "period_end_utc": records[-1]["period_end_utc"],
        "day_seconds": sum(int(item.get("day_seconds", 0)) for item in records),
        "household_kwh": optional_sum("household_kwh"), "pv_kwh": optional_sum("pv_kwh"),
        "import_kwh": optional_sum("import_kwh"), "export_kwh": optional_sum("export_kwh"),
        "non_hvac_kwh": optional_sum("non_hvac_kwh"),
        "quality": "good" if all(item.get("quality") == "good" for item in records) else "partial",
        "updated_at": records[-1].get("updated_at") or records[-1].get("collected_at"),
        "temperatures": temperatures, "allocations": allocations,
        "provenance": {"source": "persisted_hourly_records", "interval_count": len(records)},
    }

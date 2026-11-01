"""Canonical, equipment-neutral analytics shared by every presentation surface."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from math import isfinite
from typing import Any

from .timeutils import local_day_bounds


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _valid_meter_value(record: dict[str, Any], field: str) -> bool:
    """Validate a field independently of unrelated HVAC/temperature quality."""
    value = _number(record.get(field))
    if value is None or not isfinite(value) or value < 0:
        return False
    aliases = {"pv_kwh": "pv", "import_kwh": "import", "export_kwh": "export"}
    provenance = record.get("provenance") or {}
    sources = provenance.get("counter_collection") or {}
    source = sources.get(f"site.{aliases.get(field)}") if field in aliases else None
    if source in {"recorder_states_unreliable", "unavailable", "invalid", "unreconciled"}:
        return False
    quality = (record.get("meter_quality") or {}).get(field)
    return quality not in {"missing", "poor", "invalid", "unreconciled"}


def _classification_coverage(allocation: dict[str, Any]) -> float | None:
    """Return evidenced classification coverage, never a legacy default.

    New rows carry covered/period seconds. Some intermediate rows carry only
    a percentage. Older rows without either remain readable but unknown.
    """
    covered = _number(allocation.get("covered_seconds"))
    period = _number(
        allocation.get("period_seconds", allocation.get("expected_seconds"))
    )
    if covered is not None and period is not None and period > 0:
        return max(0.0, min(100.0, 100 * covered / period))
    percent = _number(allocation.get("coverage_percent"))
    if percent is not None:
        return max(0.0, min(100.0, percent))
    return None


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
        and configured_ids.issubset(set(allocations)) and all(
            _classification_coverage(allocations[equipment_id]) is not None
            and _classification_coverage(allocations[equipment_id]) >= 99.999
            for equipment_id in configured_ids
        ),
    ))
    complete = meter_complete and allocation_complete
    date_coverage = coverage.get("date_coverage") or {}
    temporal_complete = bool(date_coverage.get(
        "complete_through_available_cutoff",
        date_coverage.get("complete", interval_complete),
    ))
    calendar_period_finalized = bool(
        date_coverage.get("complete", interval_complete)
        and record.get("partial") is not True
        and record.get("finalized") is not False
    )

    def kind_complete(kind: str) -> bool:
        items = [item for item in enabled if item.get("kind") == kind]
        return temporal_complete and all(
            isinstance(allocations.get(str(item["equipment_id"])), dict)
            and _number(allocations[str(item["equipment_id"])].get("meter_kwh")) is not None
            and _classification_coverage(
                allocations[str(item["equipment_id"])]
            ) is not None
            and _classification_coverage(
                allocations[str(item["equipment_id"])]
            ) >= 99.999
            and allocations[str(item["equipment_id"])].get("quality")
            not in {"missing", "poor", "missing_counter", "unreconciled"}
            for item in items
        )

    heat_pump_complete = kind_complete("heat_pump")
    ac_complete = kind_complete("air_conditioner")
    has_heat_pumps = any(item.get("kind") == "heat_pump" for item in enabled)
    has_air_conditioners = any(
        item.get("kind") == "air_conditioner" for item in enabled
    )
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
        known_available = bool(allocation.get("known_available", meter is not None))
        if "known_categories" in allocation:
            allocation = {**allocation, "categories": allocation["known_categories"],
                          "reported_assumed_heating_kwh": allocation.get("known_reported_assumed_heating_kwh")}
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
            unknown = _number(allocation.get("remaining_unassigned_after_fallback_kwh"))
            if unknown is None:
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
        device_coverage = _classification_coverage(allocation)
        classification_complete = (
            device_complete
            and device_coverage is not None and device_coverage >= 99.999
            and allocation.get("quality")
            not in {"missing", "poor", "missing_counter", "unreconciled"}
        )
        breakdown.append({
            **item, "available": device_complete, "known_available": known_available, "meter_kwh": meter,
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
            "raw_unassigned_before_fallback_kwh": allocation.get(
                "raw_unassigned_before_fallback_kwh"
            ),
            "heating_assumed_unknown_mode_kwh": allocation.get(
                "heating_assumed_unknown_mode_kwh"
            ),
            "heating_assumed_residual_kwh": allocation.get(
                "heating_assumed_residual_kwh"
            ),
            "remaining_unassigned_after_fallback_kwh": allocation.get(
                "remaining_unassigned_after_fallback_kwh"
            ),
            "signed_overallocation_or_reconciliation_error_kwh": allocation.get(
                "signed_overallocation_or_reconciliation_error_kwh"
            ),
            "coverage_percent": device_coverage,
            "coverage_known": device_coverage is not None,
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
    def category_evidence(item: dict[str, Any], names: tuple[str, ...]) -> bool:
        allocation = allocations.get(str(item.get("equipment_id"))) or {}
        categories = allocation.get("known_categories", allocation.get("categories")) or {}
        return bool(item.get("known_available", item.get("available"))) and (
            any(name in categories and _number(categories[name]) is not None for name in names)
            or (item.get("classification_complete") and not float(item.get("unknown_kwh") or 0)
                and not float(item.get("residual_kwh") or 0))
        )

    known_heating_evidence = any(category_evidence(item, (
        "heating", "confirmed_heating", "heating_assumed", "heating_selected", "heating_estimated"
    )) or (item.get("known_available", item.get("available")) and float(item.get("known_assumed_heating_kwh") or 0) > 0)
        for item in breakdown)
    known_dhw_evidence = any(item.get("kind") == "heat_pump" and category_evidence(item, ("dhw",)) for item in breakdown)
    known_cooling_evidence = any(category_evidence(item, ("cooling", "cooling_selected", "cooling_estimated")) for item in breakdown)
    known_meter_evidence = any(item.get("available") for item in breakdown)
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
        "known_space_heating_kwh": (
            known_space_heating if known_heating_evidence else None
        ),
        "known_dhw_kwh": known_dhw if known_dhw_evidence else None,
        "known_cooling_kwh": known_cooling if known_cooling_evidence else None,
        "known_idle_kwh": known_idle if known_meter_evidence else None,
        "known_other_hvac_kwh": (
            known_other_hvac if has_air_conditioners and known_meter_evidence else None
        ),
        "known_hvac_meter_kwh": known_hvac_meter if known_meter_evidence else None,
        "space_heating_kwh": known_space_heating if heating_complete else None,
        "dhw_kwh": known_dhw if has_heat_pumps and heat_pump_complete else None,
        "cooling_kwh": (
            known_cooling if enabled and cooling_complete else None
        ),
        "idle_kwh": known_idle if allocation_complete else None,
        "hvac_meter_kwh": known_hvac_meter if allocation_complete else None,
        "heating_and_dhw_kwh": (
            known_space_heating + known_dhw
            if has_heat_pumps and heating_complete and heat_pump_complete else None
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
    energy_uncertain = any(
        item.get("quality") in {"uncertain", "filtered", "estimated"}
        for item in breakdown if item.get("available")
    )
    energy_quality = (
        "partial" if not complete else
        ("unreconciled" if discrepancy is not None and abs(discrepancy) > 0.05 else
         ("uncertain" if energy_uncertain else "good"))
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
        # Temperature quality belongs to its independent coverage layer and
        # must not downgrade otherwise complete electricity evidence.
        "quality": energy_quality,
        "complete": complete,
        "complete_through_cutoff": bool(
            temporal_complete and meter_complete and allocation_complete
        ),
        "calendar_period_finalized": calendar_period_finalized,
        "provisional": not calendar_period_finalized,
        "provisional_reason": (
            "current_period_in_progress" if not calendar_period_finalized
            and temporal_complete else
            ("incomplete_intervals" if not temporal_complete else None)
        ),
        "incompleteness_reasons": [
            reason for condition, reason in (
                (not temporal_complete, "incomplete_intervals"),
                (not meter_complete, "missing_meter_evidence"),
                (not allocation_complete, "incomplete_classification"),
                (not calendar_period_finalized, "calendar_period_in_progress"),
            ) if condition
        ],
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


def heating_response_profile(
    records: Iterable[dict[str, Any]], equipment: Iterable[dict[str, Any]],
    *, outdoor_source_id: str | None, floor_area_m2: float | None,
    outdoor_source_is_measured_air: bool = True,
    minimum_temperature_coverage: float = 95.0,
    reconciliation_tolerance_kwh: float = 0.05,
) -> dict[str, Any]:
    """Build auditable electrical heating-response points and exclusions."""
    equipment = list(equipment)
    points: list[dict[str, Any]] = []
    for record in records:
        reasons: list[str] = []
        local_date = str(record.get("local_date") or "")
        try:
            datetime.fromisoformat(local_date).date()
        except ValueError:
            reasons.append("invalid_local_date")
        if record.get("partial") is True or record.get("finalized") is False:
            reasons.append("partial_day")
        temperature = (
            (record.get("temperatures") or {}).get(outdoor_source_id) or {}
            if outdoor_source_id else {}
        )
        outdoor_mean = _number(temperature.get("mean"))
        outdoor_coverage = _number(temperature.get("coverage_percent"))
        if not outdoor_source_id:
            reasons.append("representative_outdoor_not_configured")
        elif not outdoor_source_is_measured_air:
            reasons.append("representative_source_not_measured_outdoor_air")
        elif outdoor_mean is None:
            reasons.append("outdoor_temperature_unavailable")
        elif not isfinite(outdoor_mean) or outdoor_mean in {-55.0, 155.0}:
            reasons.append("invalid_outdoor_temperature")
        if outdoor_coverage is None:
            reasons.append("outdoor_coverage_unknown")
        elif outdoor_coverage < minimum_temperature_coverage:
            reasons.append("insufficient_outdoor_coverage")
        if temperature.get("quality") in {"missing", "poor", "rejected", "unreconciled"}:
            reasons.append("invalid_outdoor_quality")

        energy = canonical_energy(record, equipment)
        heating = _number(energy.get("space_heating_kwh"))
        if heating is None or not isfinite(heating):
            reasons.append("incomplete_heating_classification")
            if heating is not None and not isfinite(heating):
                heating = None
        discrepancy = _number(energy.get("unallocated_discrepancy_kwh"))
        if discrepancy is None:
            reasons.append("reconciliation_unknown")
        elif abs(discrepancy) > reconciliation_tolerance_kwh:
            reasons.append("unreconciled_hvac_energy")
        if not floor_area_m2 or floor_area_m2 <= 0 or not isfinite(float(floor_area_m2)):
            reasons.append("floor_area_unavailable")

        devices = energy.get("equipment") or []
        equipment_heating = []
        for item in devices:
            confirmed = _number(item.get("known_confirmed_heating_kwh")) or 0.0
            uncertain = sum(
                _number(item.get(key)) or 0.0 for key in (
                    "known_assumed_heating_kwh", "known_selected_heating_kwh",
                    "known_estimated_heating_kwh",
                )
            )
            equipment_heating.append({
                "equipment_id": item.get("equipment_id"),
                "display_name": item.get("display_name"),
                "kind": item.get("kind"),
                "confirmed_heating_kwh": confirmed,
                "uncertain_heating_kwh": uncertain,
                "space_heating_kwh": confirmed + uncertain,
                "classification_complete": bool(item.get("classification_complete")),
                "classification_quality": item.get("quality", "missing"),
                "classification_source": (
                    item.get("classification_source") or item.get("source")
                ),
                "coverage_percent": item.get("coverage_percent"),
            })
        outdoor_validated_seconds = _number(temperature.get("coverage_seconds"))
        point = {
            "local_date": local_date,
            "period_start_utc": record.get("period_start_utc"),
            "period_end_utc": record.get("period_end_utc"),
            "outdoor_source_id": outdoor_source_id,
            "outdoor_mean_c": outdoor_mean,
            "outdoor_coverage_percent": outdoor_coverage,
            "outdoor_validated_seconds": outdoor_validated_seconds,
            "outdoor_full_temporal_coverage": bool(
                outdoor_coverage is not None and outdoor_coverage >= 100.0
            ),
            "outdoor_response_coverage_qualified": bool(
                outdoor_coverage is not None
                and outdoor_coverage >= minimum_temperature_coverage
            ),
            "heating_electricity_kwh": heating,
            "heat_pump_heating_kwh": (
                None if heating is None else sum(
                    float(item.get("confirmed_heating_kwh", 0) or 0)
                    + float(item.get("assumed_heating_kwh", 0) or 0)
                    for item in devices if item.get("kind") == "heat_pump"
                )
            ),
            "ac_heating_kwh": (
                None if heating is None else sum(
                    float(item.get("confirmed_heating_kwh", 0) or 0)
                    + float(item.get("selected_heating_kwh", 0) or 0)
                    + float(item.get("estimated_heating_kwh", 0) or 0)
                    for item in devices if item.get("kind") == "air_conditioner"
                )
            ),
            "confirmed_heating_kwh": (
                None if heating is None else
                float(energy.get("confirmed_heat_pump_heating_kwh", 0) or 0)
                + float(energy.get("confirmed_ac_heating_kwh", 0) or 0)
            ),
            "uncertain_heating_kwh": (
                None if heating is None else
                float(energy.get("assumed_heat_pump_heating_kwh", 0) or 0)
                + float(energy.get("selected_ac_heating_kwh", 0) or 0)
                + float(energy.get("estimated_ac_heating_kwh", 0) or 0)
            ),
            "normalized_heating_kwh_m2_day": (
                heating / floor_area_m2
                if heating is not None and floor_area_m2 and floor_area_m2 > 0 else None
            ),
            "equipment_heating": equipment_heating,
            "classification_complete": bool(
                heating is not None and all(
                    item["classification_complete"] for item in equipment_heating
                )
            ),
            "reconciliation_discrepancy_kwh": discrepancy,
            "device_classification_quality": {
                str(item.get("equipment_id")): item.get("quality", "missing")
                for item in devices
            },
            "source_provenance": {
                "temperature_source_id": outdoor_source_id,
                "temperature": temperature.get("source_provenance"),
                "classification_sources": {
                    str(item.get("equipment_id")): item.get("classification_source")
                    or item.get("source")
                    for item in devices
                },
            },
            "status": "excluded" if reasons else "validated",
            "exclusion_reasons": sorted(set(reasons)),
        }
        points.append(point)
    return {
        "outdoor_source_id": outdoor_source_id,
        "minimum_temperature_coverage_percent": minimum_temperature_coverage,
        "minimum_regression_points": 10,
        "minimum_outdoor_span_c": 8.0,
        "minimum_regression_r_squared": 0.5,
        "points": points,
    }


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
        if key in {"household_kwh", "pv_kwh", "import_kwh", "export_kwh"} and not all(
            _valid_meter_value(item, key) for item in records
        ):
            return None
        values = [_number(item.get(key)) for item in records]
        return None if any(value is None for value in values) else sum(values)  # type: ignore[arg-type]

    def known_sum(key: str) -> float | None:
        values = [_number(item.get(key)) for item in records if _valid_meter_value(item, key)]
        values = [value for value in values if value is not None and isfinite(value) and value >= 0]
        return sum(values) if values else None

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
        known_parts = [part for part in available if _number(part.get("meter_kwh")) is not None
                       and isfinite(float(part["meter_kwh"])) and float(part["meter_kwh"]) >= 0
                       and part.get("quality") not in {"missing", "missing_counter", "unreconciled", "poor"}]
        categories = set().union(*(set(part.get("categories", {})) for part in available))
        seconds = 0.0
        covered = 0.0
        coverage_known = len(available) == len(records)
        for record, part in zip(records, parts, strict=True):
            period = record_seconds(record)
            if isinstance(part, dict):
                period = float(part.get("period_seconds", period) or period)
                percent = _classification_coverage(part)
                if percent is None:
                    coverage_known = False
                else:
                    covered += period * percent / 100
            else:
                coverage_known = False
            seconds += period
        part_qualities = {str(part.get("quality", "missing")) for part in available}
        allocation_quality = (
            "partial" if len(available) != len(records) or part_qualities.intersection(
                {"partial", "poor", "missing", "missing_counter", "unreconciled"}
            ) else
            ("uncertain" if part_qualities.intersection({"uncertain", "estimated"}) else
             ("filtered" if "filtered" in part_qualities else "good"))
        )
        allocations[equipment_id] = {
            "meter_kwh": (None if len(available) != len(records) or any(
                _number(part.get("meter_kwh")) is None for part in available
            ) else sum(float(part["meter_kwh"]) for part in available)),
            "categories": {name: sum(float(part.get("categories", {}).get(name, 0) or 0)
                                      for part in available) for name in categories},
            "known_categories": {name: sum(float(part.get("categories", {}).get(name, 0) or 0)
                                             for part in known_parts)
                                 for name in set().union(*(set(part.get("categories", {})) for part in known_parts))},
            "known_available": bool(known_parts),
            "known_reported_assumed_heating_kwh": sum(float(part.get("reported_assumed_heating_kwh", 0) or 0) for part in known_parts),
            "residual_kwh": (None if len(available) != len(records) or any(
                _number(part.get("residual_kwh")) is None for part in available
            ) else sum(float(part["residual_kwh"]) for part in available)),
            "uncertain_residual_kwh": sum(float(part.get("uncertain_residual_kwh", 0) or 0)
                                            for part in available),
            "reported_assumed_heating_kwh": sum(
                float(part.get("reported_assumed_heating_kwh", 0) or 0) for part in available
            ),
            **{
                key: (
                    None if len(available) != len(records) or any(
                        _number(part.get(key)) is None for part in available
                    ) else sum(float(part[key]) for part in available)
                )
                for key in (
                    "shelly_total_kwh", "heating_confirmed_kwh",
                    "dhw_confirmed_kwh", "idle_other_kwh",
                    "heating_assumed_unknown_mode_kwh",
                    "positive_reconciliation_residual_kwh",
                    "heating_assumed_residual_kwh",
                    "raw_unassigned_before_fallback_kwh",
                    "remaining_unassigned_after_fallback_kwh",
                    "signed_overallocation_or_reconciliation_error_kwh",
                )
            },
            "coverage_percent": (
                100 * covered / seconds if coverage_known and seconds else None
            ),
            "known_coverage_percent": 100 * covered / seconds if seconds else None,
            "coverage_known": coverage_known,
            "covered_seconds": covered, "period_seconds": seconds,
            "quality": allocation_quality,
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
        temperature_complete = (
            len(available) == len(records) and period > 0
            and coverage >= period - 0.001
        )
        temperature_qualities = {
            str(part.get("quality", "missing")) for part in available
        }
        temperature_quality = (
            "missing" if not coverage else
            ("partial" if not temperature_complete or temperature_qualities.intersection(
                {"missing", "poor", "rejected", "unreconciled"}
            ) else
             ("filtered" if "filtered" in temperature_qualities or any(
                 int(part.get("rejected_count", 0) or 0) > 0 for part in available
             ) else "good"))
        )
        temperatures[source_id] = {
            "mean": sum(value * weight for value, weight in means) / coverage if coverage else None,
            "minimum": min((float(part["minimum"]) for part in available
                            if _number(part.get("minimum")) is not None), default=None),
            "maximum": max((float(part["maximum"]) for part in available
                            if _number(part.get("maximum")) is not None), default=None),
            "coverage_seconds": coverage, "period_seconds": period,
            "coverage_percent": 100 * coverage / period if period else 0.0,
            "quality": temperature_quality,
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
        allocation_coverage_known = bool(allocations) and all(
            item.get("coverage_known") is True for item in allocations.values()
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
                    if allocation_coverage_known and allocation_expected else None
                ),
                "known_coverage_percent": (
                    100 * allocation_covered / allocation_expected
                    if allocation_expected else None
                ),
                "coverage_known": allocation_coverage_known,
                "quality": (
                    "unavailable" if not allocation_expected else
                    ("uncertain" if allocation_uncertain else
                     ("good" if allocation_complete else "partial"))
                ),
            },
            "temperature_coverage": {
                source_id: {
                    "complete": float(item.get("coverage_percent", 0) or 0) >= 99.999,
                    "response_eligible": (
                        float(item.get("coverage_percent", 0) or 0) >= 95.0
                        and item.get("mean") is not None
                    ),
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
        "known_household_kwh": known_sum("household_kwh"), "known_pv_kwh": known_sum("pv_kwh"),
        "known_import_kwh": known_sum("import_kwh"), "known_export_kwh": known_sum("export_kwh"),
        "known_non_hvac_kwh": non_hvac,
        "household_kwh": household if interval_complete else None,
        "pv_kwh": pv if interval_complete else None,
        "import_kwh": imported if interval_complete else None,
        "export_kwh": exported if interval_complete else None,
        "non_hvac_kwh": non_hvac if interval_complete else None,
        "quality": (
            "partial" if not interval_complete or any(
                item.get("quality") in {"missing", "poor", "unreconciled"}
                for item in records
            ) else
            ("uncertain" if any(item.get("quality") in {"uncertain", "estimated"}
                                for item in records) else
             ("filtered" if any(item.get("quality") == "filtered" for item in records)
              else "good"))
        ),
        "interval_coverage": coverage,
        "updated_at": records[-1].get("updated_at") or records[-1].get("collected_at"),
        "temperatures": temperatures, "allocations": allocations,
        "provenance": {"source": "persisted_hourly_records", "interval_count": len(records)},
    }


def provisional_completed_day_from_hourly(
    records: list[dict[str, Any]], start: datetime, end: datetime,
    *, local_date: str, timezone: str,
) -> dict[str, Any] | None:
    """Build a non-persisted completed-day view from hourly evidence.

    Temperature-only rows contribute temperature coverage but cannot satisfy
    electrical interval coverage or manufacture zero electricity.
    """
    if not records:
        return None
    energy_records = [item for item in records if not item.get("temperature_only")]
    temperature_record = combine_interval_records(
        records, expected_start=start, expected_end=end,
    )
    combined = combine_interval_records(
        energy_records, expected_start=start, expected_end=end,
    )
    if combined is None:
        combined = {
            "local_date": local_date, "timezone": timezone,
            "period_start_utc": start.isoformat(), "period_end_utc": end.isoformat(),
            "day_seconds": int((end - start).total_seconds()),
            "known_household_kwh": None, "known_pv_kwh": None,
            "known_import_kwh": None, "known_export_kwh": None,
            "known_non_hvac_kwh": None, "household_kwh": None, "pv_kwh": None,
            "import_kwh": None, "export_kwh": None, "non_hvac_kwh": None,
            "quality": "missing", "allocations": {},
            "interval_coverage": interval_coverage([], start, end),
            "provenance": {"source": "persisted_hourly_records", "interval_count": 0},
        }
    combined["local_date"] = local_date
    combined["timezone"] = timezone
    combined["temperatures"] = (
        temperature_record.get("temperatures", {}) if temperature_record else {}
    )
    combined.update({
        "provisional": True,
        "partial": True,
        "finalized": False,
    })
    combined.setdefault("provenance", {}).update({
        "source": "persisted_hourly_records",
        "report_status": "provisional_completed_day",
        "authoritative_daily_record_pending": True,
    })
    return combined


def prefer_authoritative_daily(
    stored_records: list[dict[str, Any]], provisional: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Add a provisional day only until an authoritative row with that date exists."""
    if provisional is None:
        return list(stored_records)
    local_date = str(provisional.get("local_date"))
    if any(
        str(item.get("local_date")) == local_date
        and not item.get("temperature_only")
        and not (item.get("provisional") is True and item.get("finalized") is False)
        for item in stored_records
    ):
        return list(stored_records)
    temperatures = dict(provisional.get("temperatures") or {})
    for item in stored_records:
        if str(item.get("local_date")) == local_date and item.get("temperature_only"):
            temperatures.update(item.get("temperatures") or {})
    if temperatures:
        provisional = {**provisional, "temperatures": temperatures}
    return [
        *[item for item in stored_records if str(item.get("local_date")) != local_date],
        provisional,
    ]


def daily_coverage(
    records: list[dict[str, Any]], start_date: str, end_date: str,
    *, equipment_ids: Iterable[str] = (), temperature_source_ids: Iterable[str] = (),
    timezone: str | None = None, response_temperature_coverage_minimum: float = 95.0,
) -> dict[str, Any]:
    """Derive independent quality layers for a local-date range.

    The persisted ``quality`` column is deliberately not authoritative here.
    Older rows may predate a repair that added valid temperature evidence, or
    may have been labelled partial because an unrelated optional sensor was
    absent.  Public quality is therefore rebuilt from present-day evidence;
    the legacy label is returned only as provenance.
    """
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
    stored_quality = {
        day: str(item.get("stored_quality", item.get("quality", "unknown")))
        for day, item in by_date.items()
    }
    temporal_partial = [
        day for day, item in by_date.items()
        if item.get("partial") is True or item.get("finalized") is False
    ]

    zone_name = timezone or next(
        (str(item.get("timezone")) for item in records if item.get("timezone")), "UTC"
    )

    def calendar_seconds(day: str) -> float:
        start_utc, end_utc = local_day_bounds(datetime.fromisoformat(day).date(), zone_name)
        return (end_utc - start_utc).total_seconds()

    def expected_seconds(day: str) -> float:
        item = by_date.get(day)
        if item and (item.get("partial") is True or item.get("finalized") is False):
            return max(0.0, float(item.get("day_seconds", 0) or 0))
        return calendar_seconds(day)

    def field_coverage(field: str) -> dict[str, Any]:
        unavailable = [day for day in expected if not _valid_meter_value(by_date.get(day, {}), field)]
        total_seconds = sum(expected_seconds(day) for day in expected)
        available_seconds = sum(
            expected_seconds(day) for day in expected if day not in unavailable
        )
        return {
            "available_days": len(expected) - len(unavailable),
            "unavailable_dates": unavailable,
            "missing_record_dates": [day for day in unavailable if day not in by_date],
            "missing_measurement_dates": [day for day in unavailable if day in by_date and _number(by_date[day].get(field)) is None],
            "invalid_measurement_dates": [day for day in unavailable if day in by_date and _number(by_date[day].get(field)) is not None],
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
    allocation_unknown_coverage: list[dict[str, str]] = []
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
            percent = _classification_coverage(value)
            if percent is None:
                allocation_unknown_coverage.append({
                    "date": day, "equipment_id": equipment_id,
                })
                covered = 0.0
            else:
                covered = period * percent / 100
            allocation_classified_seconds += covered
            quality = value.get("quality")
            if (
                _number(value.get("meter_kwh")) is None
                or percent is None
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
    date_expected_seconds = sum(calendar_seconds(day) for day in expected)
    date_available_seconds = sum(
        0.0 if day not in by_date else (
            expected_seconds(day)
            if by_date[day].get("partial") is True
            or by_date[day].get("finalized") is False
            else calendar_seconds(day)
        )
        for day in expected
    )
    date_layer = {
        "expected_days": len(expected), "available_days": len(expected) - len(missing),
        "missing_dates": missing,
        "available_seconds": date_available_seconds,
        "expected_seconds": date_expected_seconds,
        "coverage_percent": (
            100 * date_available_seconds / date_expected_seconds
            if date_expected_seconds else None
        ),
        "complete": not missing and not temporal_partial,
        "complete_through_available_cutoff": not missing and all(
            not (
                by_date[day].get("partial") is True
                or by_date[day].get("finalized") is False
            ) or bool((by_date[day].get("interval_coverage") or {}).get("complete"))
            for day in expected if day in by_date
        ),
        "scope": "full_local_calendar_days",
    }
    allocation_layer = {
        "missing_dates": allocation_missing, "partial_dates": allocation_partial,
        "uncertain_dates": sorted(set(allocation_uncertain)),
        "missing_equipment": allocation_missing_equipment,
        "unknown_coverage_equipment": allocation_unknown_coverage,
        "classified_seconds": allocation_classified_seconds,
        "expected_seconds": allocation_expected_seconds,
        "coverage_percent": (
            100 * allocation_classified_seconds / allocation_expected_seconds
            if allocation_expected_seconds and not allocation_unknown_coverage else None
        ),
        "known_coverage_percent": (
            100 * allocation_classified_seconds / allocation_expected_seconds
            if allocation_expected_seconds else None
        ),
        "coverage_known": not allocation_unknown_coverage,
        "processing_complete": bool(configured_equipment) and not allocation_missing_equipment,
        "complete": bool(configured_equipment) and not allocation_missing and not allocation_partial,
        "quality": (
            "unavailable" if not configured_equipment else
            ("uncertain" if not allocation_missing and not allocation_partial
             and allocation_uncertain else
             ("good" if not allocation_missing and not allocation_partial else "partial"))
        ),
    }
    required_temperature_sources = tuple(dict.fromkeys(
        str(source_id) for source_id in temperature_source_ids if source_id
    ))
    temperature_layers: dict[str, dict[str, Any]] = {}
    for source_id in required_temperature_sources:
        valid_seconds = 0.0
        rejected_count = 0
        source_missing_dates: list[str] = []
        source_partial_dates: list[str] = []
        source_filtered_dates: list[str] = []
        response_ineligible_dates: list[str] = []
        for day in expected:
            summary = (by_date.get(day, {}).get("temperatures") or {}).get(source_id)
            if not isinstance(summary, dict) or _number(summary.get("mean")) is None:
                source_missing_dates.append(day)
                response_ineligible_dates.append(day)
                continue
            day_expected = expected_seconds(day)
            covered = max(0.0, min(
                day_expected, float(summary.get("coverage_seconds", 0) or 0)
            ))
            valid_seconds += covered
            rejected_count += int(summary.get("rejected_count", 0) or 0)
            percent = 100 * covered / day_expected if day_expected else 0.0
            invalid_quality = summary.get("quality") in {
                "missing", "poor", "rejected", "unreconciled",
            }
            if percent + 1e-9 < 100 or invalid_quality:
                source_partial_dates.append(day)
            elif summary.get("quality") == "filtered" or int(
                summary.get("rejected_count", 0) or 0
            ):
                source_filtered_dates.append(day)
            if percent + 1e-9 < response_temperature_coverage_minimum or invalid_quality:
                response_ineligible_dates.append(day)
        source_expected_seconds = sum(expected_seconds(day) for day in expected)
        percent = (
            100 * valid_seconds / source_expected_seconds
            if source_expected_seconds else None
        )
        complete = bool(
            source_expected_seconds and not source_missing_dates
            and not source_partial_dates and percent is not None
            and percent + 1e-9 >= 100
        )
        response_eligible = bool(
            source_expected_seconds and not response_ineligible_dates
            and percent is not None
            and percent + 1e-9 >= response_temperature_coverage_minimum
        )
        quality = (
            "missing" if not valid_seconds else
            ("partial" if not complete else
             ("filtered" if source_filtered_dates else "good"))
        )
        temperature_layers[source_id] = {
            "valid_seconds": valid_seconds,
            "expected_seconds": source_expected_seconds,
            "coverage_percent": percent,
            "missing_dates": source_missing_dates,
            "partial_dates": source_partial_dates,
            "filtered_dates": source_filtered_dates,
            "rejected_count": rejected_count,
            "complete": complete,
            "full_temporal_coverage": complete,
            "response_eligible": response_eligible,
            "response_coverage_minimum_percent": response_temperature_coverage_minimum,
            "response_ineligible_dates": sorted(set(response_ineligible_dates)),
            "quality": quality,
        }

    date_complete = bool(date_layer["complete"])
    household_complete = bool(meter_fields["household_kwh"]["complete"])
    allocation_complete = bool(allocation_layer["complete"])
    temperature_complete = all(
        item["complete"] for item in temperature_layers.values()
    ) if temperature_layers else True
    quality_unknown = bool(
        configured_equipment and allocation_layer["unknown_coverage_equipment"]
    )
    required_layers_complete = (
        date_complete and household_complete and allocation_complete
        and temperature_complete
    )
    uncertain = bool(allocation_layer["uncertain_dates"])
    derived_quality = (
        "missing" if len(missing) == len(expected) and expected else
        ("unknown" if quality_unknown else
         ("partial" if not required_layers_complete else
          ("uncertain" if uncertain else "good")))
    )
    derived_incomplete_dates = sorted(set(
        missing + temporal_partial + meter_fields["household_kwh"]["unavailable_dates"]
        + allocation_missing + allocation_partial
        + [day for item in temperature_layers.values()
           for day in item["missing_dates"] + item["partial_dates"]]
    ))
    return {
        "expected_days": len(expected), "present_days": len(expected) - len(missing),
        "missing_dates": missing, "poor_quality_dates": derived_incomplete_dates,
        "uncertain_quality_dates": sorted(set(allocation_uncertain)),
        "partial_dates": temporal_partial,
        "coverage_percent": date_layer["coverage_percent"],
        "date_coverage": date_layer,
        "meter_coverage": {"fields": meter_fields, **meter_fields["household_kwh"]},
        "allocation_coverage": allocation_layer,
        "temperature_coverage": temperature_layers,
        "energy_complete": date_complete and household_complete and allocation_complete,
        "temperature_complete": temperature_complete,
        "complete": required_layers_complete,
        "quality": derived_quality,
        "quality_basis": "derived_from_independent_coverage_layers_v2",
        "stored_quality_provenance": stored_quality,
    }


def combine_daily_records(
    records: list[dict[str, Any]], start_date: str, end_date: str,
    *, temperature_source_ids: Iterable[str] = (), equipment_ids: Iterable[str] = (),
    required_temperature_source_ids: Iterable[str] | None = None,
    timezone: str | None = None, response_temperature_coverage_minimum: float = 95.0,
) -> dict[str, Any] | None:
    """Combine the full requested daily range with explicit missing dates."""
    if not records:
        return None
    combined = combine_interval_records(records)
    assert combined is not None
    if any(item.get("provisional") is True for item in records):
        combined.update({"provisional": True, "partial": True, "finalized": False})
    coverage = daily_coverage(
        records, start_date, end_date,
        equipment_ids=equipment_ids,
        temperature_source_ids=(
            temperature_source_ids if required_temperature_source_ids is None
            else required_temperature_source_ids
        ),
        timezone=timezone,
        response_temperature_coverage_minimum=response_temperature_coverage_minimum,
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
            else (("filtered" if item.get("quality") == "filtered"
                   or int(item.get("rejected_count", 0) or 0) else "good")
                  if valid_seconds + 1e-9 >= expected_seconds else "partial")
        )
        full_temporal_coverage = bool(
            expected_seconds and valid_seconds + 1e-9 >= expected_seconds
        )
        response_eligible = bool(
            expected_seconds and 100 * valid_seconds / expected_seconds + 1e-9
            >= response_temperature_coverage_minimum
        )
        item["full_temporal_coverage"] = full_temporal_coverage
        item["response_eligible"] = response_eligible
        item["response_coverage_minimum_percent"] = (
            response_temperature_coverage_minimum
        )
        source_coverage = {
            "valid_seconds": valid_seconds, "expected_seconds": expected_seconds,
            "coverage_percent": item["coverage_percent"],
            "complete": full_temporal_coverage,
            "full_temporal_coverage": full_temporal_coverage,
            "response_eligible": response_eligible,
            "response_coverage_minimum_percent": response_temperature_coverage_minimum,
            "quality": item["quality"],
        }
        if source_id not in coverage["temperature_coverage"]:
            coverage.setdefault("optional_temperature_coverage", {})[source_id] = source_coverage
    combined["stored_quality"] = combined.get("quality")
    combined["quality"] = coverage["quality"]
    combined["quality_layers"] = {
        key: coverage[key] for key in (
            "date_coverage", "meter_coverage", "allocation_coverage",
            "temperature_coverage",
        )
    }
    return combined


def derive_record_quality(
    record: dict[str, Any], *, equipment_ids: Iterable[str] = (),
    temperature_source_ids: Iterable[str] = (), timezone: str | None = None,
    response_temperature_coverage_minimum: float = 95.0,
) -> dict[str, Any]:
    """Return one record with read-time, evidence-derived quality metadata."""
    local_date = str(record["local_date"])
    coverage = daily_coverage(
        [record], local_date, local_date, equipment_ids=equipment_ids,
        temperature_source_ids=temperature_source_ids, timezone=timezone,
        response_temperature_coverage_minimum=response_temperature_coverage_minimum,
    )
    result = dict(record)
    result["stored_quality"] = record.get("stored_quality", record.get("quality"))
    result["quality"] = coverage["quality"]
    result["quality_layers"] = {
        key: coverage[key] for key in (
            "date_coverage", "meter_coverage", "allocation_coverage",
            "temperature_coverage",
        )
    }
    result["quality_basis"] = coverage["quality_basis"]
    return result


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

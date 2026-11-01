"""Monthly Ecodan report snapshots and Shelly-first rolling analytics."""

from __future__ import annotations

import math
from calendar import monthrange
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .analytics import canonical_energy, combine_daily_records, combine_interval_records, interval_coverage
from .thermal import resolve_local_timestamp
from .timeutils import local_day_bounds

_ENERGY_FACTORS = {"Wh": 0.001, "kWh": 1.0, "MWh": 1000.0}
_SNAPSHOT_FIELDS = (
    "heating_thermal_kwh", "dhw_thermal_kwh",
    "vendor_heating_electricity_kwh", "vendor_dhw_electricity_kwh",
    "vendor_combined_electricity_kwh", "vendor_combined_thermal_kwh",
)


def _energy(value: Any, name: str, factor: float) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as err:
        raise ValueError(f"{name} must be numeric") from err
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{name} must be finite and at least zero")
    return number * factor


def normalize_monthly_snapshot(data: dict[str, Any], timezone: str) -> dict[str, Any]:
    """Validate a monthly/MtD report snapshot without cumulative-ledger semantics."""
    unit = str(data.get("unit") or "kWh")
    factor = _ENERGY_FACTORS.get(unit)
    if factor is None:
        raise ValueError("unit must be Wh, kWh, or MWh")
    equipment_id = str(data.get("ecodan_equipment_id") or "").strip()
    meter_identity = str(data.get("meter_identity") or "monthly_report").strip()
    source_label = str(data.get("source_label") or "").strip()
    if not equipment_id or not meter_identity or not source_label:
        raise ValueError("equipment, report identity, and source label are required")
    report_month = str(data.get("report_month") or "")
    try:
        month_start = date.fromisoformat(f"{report_month}-01")
    except ValueError as err:
        raise ValueError("report_month must use YYYY-MM") from err
    cutoff_date_text = str(data.get("cutoff_local_date") or "")
    try:
        cutoff_date = date.fromisoformat(cutoff_date_text)
    except ValueError as err:
        raise ValueError("cutoff_local_date is required") from err
    next_month = (
        date(month_start.year + 1, 1, 1)
        if month_start.month == 12
        else date(month_start.year, month_start.month + 1, 1)
    )
    cutoff_time = str(data.get("cutoff_local_time") or "").strip() or None
    fold = int(data.get("fold") or 0)
    certainty = str(data.get("cutoff_certainty") or (
        "exact" if cutoff_time else "day_only"
    ))
    if certainty not in {"exact", "day_only", "unknown"}:
        raise ValueError("cutoff certainty must be exact, day_only, or unknown")
    if cutoff_date < month_start or cutoff_date > next_month:
        raise ValueError("cutoff date must fall within the report month or its end boundary")
    if cutoff_date == next_month and not (
        certainty == "exact" and cutoff_time in {"00:00", "00:00:00"}
    ):
        raise ValueError(
            "cutoff must be within the report month unless it is the exact month-end midnight"
        )
    if certainty == "exact" and not cutoff_time:
        raise ValueError("an exact cutoff requires local time")
    if certainty == "exact":
        cutoff_utc = resolve_local_timestamp(
            cutoff_date_text, cutoff_time, timezone, data.get("fold")
        )
    elif certainty == "day_only":
        # A date-only report is comparable only through the end of that local date.
        zone = ZoneInfo(timezone)
        cutoff_utc = datetime.combine(
            cutoff_date + timedelta(days=1), time.min, zone
        ).astimezone(UTC)
        cutoff_time = None
    else:
        cutoff_utc = None
        cutoff_time = None
    status = str(data.get("status") or "partial")
    if status not in {"partial", "final"}:
        raise ValueError("status must be partial or final")
    final_boundary_utc = datetime.combine(next_month, time.min, ZoneInfo(timezone)).astimezone(UTC)
    if status == "final" and not (
        certainty == "exact" and cutoff_utc == final_boundary_utc
    ):
        raise ValueError("a final month requires an exact next-month midnight cutoff")
    entry_now = datetime.now(ZoneInfo(timezone))
    entry_date_text = str(data.get("entry_local_date") or entry_now.date().isoformat())
    entry_time_text = str(
        data.get("entry_local_time") or entry_now.time().replace(microsecond=0).isoformat()
    )
    entry_utc = resolve_local_timestamp(
        entry_date_text, entry_time_text, timezone, data.get("entry_fold", 0)
    )
    normalized: dict[str, Any] = {
        "ecodan_equipment_id": equipment_id,
        "meter_identity": meter_identity,
        "report_month": report_month,
        "cutoff_local_date": cutoff_date_text,
        "cutoff_local_time": cutoff_time,
        "cutoff_utc": cutoff_utc.isoformat() if cutoff_utc is not None else None,
        "timezone": timezone,
        "fold": fold,
        "entry_local_date": entry_date_text,
        "entry_local_time": entry_time_text,
        "entry_utc": entry_utc.isoformat(),
        "cutoff_certainty": certainty,
        "status": status,
        "source_label": source_label,
        "source_reference": str(data.get("source_reference") or "").strip() or None,
        "note": str(data.get("note") or "").strip() or None,
        "unit": "kWh",
    }
    for field in _SNAPSHOT_FIELDS:
        normalized[field] = _energy(data.get(field), field, factor)
    if not any(normalized[field] is not None for field in _SNAPSHOT_FIELDS):
        raise ValueError("at least one monthly energy value is required")
    return normalized


def rolling_months(end_month: str, count: int = 13) -> list[str]:
    """Return chronological calendar month labels ending at ``end_month``."""
    if count < 1 or count > 120:
        raise ValueError("month count must be between 1 and 120")
    cursor = date.fromisoformat(f"{end_month}-01")
    result: list[str] = []
    for _ in range(count):
        result.append(cursor.strftime("%Y-%m"))
        cursor = (cursor.replace(day=1) - timedelta(days=1)).replace(day=1)
    return list(reversed(result))


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    return (
        float(numerator) / float(denominator)
        if numerator is not None and denominator is not None and denominator > 0
        else None
    )


def _sum_present(values: Iterable[float | None]) -> float | None:
    values = list(values)
    present = [float(value) for value in values if value is not None]
    return sum(present) if present else None


def _sum_complete(values: Iterable[float | None]) -> float | None:
    """Sum a required set only when every component is measured."""
    values = list(values)
    return None if not values or any(value is None for value in values) else sum(values)


def _allocation_subtotal(item: dict[str, Any], *keys: str) -> float | None:
    """Keep a confirmed zero distinct from an unavailable allocation subtotal."""
    return _sum_complete(item.get(key) for key in keys)


def _known_allocation_subtotal(records, equipment, kind, categories):
    """Sum explicitly recorded categories only, preserving absence as unknown."""
    ids = {str(item.get("equipment_id")) for item in equipment
           if item.get("enabled", True) and item.get("kind") == kind}
    values = []
    for record in records:
        for equipment_id, allocation in (record.get("allocations") or {}).items():
            if str(equipment_id) not in ids or not isinstance(allocation, dict):
                continue
            if allocation.get("meter_kwh") is None or allocation.get("quality") in {"missing_counter", "unreconciled"}:
                continue
            entries = allocation.get("categories") or {}
            for key in categories:
                value = entries.get(key)
                if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0:
                    values.append(float(value))
    return sum(values) if values else None


def _dhw_zero_proven(records, equipment):
    ids = [str(item.get("equipment_id")) for item in equipment
           if item.get("enabled", True) and item.get("kind") == "heat_pump"]
    if not ids or not records:
        return False
    for record in records:
        for equipment_id in ids:
            allocation = (record.get("allocations") or {}).get(equipment_id) or {}
            categories = allocation.get("categories") or {}
            if (allocation.get("quality") not in {"good", "filtered"}
                    or allocation.get("meter_kwh") is None
                    or float(allocation.get("coverage_percent") or 0) < 99.999
                    or any(float(value or 0) != 0 for value in (
                        categories.get("dhw"), categories.get("unknown"),
                        categories.get("heating_assumed"), allocation.get("residual_kwh"),
                        allocation.get("heating_assumed_unknown_mode_kwh"),
                    ))):
                return False
    return True


def _equipment_total(
    devices: list[dict[str, Any]], *keys: str, optional_when_empty: bool = False,
) -> float | None:
    """Aggregate dynamic equipment without turning a missing device into zero."""
    if not devices:
        return 0.0 if optional_when_empty else None
    return _sum_complete(_allocation_subtotal(item, *keys) for item in devices)


def latest_monthly_snapshots(
    snapshots: Iterable[dict[str, Any]], report_month: str,
) -> dict[str, dict[str, Any]]:
    """Choose the latest auditable snapshot for each equipment ID in a month."""
    latest: dict[str, dict[str, Any]] = {}
    for item in snapshots:
        if item.get("report_month") != report_month:
            continue
        equipment_id = str(item.get("ecodan_equipment_id"))
        current = latest.get(equipment_id)
        key = (str(item.get("cutoff_utc") or ""), int(item.get("id", 0)))
        current_key = (
            (str(current.get("cutoff_utc") or ""), int(current.get("id", 0)))
            if current else ("", -1)
        )
        if current is None or key > current_key:
            latest[equipment_id] = item
    return latest


def build_rolling_month_report(
    *, daily_records: Iterable[dict[str, Any]], snapshots: Iterable[dict[str, Any]],
    equipment: list[dict[str, Any]], timezone: str, end_month: str, count: int = 13,
    floor_area_m2: float | None = None,
    hourly_records: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Build complete calendar-month rows from compact SQLite evidence."""
    months = rolling_months(end_month, count)
    by_month: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in daily_records:
        local_date = str(record.get("local_date") or "")
        if len(local_date) >= 7 and local_date[:7] in months:
            by_month[local_date[:7]].append(record)
    all_snapshots = list(snapshots)
    all_hours = list(hourly_records)
    heat_pump_ids = {
        str(item.get("equipment_id")) for item in equipment
        if item.get("enabled", True) and item.get("kind") == "heat_pump"
    }
    rows: list[dict[str, Any]] = []
    for month in months:
        year, number = (int(part) for part in month.split("-"))
        month_start = date(year, number, 1)
        month_end = date(year, number, monthrange(year, number)[1])
        selected = latest_monthly_snapshots(all_snapshots, month)
        selected_values = list(selected.values())
        cutoff_values = sorted(
            {str(item.get("cutoff_utc")) for item in selected_values if item.get("cutoff_utc")}
        )
        cutoff_local_dates = sorted({
            str(item.get("cutoff_local_date"))
            for item in selected_values if item.get("cutoff_local_date")
        })
        report_end = month_end
        if len(cutoff_local_dates) == 1:
            report_end = date.fromisoformat(cutoff_local_dates[0])
            if selected_values and all(
                item.get("cutoff_certainty") == "exact"
                and item.get("cutoff_local_time") in {"00:00", "00:00:00"}
                for item in selected_values
            ):
                report_end -= timedelta(days=1)
            if report_end > month_end:
                report_end = month_end
        all_month_records = by_month.get(month, [])
        records = [
            record for record in all_month_records
            if str(record.get("local_date") or "") <= report_end.isoformat()
            and (
                len(cutoff_values) != 1
                or str(record.get("period_end_utc") or "") <= cutoff_values[0]
            )
        ]
        combined = combine_daily_records(
            records, month_start.isoformat(), report_end.isoformat(),
            equipment_ids=[str(item.get("equipment_id")) for item in equipment if item.get("enabled", True)],
            timezone=timezone,
        ) if records else None
        month_start_utc, _ = local_day_bounds(month_start, timezone)
        if len(cutoff_values) == 1 and selected_values and all(
            item.get("cutoff_certainty") == "exact" for item in selected_values
        ):
            cutoff = datetime.fromisoformat(cutoff_values[0])
            seconds = (cutoff - month_start_utc).total_seconds()
            hours = [item for item in all_hours if not item.get("temperature_only")
                     and str(item.get("period_start_utc")) >= month_start_utc.isoformat()
                     and str(item.get("period_end_utc")) <= cutoff.isoformat()]
            if seconds > 0 and seconds % 3600 == 0 and hours:
                coverage = interval_coverage(hours, month_start_utc, cutoff)
                hourly = combine_interval_records(hours, expected_start=month_start_utc, expected_end=cutoff)
                if coverage["complete"] and len(hours) == coverage["expected_intervals"] and hourly:
                    hourly["period_coverage"] = {
                        "date_coverage": {"complete_through_available_cutoff": True},
                        "allocation_coverage": {"complete": all(
                            item.get("allocations", {}).get(eid, {}).get("coverage_percent") == 100
                            and item.get("allocations", {}).get(eid, {}).get("meter_kwh") is not None
                            and item.get("allocations", {}).get(eid, {}).get("quality") not in {"partial", "poor", "missing", "missing_counter", "unreconciled"}
                            for item in hours for eid in heat_pump_ids
                        )},
                    }
                    combined = hourly
        energy = canonical_energy(combined or {}, equipment)
        thermal_heating = _sum_present(
            item.get("heating_thermal_kwh") for item in selected_values
        )
        thermal_dhw = _sum_present(item.get("dhw_thermal_kwh") for item in selected_values)
        vendor_heating_electricity = _sum_present(
            item.get("vendor_heating_electricity_kwh") for item in selected_values
        )
        vendor_dhw_electricity = _sum_present(
            item.get("vendor_dhw_electricity_kwh") for item in selected_values
        )
        vendor_combined_electricity = _sum_present(
            item.get("vendor_combined_electricity_kwh") for item in selected_values
        )
        vendor_combined_thermal = _sum_present(
            item.get("vendor_combined_thermal_kwh") for item in selected_values
        )
        device_rows = energy.get("equipment", [])
        heat_pump_rows = [item for item in device_rows if item.get("kind") == "heat_pump"]
        ac_rows = [item for item in device_rows if item.get("kind") == "air_conditioner"]
        confirmed = _equipment_total(heat_pump_rows, "known_confirmed_heating_kwh")
        assumed = _equipment_total(heat_pump_rows, "known_assumed_heating_kwh")
        shelly_heating = (
            None if confirmed is None and assumed is None
            else float(confirmed or 0) + float(assumed or 0)
        )
        shelly_dhw = _equipment_total(heat_pump_rows, "known_dhw_kwh")
        shelly_idle_other = _sum_complete(
            _allocation_subtotal(item, "known_idle_kwh", "known_other_hvac_kwh")
            for item in heat_pump_rows
        )
        shelly_ecodan_total = _equipment_total(heat_pump_rows, "meter_kwh")
        ac_heating = _equipment_total(
            ac_rows, "known_confirmed_heating_kwh", "known_selected_heating_kwh",
            "known_estimated_heating_kwh", optional_when_empty=True,
        )
        ac_cooling = _equipment_total(
            ac_rows, "known_confirmed_cooling_kwh", "known_selected_cooling_kwh",
            "known_estimated_cooling_kwh", optional_when_empty=True,
        )
        whole_house_heating = _sum_complete((shelly_heating, ac_heating))
        generated_total = _sum_complete((thermal_heating, thermal_dhw))
        energy_dates = sorted(str(item.get("local_date")) for item in records)
        month_start_utc, _ = local_day_bounds(month_start, timezone)
        energy_start_utc = str(combined.get("period_start_utc")) if combined else None
        energy_end_utc = str(combined.get("period_end_utc")) if combined else None
        snapshot_equipment_complete = bool(
            heat_pump_ids and heat_pump_ids.issubset(selected)
        )
        exact_cutoff = bool(
            selected_values
            and all(item.get("cutoff_certainty") == "exact" for item in selected_values)
        )
        cutoff_matches = bool(
            snapshot_equipment_complete and exact_cutoff and len(cutoff_values) == 1
            and energy_start_utc == month_start_utc.isoformat()
            and energy_end_utc == cutoff_values[0]
            and (combined.get("period_coverage") or {}).get("date_coverage", {}).get(
                "complete_through_available_cutoff"
            )
            and (combined.get("period_coverage") or {}).get("allocation_coverage", {}).get(
                "complete"
            )
        )
        assumed_value = float(assumed or 0)
        primary_quality = (
            "unavailable" if not cutoff_matches else
            ("provisional_assumed_heating" if assumed_value > 0 else "measured_exact_cutoff")
        )
        primary_heating_cop = _ratio(thermal_heating, shelly_heating) if cutoff_matches else None
        primary_dhw_cop = _ratio(thermal_dhw, shelly_dhw) if cutoff_matches else None
        primary_combined_cop = _ratio(
            _sum_complete((thermal_heating, thermal_dhw)),
            _sum_complete((shelly_heating, shelly_dhw)),
        ) if cutoff_matches else None
        vendor_heating_cop = _ratio(thermal_heating, vendor_heating_electricity)
        vendor_dhw_cop = _ratio(thermal_dhw, vendor_dhw_electricity)
        vendor_combined_cop = _ratio(
            vendor_combined_thermal if vendor_combined_thermal is not None
            else _sum_complete((thermal_heating, thermal_dhw)),
            vendor_combined_electricity if vendor_combined_electricity is not None
            else _sum_complete((vendor_heating_electricity, vendor_dhw_electricity)),
        )
        vendor_total = (
            vendor_combined_electricity if vendor_combined_electricity is not None
            else _sum_complete((vendor_heating_electricity, vendor_dhw_electricity))
        )
        vendor_category_sum = _sum_complete(
            (vendor_heating_electricity, vendor_dhw_electricity)
        )
        # This estimate uses only complete finalized daily meter evidence; mode
        # classification and temperature coverage are deliberately independent.
        day = month_start
        daily_meter_values = []
        daily_by_date = {str(item.get("local_date")): item for item in records}
        meter_complete = len(records) == len(daily_by_date) == (month_end - month_start).days + 1
        while meter_complete and day <= month_end:
            item = daily_by_date.get(day.isoformat())
            left, right = local_day_bounds(day, timezone)
            if (not item or item.get("temperature_only") or item.get("provisional")
                    or item.get("finalized") is False
                    or item.get("period_start_utc") != left.isoformat()
                    or item.get("period_end_utc") != right.isoformat()):
                meter_complete = False
                break
            for equipment_id in heat_pump_ids:
                allocation = item.get("allocations", {}).get(equipment_id, {})
                meter = allocation.get("meter_kwh")
                if (meter is None or not math.isfinite(float(meter)) or float(meter) < 0
                        or allocation.get("quality") in {"missing_counter", "unreconciled"}
                        or allocation.get("authoritative_counter_source") == "unavailable"
                        or allocation.get("rejected_count", 0)):
                    meter_complete = False
                    break
                daily_meter_values.append(float(meter))
            day += timedelta(days=1)
        _, month_end_utc = local_day_bounds(month_end, timezone)
        provisional_valid = bool(
            meter_complete and heat_pump_ids and set(selected) == heat_pump_ids and exact_cutoff
            and len(cutoff_values) == 1 and cutoff_values[0] == month_end_utc.isoformat()
            and all(item.get("status") == "final" for item in selected_values)
            and vendor_category_sum is not None and vendor_category_sum > 0
            and all(item.get("vendor_heating_electricity_kwh") is not None
                    and item.get("vendor_dhw_electricity_kwh") is not None for item in selected_values)
        )
        measured_month = sum(daily_meter_values) if provisional_valid else None
        estimated_heating = (measured_month * vendor_heating_electricity / vendor_category_sum
                             if provisional_valid else None)
        estimated_dhw = measured_month - estimated_heating if provisional_valid else None
        provisional_thermal = (
            vendor_combined_thermal if all(item.get("vendor_combined_thermal_kwh") is not None for item in selected_values)
            else generated_total if all(item.get("heating_thermal_kwh") is not None and item.get("dhw_thermal_kwh") is not None for item in selected_values)
            else None
        )
        vendor_rounding_mismatch = (
            float(vendor_combined_electricity) - float(vendor_category_sum)
            if vendor_combined_electricity is not None
            and vendor_category_sum is not None else None
        )
        shelly_total = shelly_ecodan_total
        difference = (
            float(shelly_total) - float(vendor_total)
            if cutoff_matches and vendor_total is not None and shelly_total is not None else None
        )
        def comparison(shelly, vendor):
            if not cutoff_matches or shelly is None or vendor is None:
                return None, None
            delta = float(shelly) - float(vendor)
            return delta, 100 * delta / float(vendor) if vendor else None

        heating_difference, heating_percent = comparison(shelly_heating, vendor_heating_electricity)
        dhw_difference, dhw_percent = comparison(shelly_dhw, vendor_dhw_electricity)
        device_metrics: dict[str, float | None] = {}
        for item in ac_rows:
            equipment_id = str(item.get("equipment_id"))
            device_metrics[f"equipment.{equipment_id}.heating_kwh"] = _allocation_subtotal(
                item, "known_confirmed_heating_kwh", "known_selected_heating_kwh",
                "known_estimated_heating_kwh",
            )
            device_metrics[f"equipment.{equipment_id}.cooling_kwh"] = _allocation_subtotal(
                item, "known_confirmed_cooling_kwh", "known_selected_cooling_kwh",
                "known_estimated_cooling_kwh",
            )
        row = {
            "month": month, "month_start": month_start.isoformat(),
            "month_end": month_end.isoformat(),
            "household_electricity_kwh": (
                energy.get("household_kwh")
                if energy.get("household_kwh") is not None
                else (combined or {}).get("known_household_kwh")
            ),
            "pv_production_kwh": (
                energy.get("pv_kwh") if energy.get("pv_kwh") is not None
                else (combined or {}).get("known_pv_kwh")
            ),
            "grid_import_kwh": (
                energy.get("import_kwh") if energy.get("import_kwh") is not None
                else (combined or {}).get("known_import_kwh")
            ),
            "grid_export_kwh": (
                energy.get("export_kwh") if energy.get("export_kwh") is not None
                else (combined or {}).get("known_export_kwh")
            ),
            "ecodan_total_measured_electricity_kwh": shelly_ecodan_total,
            "ecodan_confirmed_heating_electricity_kwh": confirmed,
            "ecodan_heating_electricity_kwh": shelly_heating,
            "ecodan_dhw_electricity_kwh": shelly_dhw,
            "ecodan_dhw_zero_proven": _dhw_zero_proven(records, equipment),
            "ecodan_idle_other_kwh": shelly_idle_other,
            "ecodan_assumed_heating_kwh": assumed,
            "ecodan_assumed_heating_percent": (
                100 * float(assumed) / float(shelly_heating)
                if assumed is not None and shelly_heating not in {None, 0} else None
            ),
            "ac_heating_electricity_kwh": ac_heating,
            "ac_cooling_electricity_kwh": ac_cooling,
            "known_ecodan_heating_electricity_kwh": _known_allocation_subtotal(
                records, equipment, "heat_pump", ("confirmed_heating", "heating", "heating_assumed")
            ),
            "known_ecodan_dhw_electricity_kwh": _known_allocation_subtotal(
                records, equipment, "heat_pump", ("dhw",)
            ),
            "known_ac_heating_electricity_kwh": _known_allocation_subtotal(
                records, equipment, "air_conditioner", ("heating", "heating_selected", "heating_estimated")
            ),
            "known_ac_cooling_electricity_kwh": _known_allocation_subtotal(
                records, equipment, "air_conditioner", ("cooling", "cooling_selected", "cooling_estimated")
            ),
            "whole_house_heating_electricity_kwh": whole_house_heating,
            "whole_house_heating_kwh_m2": (
                float(whole_house_heating) / float(floor_area_m2)
                if whole_house_heating is not None and floor_area_m2
                and float(floor_area_m2) > 0 else None
            ),
            "raw_unassigned_before_fallback_kwh": _sum_present(
                item.get("raw_unassigned_before_fallback_kwh")
                for item in energy.get("equipment", []) if item.get("kind") == "heat_pump"
            ),
            "remaining_unassigned_after_fallback_kwh": _sum_present(
                item.get("remaining_unassigned_after_fallback_kwh")
                for item in energy.get("equipment", []) if item.get("kind") == "heat_pump"
            ),
            "signed_reconciliation_error_kwh": _sum_present(
                item.get("signed_overallocation_or_reconciliation_error_kwh")
                for item in energy.get("equipment", []) if item.get("kind") == "heat_pump"
            ),
            "heating_thermal_kwh": thermal_heating, "dhw_thermal_kwh": thermal_dhw,
            "ecodan_total_generated_thermal_kwh": generated_total,
            "vendor_heating_electricity_kwh": vendor_heating_electricity,
            "vendor_dhw_electricity_kwh": vendor_dhw_electricity,
            "vendor_combined_electricity_kwh": vendor_total,
            "vendor_combined_thermal_kwh": vendor_combined_thermal,
            "vendor_category_electricity_sum_kwh": vendor_category_sum,
            "vendor_rounding_mismatch_kwh": vendor_rounding_mismatch,
            "primary_heating_cop": primary_heating_cop,
            "primary_dhw_cop": primary_dhw_cop,
            "primary_combined_cop": primary_combined_cop,
            "vendor_heating_cop": vendor_heating_cop,
            "vendor_dhw_cop": vendor_dhw_cop,
            "vendor_combined_cop": vendor_combined_cop,
            "provisional_shelly_heating_electricity_kwh": estimated_heating,
            "provisional_shelly_dhw_electricity_kwh": estimated_dhw,
            "provisional_shelly_heating_cop": _ratio(thermal_heating, estimated_heating) if all(item.get("heating_thermal_kwh") is not None for item in selected_values) else None,
            "provisional_shelly_dhw_cop": _ratio(thermal_dhw, estimated_dhw) if all(item.get("dhw_thermal_kwh") is not None for item in selected_values) else None,
            "provisional_shelly_combined_cop": _ratio(provisional_thermal, measured_month),
            "provisional_cop_quality": "provisional_controller_proportions" if provisional_valid else "unavailable",
            "shelly_minus_vendor_kwh": difference,
            "shelly_heating_minus_vendor_kwh": heating_difference,
            "shelly_heating_minus_vendor_percent": heating_percent,
            "shelly_dhw_minus_vendor_kwh": dhw_difference,
            "shelly_dhw_minus_vendor_percent": dhw_percent,
            "shelly_minus_vendor_percent": (
                100 * difference / vendor_total
                if difference is not None and vendor_total not in (None, 0) else None
            ),
            "quality": (
                "missing" if not records and not selected_values else
                ("partial" if not cutoff_matches or energy.get("quality") != "good" else "good")
            ),
            "primary_cop_quality": primary_quality,
            "cutoff_match": cutoff_matches,
            "energy_period_start_utc": energy_start_utc,
            "energy_period_end_utc": energy_end_utc,
            "snapshot_cutoff_utc": cutoff_values[0] if len(cutoff_values) == 1 else None,
            "snapshot_cutoff_consistent": len(cutoff_values) <= 1,
            "snapshot_equipment_complete": snapshot_equipment_complete,
            "snapshot_status": (
                "final" if selected_values and all(item.get("status") == "final" for item in selected_values)
                else ("partial" if selected_values else "missing")
            ),
            "snapshot_count": len(selected_values),
            "configured_heat_pump_count": len(heat_pump_ids),
            "snapshot_equipment_ids": sorted(selected),
            "missing_snapshot_equipment_ids": sorted(heat_pump_ids - set(selected)),
            "available_daily_dates": energy_dates,
            "period_coverage": (combined or {}).get("period_coverage"),
            "source": {
                "electricity": "shelly_classified_daily_records",
                "thermal": "monthly_ecodan_operator_snapshot",
                "vendor": "monthly_ecodan_operator_snapshot_secondary",
            },
            **device_metrics,
        }
        rows.append(row)
    return {
        "months": months,
        "rows": rows,
        "default_metric_ids": [
            "ecodan_total_measured_electricity_kwh",
            "ecodan_total_generated_thermal_kwh", "primary_combined_cop",
            "ac_heating_electricity_kwh", "ac_cooling_electricity_kwh",
        ],
        "metric_catalog": monthly_metric_catalog(equipment),
        "timezone": timezone,
        "count": count,
    }


def monthly_metric_catalog(
    equipment: Iterable[dict[str, Any]] = (),
) -> list[dict[str, str]]:
    """Stable metric metadata shared by the chart, table, and CSV export."""
    energy = {
        "household_electricity_kwh": "Whole-house electricity consumed",
        "pv_production_kwh": "PV production",
        "grid_import_kwh": "Grid import",
        "grid_export_kwh": "Grid export",
        "ecodan_total_measured_electricity_kwh": "Shelly Ecodan total electricity",
        "ecodan_confirmed_heating_electricity_kwh": "Confirmed Ecodan heating electricity",
        "ecodan_heating_electricity_kwh": "Shelly Ecodan heating electricity",
        "ecodan_dhw_electricity_kwh": "Shelly Ecodan DHW electricity",
        "ecodan_idle_other_kwh": "Shelly Ecodan idle/other electricity",
        "ecodan_assumed_heating_kwh": "Assumed Ecodan heating electricity",
        "raw_unassigned_before_fallback_kwh": "Raw unassigned before fallback",
        "remaining_unassigned_after_fallback_kwh": "Remaining unassigned after fallback",
        "signed_reconciliation_error_kwh": "Signed reconciliation error",
        "ac_heating_electricity_kwh": "Total AC heating electricity",
        "ac_cooling_electricity_kwh": "Total AC cooling electricity",
        "whole_house_heating_electricity_kwh": "Whole-house heating electricity",
        "heating_thermal_kwh": "Heating thermal energy",
        "dhw_thermal_kwh": "DHW thermal energy",
        "ecodan_total_generated_thermal_kwh": "Total Ecodan generated thermal energy",
        "vendor_heating_electricity_kwh": "Vendor heating electricity",
        "vendor_dhw_electricity_kwh": "Vendor DHW electricity",
        "vendor_combined_electricity_kwh": "Vendor combined electricity",
        "vendor_category_electricity_sum_kwh": "Vendor heating + DHW electricity",
        "vendor_rounding_mismatch_kwh": "Vendor displayed total rounding mismatch",
        "vendor_combined_thermal_kwh": "Vendor combined thermal energy",
        "shelly_minus_vendor_kwh": "Shelly minus Ecodan electricity",
        "provisional_shelly_heating_electricity_kwh": "Provisional Shelly heating electricity",
        "provisional_shelly_dhw_electricity_kwh": "Provisional Shelly DHW electricity",
        "shelly_heating_minus_vendor_kwh": "Shelly minus Ecodan heating electricity",
        "shelly_dhw_minus_vendor_kwh": "Shelly minus Ecodan DHW electricity",
    }
    catalog = [
        {
            "id": key, "label": label, "unit": "kWh", "axis": "energy",
            "group": (
                "house_pv" if key in {
                    "household_electricity_kwh", "pv_production_kwh",
                    "grid_import_kwh", "grid_export_kwh",
                } else
                ("ac_house" if key in {
                    "ac_heating_electricity_kwh", "ac_cooling_electricity_kwh",
                    "whole_house_heating_electricity_kwh",
                } else
                 ("thermal" if "thermal" in key else
                  ("vendor" if key.startswith("vendor_")
                   or key.startswith("shelly_minus_vendor") else "ecodan_shelly")))
            ),
        }
        for key, label in energy.items()
    ]
    catalog.append({
        "id": "whole_house_heating_kwh_m2",
        "label": "Whole-house heating electricity per floor area",
        "unit": "kWh/m²", "axis": "normalized", "group": "ac_house",
    })
    for item in equipment:
        if not item.get("enabled", True) or item.get("kind") != "air_conditioner":
            continue
        equipment_id = str(item.get("equipment_id"))
        display_name = str(item.get("display_name") or equipment_id)
        catalog.extend((
            {"id": f"equipment.{equipment_id}.heating_kwh",
             "label": f"{display_name} heating electricity", "unit": "kWh",
             "axis": "energy", "group": "ac_house"},
            {"id": f"equipment.{equipment_id}.cooling_kwh",
             "label": f"{display_name} cooling electricity", "unit": "kWh",
             "axis": "energy", "group": "ac_house"},
        ))
    catalog.extend(
        {
            "id": key, "label": label, "unit": "COP", "axis": "cop",
            "group": "vendor" if key.startswith("vendor_") else "primary_cop",
        }
        for key, label in {
            "primary_heating_cop": "Primary heating COP",
            "primary_dhw_cop": "Primary DHW COP",
            "primary_combined_cop": "Primary combined COP",
            "vendor_heating_cop": "Vendor heating COP",
            "vendor_dhw_cop": "Vendor DHW COP",
            "vendor_combined_cop": "Vendor combined COP",
            "provisional_shelly_heating_cop": "PROVISIONAL Shelly Heating COP",
            "provisional_shelly_dhw_cop": "PROVISIONAL Shelly DHW COP",
            "provisional_shelly_combined_cop": "PROVISIONAL Shelly Combined COP",
        }.items()
    )
    catalog.extend((
        {"id": "shelly_heating_minus_vendor_percent", "label": "Shelly minus Ecodan heating", "unit": "%", "axis": "percent", "group": "vendor"},
        {"id": "shelly_dhw_minus_vendor_percent", "label": "Shelly minus Ecodan DHW", "unit": "%", "axis": "percent", "group": "vendor"},
        {
            "id": "shelly_minus_vendor_percent", "label": "Shelly minus Ecodan",
            "unit": "%", "axis": "percent", "group": "vendor",
        },
        {
            "id": "ecodan_assumed_heating_percent",
            "label": "Assumed share of Ecodan heating electricity",
            "unit": "%", "axis": "percent", "group": "ecodan_shelly",
        },
    ))
    return catalog

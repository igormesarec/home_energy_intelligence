"""Prepare, but do not write, strict historical preview evidence."""

from __future__ import annotations

from datetime import date, datetime
from math import isfinite
from typing import Any

from .timeutils import local_day_bounds


def recovered_heating_points(evidence, occupied, daily_records, equipment, outdoor_source, timezone, minimum_coverage):
    """Join independently qualified source-days; never merge them into canonical totals."""
    from math import isfinite

    points, excluded = [], list(evidence.get("ambiguous", []))
    devices = {f"equipment.{item.equipment_id}": item for item in equipment if item.enabled}
    temperatures = {record.get("local_date"): record for record in daily_records}
    occupied = set(occupied)
    for item in evidence.get("records", []):
        key, day = item.get("source_key"), item.get("local_date")
        device = devices.get(key)
        if not device:
            continue
        reason = None
        try:
            local_date = date.fromisoformat(day)
            start, end = local_day_bounds(local_date, timezone)
            if not device.commissioning_date:
                reason = "commissioning_date_unknown"
            elif local_date < date.fromisoformat(device.commissioning_date):
                reason = "before_commissioning"
            mappings = [m for m in device.historical_energy_mappings if date.fromisoformat(m["start_date"]) <= local_date <= (date.fromisoformat(m["end_date"]) if "end_date" in m else date.max)]
            if not reason and (len(mappings) != 1 or mappings[0]["entity_id"] != item.get("source_entity_id")):
                reason = "historical_mapping_unverified_or_mismatched"
            if not reason and ((day, key) in occupied or (day, "*") in occupied):
                reason = "overlapping_authoritative_source_day"
            if not reason and (datetime.fromisoformat(item["interval_start_utc"]) != start or datetime.fromisoformat(item["interval_end_utc"]) != end):
                reason = "electricity_interval_mismatch"
            categories = item.get("categories") or {}
            names = ("confirmed_heating", "heating_assumed") if device.kind == "heat_pump" else ("heating_selected",)
            values = {name: categories[name] for name in names if name in categories}
            if not reason and not values:
                reason = "heating_classification_unavailable"
            if not reason and not sum(values.values()) and (categories.get("unknown", 0) or item.get("residual_kwh")):
                reason = "heating_classification_unavailable"
            temp = (temperatures.get(day, {}).get("temperatures") or {}).get(outdoor_source.source_id, {}) if outdoor_source else {}
            provenance = temp.get("source_provenance") or {}
            mean, coverage = temp.get("mean"), temp.get("coverage_percent")
            if not reason and (not outdoor_source or outdoor_source.role != "outdoor" or provenance.get("role") != "outdoor" or provenance.get("entity_id") != outdoor_source.entity_id or "attribute" not in provenance or provenance.get("attribute") != outdoor_source.attribute):
                reason = "outdoor_source_identity_unverified"
            if not reason:
                effective = provenance.get("mapping_effective_at")
                effective_end = provenance.get("mapping_effective_end")
                if not effective or datetime.fromisoformat(effective.replace("Z", "+00:00")) > start or (effective_end and datetime.fromisoformat(effective_end.replace("Z", "+00:00")) < end):
                    reason = "outdoor_mapping_interval_unverified"
            if not reason and (temp.get("period_start_utc") != start.isoformat() or temp.get("period_end_utc") != end.isoformat()):
                reason = "outdoor_interval_unverified"
            if not reason and (not isinstance(mean, (int, float)) or not isfinite(mean) or mean in (-55, 155) or temp.get("quality") not in ("good", "filtered", "partial") or coverage is None or coverage < minimum_coverage):
                reason = "outdoor_temperature_invalid_or_incomplete"
            if reason:
                detail = reason
                if reason == "historical_mapping_unverified_or_mismatched":
                    detail = "missing_mapping" if not mappings else "ambiguous_mapping" if len(mappings) > 1 else "mismatched_entity_id"
                elif reason == "heating_classification_unavailable" and item.get("residual_kwh"):
                    detail = "source_reconciliation_unresolved"
                excluded.append({"local_date": day, "source_key": key, "reason": reason,
                                 "diagnostic_reason": detail, "meter_kwh": item.get("meter_kwh"),
                                 "source_entity_id": item.get("source_entity_id"),
                                 "mapping_entity_ids": [m.get("entity_id") for m in mappings],
                                 "outdoor_coverage_percent": coverage,
                                 "recovered_meter_coverage_seconds": item.get("meter_coverage_seconds"),
                                 "blocked_source_day_seconds": (end-start).total_seconds() if reason == "overlapping_authoritative_source_day" else None})
                continue
            points.append({"local_date": day, "equipment_id": device.equipment_id, "display_name": device.display_name,
                           "source_entity_id": item["source_entity_id"], "mapping_entity_ids": [mappings[0]["entity_id"]], "meter_kwh": item.get("meter_kwh"), "outdoor_mean_c": mean,
                           "outdoor_coverage_percent": coverage, "heating_kwh": sum(values.values()),
                           "categories": values, "meter_coverage_seconds": item.get("meter_coverage_seconds"),
                           "classification_coverage_seconds": item.get("classification_coverage_seconds"),
                           "day_seconds": (end-start).total_seconds(), "quality": "provisional; historical source mapping confirmed"})
        except (TypeError, ValueError, KeyError, AttributeError):
            excluded.append({"local_date": day, "source_key": key, "reason": "invalid_evidence"})
    # Source-day observations only: do not fill absent or overlapping devices
    # from canonical aggregates whose precise intervals may be unknown.
    required = {item.equipment_id: item.display_name for item in equipment
                if item.enabled and item.kind in ("heat_pump", "air_conditioner")}
    grouped = {}
    for point in points:
        grouped.setdefault(point["local_date"], []).append(point)
    whole_house = []
    for day, components in sorted(grouped.items()):
        present = {item["equipment_id"] for item in components}
        missing = [{"equipment_id": key, "display_name": name,
                    "reason": next((entry.get("reason") or "conflicting_evidence" for entry in excluded
                                    if entry.get("local_date") == day and entry.get("source_key") == f"equipment.{key}"), "source_evidence_unavailable")}
                   for key, name in required.items() if key not in present]
        whole_house.append({"local_date": day, "outdoor_mean_c": components[0]["outdoor_mean_c"],
                            "outdoor_coverage_percent": components[0]["outdoor_coverage_percent"],
                            "known_heating_kwh": sum(item["heating_kwh"] for item in components),
                            "components": components, "missing_sources": missing,
                            "expected_equipment_count": len(required), "quality": "provisional partial known subtotal"})
    return {"points": points, "excluded": excluded, "whole_house": whole_house}


def prepare_recovery_day(preview: dict[str, Any], timezone: str) -> dict[str, Any]:
    """Build provisional day evidence without modifying the preview or database."""
    day = date.fromisoformat(preview["local_date"])
    start, end = local_day_bounds(day, timezone)
    records, skipped = [], []
    common = {"local_date": day.isoformat(), "timezone": timezone,
              "interval_start_utc": start.isoformat(), "interval_end_utc": end.isoformat(),
              "verification_status": "provisional", "calculation_version": "historical-preview-v1",
              "historical_mapping_quality": "unverified historical mapping",
              "preview_mapping_context": preview.get("historical_mapping_quality", "unverified")}
    for device, allocation in preview.get("allocations", {}).items():
        identity = allocation.get("source_provenance", {}).get("energy_entity_id")
        meter = allocation.get("meter_kwh")
        if not identity or not isinstance(meter, (int, float)) or not isfinite(meter) or meter < 0:
            skipped.append({"source_key": f"equipment.{device}", "reason": "missing_identity_or_meter"})
            continue
        records.append({**common, "source_key": f"equipment.{device}", "source_entity_id": identity,
                        "meter_kwh": meter, "categories": allocation.get("categories", {}),
                        "residual_kwh": allocation.get("residual_kwh"),
                        "meter_coverage_seconds": allocation.get("source_provenance", {}).get("meter_coverage_seconds"),
                        "classification_coverage_seconds": allocation.get("classification_coverage_seconds"),
                        "source_provenance": allocation.get("source_provenance", {}),
                        "counter_source": allocation.get("authoritative_counter_source"),
                        "quality": allocation.get("quality"), "reset_count": allocation.get("reset_count"),
                        "rejected_count": allocation.get("rejected_count"),
                        "errors": [item for item in preview.get("errors", []) if item.get("source", "").startswith(f"equipment.{device}.")]})
    for key, site in preview.get("site", {}).items():
        identity, meter = site.get("entity_id"), site.get("meter_kwh")
        if not identity or not isinstance(meter, (int, float)) or not isfinite(meter) or meter < 0:
            skipped.append({"source_key": f"site.{key}", "reason": "missing_identity_or_meter"})
            continue
        records.append({**common, "source_key": f"site.{key}", "source_entity_id": identity,
                        "meter_kwh": meter, "categories": {}, "residual_kwh": None,
                        "source_provenance": {"energy_entity_id": identity, "strict_influx_identity": bool(preview.get("evidence_only"))},
                        "meter_coverage_seconds": site.get("covered_seconds"), "quality": site.get("status"),
                        "counter_source": site.get("source"), "rejected_count": site.get("rejected_count"),
                        "reset_count": site.get("reset_count")})
    return {"local_date": day.isoformat(), "records": records, "skipped": skipped}


def summarize_provisional_evidence(evidence: dict[str, Any], excluded_dates: set,
                                   source_entities: dict[str, str]) -> dict[str, Any]:
    """Summarize separate provisional evidence without merging it into measured totals."""
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    references: dict[tuple[str, str], dict[str, Any]] = {}
    skipped = list(evidence.get("ambiguous", []))
    for record in evidence.get("records", []):
        day, key = record.get("local_date", ""), record.get("source_key", "")
        if not source_entities.get(key) or source_entities[key] != record.get("source_entity_id"):
            skipped.append({"local_date": day, "source_key": key, "reason": "current_source_identity_mismatch"})
            continue
        overlap = day in excluded_dates or (day, key) in excluded_dates or (day, "*") in excluded_dates
        if overlap:
            skipped.append({"local_date": day, "source_key": key, "reason": "existing_electrical_record"})
        group = (references if overlap else groups).setdefault((day[:7], key), {"month": day[:7], "source_key": key,
            "source_entity_id": record["source_entity_id"], "meter_kwh": 0.0, "categories": {}, "residual_kwh": None, "days": 0,
            "coverage_seconds": None, "expected_seconds": 0.0, "coverage_unknown_days": 0, "sources": [],
            "quality": ("overlapping historical reference; non-additive; historical mapping unverified" if overlap else "provisional partial subtotal; historical mapping unverified")})
        group["meter_kwh"] += record["meter_kwh"]
        group["days"] += 1
        covered = record.get("meter_coverage_seconds")
        if isinstance(covered, (int, float)):
            group["coverage_seconds"] = (group["coverage_seconds"] or 0.0) + covered
        else:
            group["coverage_unknown_days"] += 1
        group["expected_seconds"] += (datetime.fromisoformat(record["interval_end_utc"]) - datetime.fromisoformat(record["interval_start_utc"])).total_seconds()
        source = record.get("counter_source")
        if source and source not in group["sources"]:
            group["sources"].append(source)
        if record.get("residual_kwh") is not None:
            group["residual_kwh"] = (group["residual_kwh"] or 0.0) + record["residual_kwh"]
        for category, value in record.get("categories", {}).items():
            group["categories"][category] = group["categories"].get(category, 0.0) + value
    return {"rows": list(groups.values()), "reference_rows": list(references.values()), "skipped": skipped}


RECOVERED_METRICS = {
    "recovered_ecodan_meter_kwh": ("heat_pump", None),
    "recovered_ecodan_confirmed_heating_kwh": ("heat_pump", "confirmed_heating"),
    "recovered_ecodan_assumed_heating_kwh": ("heat_pump", "heating_assumed"),
    "recovered_ecodan_dhw_kwh": ("heat_pump", "dhw"),
    "recovered_ecodan_unknown_kwh": ("heat_pump", "unknown"),
    "recovered_ac_selected_heating_kwh": ("air_conditioner", "heating_selected"),
    "recovered_ac_selected_cooling_kwh": ("air_conditioner", "cooling_selected"),
    "recovered_ac_meter_kwh": ("air_conditioner", None),
    "recovered_ac_unknown_kwh": ("air_conditioner", "unknown"),
    "recovered_pv_kwh": ("site.pv", None),
    "recovered_import_kwh": ("site.import", None),
    "recovered_export_kwh": ("site.export", None),
}


def add_recovered_presentation_metrics(report: dict[str, Any], summary: dict[str, Any], equipment: list[dict[str, Any]]) -> None:
    """Attach separate known subtotals and coverage, without changing canonical totals."""
    kinds = {f"equipment.{item['equipment_id']}": item.get("kind") for item in equipment if item.get("enabled", True)}
    kinds.update({key: key for key in ("site.pv", "site.import", "site.export")})
    # A generic heat-pump mapping cannot identify Ecodan when multiple heat pumps exist.
    unique_heat_pump = sum(kind == "heat_pump" for kind in kinds.values()) == 1
    rows = {row["month"]: row for row in report.get("rows", [])}
    for is_reference, item in [(False, item) for item in summary.get("rows", [])] + [(True, item) for item in summary.get("reference_rows", [])]:
        row = rows.get(item.get("month"))
        if row is None:
            continue
        for metric, (kind, category) in RECOVERED_METRICS.items():
            if kinds.get(item.get("source_key")) != kind or (kind == "heat_pump" and not unique_heat_pump):
                continue
            value = item.get("meter_kwh") if category is None else item.get("categories", {}).get(category)
            if value is None:
                continue
            if is_reference:
                metric = metric.replace("recovered_", "recovered_reference_", 1)
            row[metric] = (row.get(metric) or 0.0) + value
            row.setdefault("recovered_metric_evidence", {}).setdefault(metric, []).append({
                "source_key": item["source_key"], "source_entity_id": item["source_entity_id"],
                "sources": item.get("sources", []), "coverage_seconds": item.get("coverage_seconds"),
                "expected_seconds": item.get("expected_seconds"), "days": item.get("days"),
                "coverage_unknown_days": item.get("coverage_unknown_days", 0),
                "recovery_kind": "overlapping_reference" if is_reference else "non_overlapping_provisional",
                "quality": item.get("quality")})
    for row in rows.values():
        row["recovered_excluded"] = [item for item in summary.get("skipped", []) if str(item.get("local_date", "")).startswith(row["month"])]

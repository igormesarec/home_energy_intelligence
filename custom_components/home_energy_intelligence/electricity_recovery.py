"""Prepare, but do not write, strict historical preview evidence."""

from __future__ import annotations

from datetime import date, datetime
from math import isfinite
from typing import Any

from .timeutils import local_day_bounds


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
    skipped = list(evidence.get("ambiguous", []))
    for record in evidence.get("records", []):
        day, key = record.get("local_date", ""), record.get("source_key", "")
        if day in excluded_dates or (day, key) in excluded_dates or (day, "*") in excluded_dates:
            skipped.append({"local_date": day, "source_key": key, "reason": "existing_electrical_record"})
            continue
        if not source_entities.get(key) or source_entities[key] != record.get("source_entity_id"):
            skipped.append({"local_date": day, "source_key": key, "reason": "current_source_identity_mismatch"})
            continue
        group = groups.setdefault((day[:7], key), {"month": day[:7], "source_key": key,
            "source_entity_id": record["source_entity_id"], "meter_kwh": 0.0, "categories": {}, "residual_kwh": None, "days": 0,
            "coverage_seconds": None, "expected_seconds": 0.0, "coverage_unknown_days": 0, "sources": [],
            "quality": "provisional partial subtotal; historical mapping unverified"})
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
    return {"rows": list(groups.values()), "skipped": skipped}


RECOVERED_METRICS = {
    "recovered_ecodan_meter_kwh": ("heat_pump", None),
    "recovered_ecodan_assumed_heating_kwh": ("heat_pump", "heating_assumed"),
    "recovered_ecodan_dhw_kwh": ("heat_pump", "dhw"),
    "recovered_ecodan_unknown_kwh": ("heat_pump", "unknown"),
    "recovered_ac_selected_heating_kwh": ("air_conditioner", "heating_selected"),
    "recovered_ac_selected_cooling_kwh": ("air_conditioner", "cooling_selected"),
    "recovered_ac_meter_kwh": ("air_conditioner", None),
    "recovered_ac_unknown_kwh": ("air_conditioner", "unknown"),
}


def add_recovered_presentation_metrics(report: dict[str, Any], summary: dict[str, Any], equipment: list[dict[str, Any]]) -> None:
    """Attach separate known subtotals and coverage, without changing canonical totals."""
    kinds = {f"equipment.{item['equipment_id']}": item.get("kind") for item in equipment if item.get("enabled", True)}
    # A generic heat-pump mapping cannot identify Ecodan when multiple heat pumps exist.
    unique_heat_pump = sum(kind == "heat_pump" for kind in kinds.values()) == 1
    rows = {row["month"]: row for row in report.get("rows", [])}
    for item in summary.get("rows", []):
        row = rows.get(item.get("month"))
        if row is None:
            continue
        for metric, (kind, category) in RECOVERED_METRICS.items():
            if kinds.get(item.get("source_key")) != kind or (kind == "heat_pump" and not unique_heat_pump):
                continue
            value = item.get("meter_kwh") if category is None else item.get("categories", {}).get(category)
            if value is None:
                continue
            row[metric] = (row.get(metric) or 0.0) + value
            row.setdefault("recovered_metric_evidence", {}).setdefault(metric, []).append({
                "source_key": item["source_key"], "source_entity_id": item["source_entity_id"],
                "sources": item.get("sources", []), "coverage_seconds": item.get("coverage_seconds"),
                "expected_seconds": item.get("expected_seconds"), "days": item.get("days"),
                "coverage_unknown_days": item.get("coverage_unknown_days", 0),
                "quality": item.get("quality")})
    for row in rows.values():
        row["recovered_excluded"] = [item for item in summary.get("skipped", []) if str(item.get("local_date", "")).startswith(row["month"])]

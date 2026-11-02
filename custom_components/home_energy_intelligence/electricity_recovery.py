"""Prepare, but do not write, strict historical preview evidence."""

from __future__ import annotations

from datetime import date
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
              "historical_mapping_quality": preview.get("historical_mapping_quality", "unverified")}
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
                        "meter_coverage_seconds": site.get("covered_seconds"), "quality": site.get("status"),
                        "counter_source": site.get("source"), "rejected_count": site.get("rejected_count"),
                        "reset_count": site.get("reset_count")})
    return {"local_date": day.isoformat(), "records": records, "skipped": skipped}

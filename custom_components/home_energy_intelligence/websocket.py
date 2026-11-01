"""Small read-only WebSocket API for the dashboard."""

from __future__ import annotations

import csv
import io
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import voluptuous as vol
from homeassistant.components import websocket_api

from .analytics import (
    attach_canonical,
    canonical_energy,
    combine_daily_records,
    combine_interval_records,
    daily_coverage,
    derive_record_quality,
    heating_response_profile,
    interval_coverage,
    prefer_authoritative_daily,
    provisional_completed_day_from_hourly,
    replace_with_partial_day,
)
from .configuration import (
    equipment_from_config,
    equipment_temperature_mappings,
    temperatures_from_config,
)
from .const import DOMAIN
from .monthly import (
    build_rolling_month_report,
    monthly_metric_catalog,
    normalize_monthly_snapshot,
    rolling_months,
)
from .periods import period_bounds, previous_day_same_local_cutoff
from .temperature_timeline import (
    build_timeline_intervals,
    hourly_axis_ticks,
    validate_timeline_dates,
)
from .thermal import (
    cumulative_intervals,
    normalize_manual_reading,
    ratio_of_sums,
    thermal_performance_points,
)
from .timeutils import local_day_bounds


def async_register_websocket(hass) -> None:
    websocket_api.async_register_command(hass, ws_overview)
    websocket_api.async_register_command(hass, ws_history)
    websocket_api.async_register_command(hass, ws_settings)
    websocket_api.async_register_command(hass, ws_refresh)
    websocket_api.async_register_command(hass, ws_csv)
    websocket_api.async_register_command(hass, ws_temperature_diagnostics)
    websocket_api.async_register_command(hass, ws_reprocess_temperature_history)
    websocket_api.async_register_command(hass, ws_temperature_backfill_preview)
    websocket_api.async_register_command(hass, ws_temperature_backfill_start)
    websocket_api.async_register_command(hass, ws_temperature_backfill_status)
    websocket_api.async_register_command(hass, ws_temperature_backfill_cancel)
    websocket_api.async_register_command(hass, ws_temperature_timeline)
    websocket_api.async_register_command(hass, ws_v02_report)
    websocket_api.async_register_command(hass, ws_thermal_readings)
    websocket_api.async_register_command(hass, ws_save_thermal_reading)
    websocket_api.async_register_command(hass, ws_delete_thermal_reading)
    websocket_api.async_register_command(hass, ws_backfill_thermal_readings)
    websocket_api.async_register_command(hass, ws_monthly_report)
    websocket_api.async_register_command(hass, ws_monthly_report_csv)
    websocket_api.async_register_command(hass, ws_monthly_ecodan_snapshots)
    websocket_api.async_register_command(hass, ws_save_monthly_ecodan_snapshot)
    websocket_api.async_register_command(hass, ws_delete_monthly_ecodan_snapshot)


def _coordinator(hass):
    entries = hass.config_entries.async_entries(DOMAIN)
    if not entries:
        return None
    return hass.data.get(DOMAIN, {}).get(entries[0].entry_id)


def _equipment(coordinator):
    return [
        {"equipment_id": item.equipment_id, "display_name": item.display_name,
         "kind": item.kind, "enabled": item.enabled}
        for item in equipment_from_config(coordinator.config)
    ]


def _temperatures(coordinator):
    explicit = temperatures_from_config(coordinator.config)
    seen = {(item.entity_id, item.role) for item in explicit}
    generated = [
        item for item in equipment_temperature_mappings(equipment_from_config(coordinator.config))
        if (item.entity_id, item.role) not in seen
    ]
    return explicit + generated


def _representative_temperature_source_ids(coordinator) -> list[str]:
    """Return only enabled, role-compatible sources that affect overall quality."""
    configured = {
        "indoor": coordinator.config.get("representative_indoor_source_id"),
        "outdoor": coordinator.config.get("representative_outdoor_source_id"),
    }
    sources = {item.source_id: item for item in _temperatures(coordinator)}
    return [
        source_id for role, source_id in configured.items()
        if source_id and source_id in sources and sources[source_id].enabled
        and sources[source_id].role == role
    ]


def _response_temperature_coverage_minimum(coordinator) -> float:
    return float(coordinator.config.get("response_temperature_coverage_minimum", 95))


def _local_calendar_dates(timezone: str, now: datetime | None = None):
    """Return HA-local Today and the immediately completed Yesterday."""
    today = (now or datetime.now(UTC)).astimezone(ZoneInfo(timezone)).date()
    return today, today - timedelta(days=1)


@websocket_api.websocket_command({
    vol.Required("type"): f"{DOMAIN}/temperature_timeline",
    vol.Required("start_date"): str,
    vol.Required("end_date"): str,
    vol.Required("resolution"): vol.In({"hourly", "daily"}),
    vol.Optional("analysis", default=False): bool,
    vol.Required("source_ids"): vol.All([str], vol.Length(min=1, max=8)),
})
@websocket_api.async_response
async def ws_temperature_timeline(hass, connection, msg):
    """Return bounded, persisted and explicitly gapped temperature history."""
    coordinator = _coordinator(hass)
    if coordinator is None:
        connection.send_error(msg["id"], "not_configured", "Integration is not configured")
        return
    resolution = msg["resolution"]
    try:
        if resolution == "hourly" and msg.get("analysis"):
            start_date, end_date = validate_timeline_dates(msg["start_date"], msg["end_date"], maximum_days=366)
        else:
            start_date, end_date = validate_timeline_dates(
                msg["start_date"], msg["end_date"],
                maximum_days=7 if resolution == "hourly" else 3660,
            )
    except ValueError as error:
        connection.send_error(msg["id"], "invalid_range", str(error))
        return
    configured = {
        item.source_id: item for item in _temperatures(coordinator) if item.enabled
    }
    requested_ids = list(dict.fromkeys(msg["source_ids"]))
    invalid_ids = [source_id for source_id in requested_ids if source_id not in configured]
    if invalid_ids:
        connection.send_error(
            msg["id"], "invalid_source",
            f"Sources are not configured and enabled: {', '.join(invalid_ids)}",
        )
        return

    timezone = coordinator.timezone
    start_utc, _ = local_day_bounds(start_date, timezone)
    _, requested_end_utc = local_day_bounds(end_date, timezone)
    now = datetime.now(UTC)
    completed_hour = now.replace(minute=0, second=0, microsecond=0)
    end_utc = min(requested_end_utc, completed_hour) if end_date >= now.astimezone(
        ZoneInfo(timezone)
    ).date() else requested_end_utc
    if end_utc < start_utc:
        end_utc = start_utc

    if resolution == "hourly":
        stored = await hass.async_add_executor_job(
            coordinator.database.hourly_range, start_utc, end_utc
        )
    else:
        stored = await hass.async_add_executor_job(
            coordinator.database.history_period,
            start_date.isoformat(), end_date.isoformat(),
        )
    intervals, available = build_timeline_intervals(
        resolution=resolution, start_date=start_date, end_date=end_date,
        timezone=timezone, range_end_utc=end_utc, stored=stored,
        source_ids=requested_ids,
    )
    connection.send_result(msg["id"], {
        "resolution": resolution, "timezone": timezone,
        "start_date": start_date.isoformat(), "end_date": end_date.isoformat(),
        "range_start_utc": start_utc.isoformat(), "range_end_utc": end_utc.isoformat(),
        "requested_range_end_utc": requested_end_utc.isoformat(),
        "partial": end_utc < requested_end_utc,
        "source_ids": requested_ids,
        "sources": [{"source_id": source_id,
                     "display_name": configured[source_id].display_name,
                     "entity_id": configured[source_id].entity_id,
                     "role": configured[source_id].role}
                    for source_id in requested_ids],
        "intervals": intervals, "expected_interval_count": len(intervals),
        "available_interval_count": available,
        "axis_ticks": (hourly_axis_ticks(start_utc, end_utc, timezone)
                       if resolution == "hourly" else []),
    })


def _v02_daily_curves(records, equipment, outdoor_source_id, floor_area_m2):
    points = []
    for record in records:
        energy = canonical_energy(record, equipment)
        temperature = (record.get("temperatures") or {}).get(outdoor_source_id, {})
        devices = energy.get("equipment") or []
        heat_pumps = []
        air_conditioners = []
        cooling_units = []
        for item in devices:
            allocation = (record.get("allocations") or {}).get(str(item.get("equipment_id"))) or {}
            categories = allocation.get("categories") or {}
            available = bool(item.get("available"))
            resolved = bool(item.get("classification_complete")) and not float(categories.get("unknown") or 0) and abs(float(allocation.get("residual_kwh") or 0)) < 0.000001
            if item.get("kind") == "heat_pump":
                value = sum(float(item.get(key) or 0) for key in (
                    "known_confirmed_heating_kwh", "known_assumed_heating_kwh",
                ))
                heat_pumps.append({"equipment_id": item.get("equipment_id"),
                                   "display_name": item.get("display_name"),
                                   "electricity_kwh": value if available and (resolved or value > 0 or any(key in categories for key in ("heating", "confirmed_heating", "heating_assumed"))) else None,
                                   "quality": item.get("quality"), "coverage_percent": item.get("coverage_percent"),
                                   "classification_complete": item.get("classification_complete"),
                                   "confirmed_kwh": item.get("known_confirmed_heating_kwh"),
                                   "assumed_kwh": item.get("known_assumed_heating_kwh")})
            elif item.get("kind") == "air_conditioner":
                heating = sum(float(item.get(key) or 0) for key in (
                    "known_confirmed_heating_kwh", "known_selected_heating_kwh",
                    "known_estimated_heating_kwh",
                ))
                cooling = sum(float(item.get(key) or 0) for key in (
                    "known_confirmed_cooling_kwh", "known_selected_cooling_kwh",
                    "known_estimated_cooling_kwh",
                ))
                air_conditioners.append({"equipment_id": item.get("equipment_id"),
                                         "display_name": item.get("display_name"),
                                         "electricity_kwh": heating if available and (resolved or heating > 0 or any(key in categories for key in ("heating", "heating_selected", "heating_estimated"))) else None,
                                         "quality": item.get("quality"), "coverage_percent": item.get("coverage_percent"),
                                         "classification_complete": item.get("classification_complete")})
                cooling_units.append({"equipment_id": item.get("equipment_id"),
                                      "display_name": item.get("display_name"),
                                      "electricity_kwh": cooling if available and (resolved or cooling > 0 or any(key in categories for key in ("cooling", "cooling_selected", "cooling_estimated"))) else None,
                                      "quality": item.get("quality"), "coverage_percent": item.get("coverage_percent"),
                                      "classification_complete": resolved})
        heating_values = [item["electricity_kwh"] for item in heat_pumps + air_conditioners if item["electricity_kwh"] is not None]
        cooling_values = [item["electricity_kwh"] for item in cooling_units if item["electricity_kwh"] is not None]
        whole = sum(heating_values) if heating_values else None
        cooling = sum(cooling_values) if cooling_values else None
        finalized = energy.get("calendar_period_finalized") is True
        complete = finalized and bool(devices) and all(bool(item.get("classification_complete")) for item in devices) and len(heating_values) == len(devices) and not float(energy.get("unknown_kwh") or 0)
        cooling_complete = finalized and bool(cooling_units) and all(bool(item.get("classification_complete")) for item in cooling_units) and len(cooling_values) == len(cooling_units)
        points.append({
            "local_date": record.get("local_date"),
            "outdoor_mean_c": temperature.get("mean"),
            "outdoor_coverage_percent": temperature.get("coverage_percent"),
            "heat_pumps": heat_pumps, "air_conditioners": air_conditioners,
            "cooling_units": cooling_units,
            "whole_house_heating_kwh": whole if complete else None,
            "whole_house_heating_known_subtotal_kwh": whole,
            "normalized_heating_kwh_m2_day": (
                whole / float(floor_area_m2) if complete and whole is not None and floor_area_m2 else None
            ),
            "ac_cooling_kwh": cooling if cooling_complete else None,
            "ac_cooling_known_subtotal_kwh": cooling,
            "normalized_cooling_kwh_m2_day": (
                cooling / float(floor_area_m2) if cooling_complete and cooling is not None and floor_area_m2 else None
            ),
            "classification_complete": complete,
            "cooling_classification_complete": cooling_complete,
            "unknown_kwh": energy.get("unknown_kwh"),
            "heating_exclusion_reasons": [] if complete else ["incomplete_heating_classification"],
            "cooling_exclusion_reasons": [] if cooling_complete else ["incomplete_ac_cooling_classification"],
        })
    return points


def _outdoor_interval_evidence(intervals, hourly, source_id):
    result = {}
    for interval in intervals:
        start = datetime.fromisoformat(interval["start_utc"]).astimezone(UTC)
        end = datetime.fromisoformat(interval["end_utc"]).astimezone(UTC)
        weighted = valid = 0.0
        for record in hourly:
            row_start = datetime.fromisoformat(record["period_start_utc"]).astimezone(UTC)
            row_end = datetime.fromisoformat(record["period_end_utc"]).astimezone(UTC)
            if row_start < start or row_end > end:
                continue
            value = (record.get("temperatures") or {}).get(source_id, {})
            mean = value.get("mean")
            seconds = float(value.get("coverage_seconds") or 0)
            if mean is not None and seconds > 0:
                weighted += float(mean) * seconds
                valid += seconds
        expected = (end - start).total_seconds()
        coverage = valid / expected * 100 if expected else 0.0
        key = (interval["start_utc"], interval["end_utc"])
        result[key] = {
            "mean_c": weighted / valid if valid and coverage >= 95.0 else None,
            "coverage_percent": coverage,
            "response_eligible": coverage >= 95.0,
            "complete": coverage >= 100.0,
        }
    return result


@websocket_api.websocket_command({
    vol.Required("type"): f"{DOMAIN}/v02_report",
    vol.Required("start_date"): str, vol.Required("end_date"): str,
})
@websocket_api.async_response
async def ws_v02_report(hass, connection, msg):
    """Return four separated response datasets without fabricating COP."""
    coordinator = _coordinator(hass)
    if coordinator is None:
        connection.send_error(msg["id"], "not_configured", "Integration is not configured")
        return
    try:
        start, end = validate_timeline_dates(
            msg["start_date"], msg["end_date"], maximum_days=3660
        )
    except ValueError as error:
        connection.send_error(msg["id"], "invalid_range", str(error))
        return
    records = await hass.async_add_executor_job(
        coordinator.database.history_period, start.isoformat(), end.isoformat()
    )
    local_today = datetime.now(UTC).astimezone(ZoneInfo(coordinator.timezone)).date()
    if start <= local_today <= end:
        partial_today, _hours, _coverage, _cutoff, _today = (
            await _current_day_snapshot(hass, coordinator)
        )
        records = replace_with_partial_day(records, partial_today)
    equipment = _equipment(coordinator)
    outdoor_source_id = coordinator.config.get("representative_outdoor_source_id")
    daily = _v02_daily_curves(
        records, equipment, outdoor_source_id, coordinator.config.get("floor_area_m2")
    )
    period_start, _ = local_day_bounds(start, coordinator.timezone)
    _, period_end = local_day_bounds(end, coordinator.timezone)
    readings = await hass.async_add_executor_job(
        coordinator.database.thermal_readings_period, period_start, period_end
    )
    candidate_intervals = [item for item in cumulative_intervals(readings)
                           if item["end_utc"] >= period_start.isoformat()
                           and item["start_utc"] <= period_end.isoformat()]
    intervals = [item for item in candidate_intervals
                 if item["start_utc"] >= period_start.isoformat()
                 and item["end_utc"] <= period_end.isoformat()]
    overlapping_evidence = [item for item in candidate_intervals if item not in intervals]
    hourly = []
    if intervals:
        interval_start = min(datetime.fromisoformat(item["start_utc"]) for item in intervals)
        interval_end = max(datetime.fromisoformat(item["end_utc"]) for item in intervals)
        hourly = await hass.async_add_executor_job(
            coordinator.database.hourly_range, interval_start, interval_end
        )
    outdoor = _outdoor_interval_evidence(intervals, hourly, outdoor_source_id)
    thermal_points = thermal_performance_points(
        intervals, hourly, equipment,
        {key: value["mean_c"] for key, value in outdoor.items()},
    )
    for point in thermal_points:
        point["outdoor_evidence"] = outdoor[(point["start_utc"], point["end_utc"])]
    def category_report(category):
        eligible = [point for point in thermal_points
                    if point.get(f"{category}_thermal_delta_kwh") is not None
                    and point.get(f"{category}_electricity_kwh") is not None]
        thermal_values = [point[f"{category}_thermal_delta_kwh"] for point in eligible]
        electricity_values = [point[f"{category}_electricity_kwh"] for point in eligible]
        return {"thermal_kwh": sum(thermal_values),
                "electricity_kwh": sum(electricity_values),
                "cop": ratio_of_sums(thermal_values, electricity_values),
                "matched_interval_count": len(eligible)}
    heating_report = category_report("heating")
    dhw_report = category_report("dhw")
    combined_report = {
        "thermal_kwh": heating_report["thermal_kwh"] + dhw_report["thermal_kwh"],
        "electricity_kwh": (
            heating_report["electricity_kwh"] + dhw_report["electricity_kwh"]
        ),
        "matched_interval_count": (
            heating_report["matched_interval_count"] + dhw_report["matched_interval_count"]
        ),
    }
    combined_report["cop"] = ratio_of_sums(
        [combined_report["thermal_kwh"]], [combined_report["electricity_kwh"]]
    )
    connection.send_result(msg["id"], {
        "timezone": coordinator.timezone, "floor_area_m2": coordinator.config.get("floor_area_m2"),
        "outdoor_source_id": outdoor_source_id,
        "whole_house_heating": daily, "ac_cooling": daily,
        "ecodan_space_heating": thermal_points, "ecodan_dhw": thermal_points,
        "ecodan_reports": {"heating": heating_report, "dhw": dhw_report,
                           "combined": combined_report},
        "overlapping_thermal_evidence": overlapping_evidence,
        "thermal_exact_interval_policy": "fully_contained_only",
        "cop_scope": "ecodan_only", "ac_cop_supported": False,
    })


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/thermal_readings"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_thermal_readings(hass, connection, msg):
    coordinator = _coordinator(hass)
    if coordinator is None:
        connection.send_error(msg["id"], "not_configured", "Integration is not configured")
        return
    readings = await hass.async_add_executor_job(
        lambda: coordinator.database.thermal_readings(limit=500)
    )
    total = await hass.async_add_executor_job(coordinator.database.thermal_reading_count)
    connection.send_result(msg["id"], {"readings": readings,
                                        "intervals": cumulative_intervals(readings),
                                        "total": total, "limit": 500})


_OPTIONAL_COUNTER = vol.Any(None, str, int, float)


@websocket_api.websocket_command({
    vol.Required("type"): f"{DOMAIN}/save_thermal_reading",
    vol.Optional("reading_id"): int,
    vol.Required("ecodan_equipment_id"): str, vol.Required("meter_identity"): str,
    vol.Required("local_date"): str, vol.Required("local_time"): str,
    vol.Optional("fold"): vol.In([0, 1]),
    vol.Optional("heating_thermal_kwh"): _OPTIONAL_COUNTER,
    vol.Optional("dhw_thermal_kwh"): _OPTIONAL_COUNTER,
    vol.Optional("vendor_heating_electricity_kwh"): _OPTIONAL_COUNTER,
    vol.Optional("vendor_dhw_electricity_kwh"): _OPTIONAL_COUNTER,
    vol.Required("source_label"): str, vol.Optional("note"): str,
    vol.Optional("unit", default="kWh"): vol.In(["Wh", "kWh", "MWh"]),
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_save_thermal_reading(hass, connection, msg):
    coordinator = _coordinator(hass)
    if coordinator is None:
        connection.send_error(msg["id"], "not_configured", "Integration is not configured")
        return
    equipment = {item.equipment_id: item for item in equipment_from_config(coordinator.config)}
    selected = equipment.get(msg["ecodan_equipment_id"])
    if selected is None or selected.kind != "heat_pump":
        connection.send_error(msg["id"], "invalid_equipment", "Select a configured heat pump")
        return
    try:
        payload = normalize_manual_reading(msg, coordinator.timezone)
        result = await hass.async_add_executor_job(
            coordinator.database.save_thermal_reading, payload, msg.get("reading_id")
        )
    except (ValueError, KeyError) as error:
        connection.send_error(msg["id"], "invalid_reading", str(error))
        return
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command({
    vol.Required("type"): f"{DOMAIN}/delete_thermal_reading",
    vol.Required("reading_id"): int,
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_delete_thermal_reading(hass, connection, msg):
    coordinator = _coordinator(hass)
    if coordinator is None:
        connection.send_error(msg["id"], "not_configured", "Integration is not configured")
        return
    deleted = await hass.async_add_executor_job(
        coordinator.database.delete_thermal_reading, msg["reading_id"]
    )
    if not deleted:
        connection.send_error(msg["id"], "not_found", "Thermal reading was not found")
        return
    connection.send_result(msg["id"], {"deleted": True})


@websocket_api.websocket_command({
    vol.Required("type"): f"{DOMAIN}/backfill_thermal_readings",
    vol.Required("start_date"): str, vol.Required("end_date"): str,
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_backfill_thermal_readings(hass, connection, msg):
    coordinator = _coordinator(hass)
    if coordinator is None:
        connection.send_error(msg["id"], "not_configured", "Integration is not configured")
        return
    try:
        start, end = validate_timeline_dates(
            msg["start_date"], msg["end_date"], maximum_days=366
        )
        result = await coordinator.async_backfill_thermal_counters(start, end)
    except ValueError as error:
        connection.send_error(msg["id"], "invalid_range", str(error))
        return
    connection.send_result(msg["id"], result)


async def _monthly_report_payload(hass, coordinator, end_month: str, count: int = 13):
    """Load one bounded SQLite window for the rolling monthly report."""
    months = rolling_months(end_month, count)
    start_date = f"{months[0]}-01"
    end_year, end_number = (int(part) for part in months[-1].split("-"))
    if end_number == 12:
        next_month = datetime(end_year + 1, 1, 1).date()
    else:
        next_month = datetime(end_year, end_number + 1, 1).date()
    end_date = (next_month - timedelta(days=1)).isoformat()
    records = await hass.async_add_executor_job(
        coordinator.database.history_period, start_date, end_date
    )
    local_today, local_yesterday = _local_calendar_dates(coordinator.timezone)
    if start_date <= local_yesterday.isoformat() <= end_date and not any(
        str(item.get("local_date")) == local_yesterday.isoformat() and not item.get("temperature_only") for item in records
    ):
        provisional = await _provisional_completed_day_snapshot(
            hass, coordinator, local_yesterday
        )
        if provisional is not None:
            records = prefer_authoritative_daily(records, provisional)
    if start_date <= local_today.isoformat() <= end_date:
        partial_today, _hours, _coverage, _cutoff, _today = await _current_day_snapshot(
            hass, coordinator
        )
        records = replace_with_partial_day(
            records, partial_today, local_date=local_today.isoformat()
        )
    snapshots = await hass.async_add_executor_job(
        lambda: coordinator.database.monthly_ecodan_snapshots(
            start_month=months[0], end_month=months[-1]
        )
    )
    hourly_records = []
    for month in months:
        candidates = [item for item in snapshots if item.get("report_month") == month and item.get("cutoff_certainty") == "exact" and item.get("cutoff_utc")]
        if not candidates:
            continue
        start, _ = local_day_bounds(date.fromisoformat(f"{month}-01"), coordinator.timezone)
        end = max(datetime.fromisoformat(item["cutoff_utc"]) for item in candidates)
        if end > start and (end - start).total_seconds() % 3600 == 0:
            hourly_records.extend(await hass.async_add_executor_job(coordinator.database.hourly_range, start, end))
    return build_rolling_month_report(
        daily_records=records, snapshots=snapshots, equipment=_equipment(coordinator),
        timezone=coordinator.timezone, end_month=end_month, count=count,
        floor_area_m2=coordinator.config.get("floor_area_m2"),
        hourly_records=hourly_records,
    )


@websocket_api.websocket_command({
    vol.Required("type"): f"{DOMAIN}/monthly_report",
    vol.Required("end_month"): str,
    vol.Optional("count", default=13): vol.All(vol.Coerce(int), vol.Range(min=1, max=24)),
})
@websocket_api.async_response
async def ws_monthly_report(hass, connection, msg):
    coordinator = _coordinator(hass)
    if coordinator is None:
        connection.send_error(msg["id"], "not_configured", "Integration is not configured")
        return
    try:
        result = await _monthly_report_payload(
            hass, coordinator, msg["end_month"], msg["count"]
        )
    except ValueError as error:
        connection.send_error(msg["id"], "invalid_month", str(error))
        return
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command({
    vol.Required("type"): f"{DOMAIN}/monthly_report_csv",
    vol.Required("end_month"): str,
    vol.Optional("count", default=13): vol.All(vol.Coerce(int), vol.Range(min=1, max=24)),
    vol.Required("metric_ids"): vol.All([str], vol.Length(min=1, max=30)),
})
@websocket_api.async_response
async def ws_monthly_report_csv(hass, connection, msg):
    coordinator = _coordinator(hass)
    if coordinator is None:
        connection.send_error(msg["id"], "not_configured", "Integration is not configured")
        return
    allowed = {item["id"] for item in monthly_metric_catalog(_equipment(coordinator))}
    metrics = list(dict.fromkeys(msg["metric_ids"]))
    invalid = [item for item in metrics if item not in allowed]
    if invalid:
        connection.send_error(msg["id"], "invalid_metric", ", ".join(invalid))
        return
    try:
        report = await _monthly_report_payload(
            hass, coordinator, msg["end_month"], msg["count"]
        )
    except ValueError as error:
        connection.send_error(msg["id"], "invalid_month", str(error))
        return
    output = io.StringIO()
    fields = [
        "month", *metrics, "quality", "primary_cop_quality", "snapshot_status",
        "snapshot_cutoff_utc", "energy_period_end_utc", "cutoff_match",
    ]
    writer = csv.DictWriter(output, fieldnames=fields)
    units = {item["id"]: item["unit"] for item in report["metric_catalog"]}
    writer.writerow({field: f"{field} ({units[field]})" if field in units else field for field in fields})
    for row in report["rows"]:
        writer.writerow({field: row.get(field) for field in fields})
    connection.send_result(msg["id"], {
        "filename": f"home-energy-intelligence-monthly-{msg['end_month']}.csv",
        "csv": output.getvalue(), "row_count": len(report["rows"]),
        "metric_ids": metrics,
    })


@websocket_api.websocket_command({
    vol.Required("type"): f"{DOMAIN}/monthly_ecodan_snapshots",
    vol.Optional("start_month"): str, vol.Optional("end_month"): str,
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_monthly_ecodan_snapshots(hass, connection, msg):
    coordinator = _coordinator(hass)
    if coordinator is None:
        connection.send_error(msg["id"], "not_configured", "Integration is not configured")
        return
    rows = await hass.async_add_executor_job(
        lambda: coordinator.database.monthly_ecodan_snapshots(
            start_month=msg.get("start_month"), end_month=msg.get("end_month")
        )
    )
    connection.send_result(msg["id"], {"snapshots": rows, "total": len(rows)})


@websocket_api.websocket_command({
    vol.Required("type"): f"{DOMAIN}/save_monthly_ecodan_snapshot",
    vol.Optional("snapshot_id"): int,
    vol.Required("ecodan_equipment_id"): str,
    vol.Optional("meter_identity", default="monthly_report"): str,
    vol.Required("report_month"): str,
    vol.Required("cutoff_local_date"): str,
    vol.Optional("cutoff_local_time"): str,
    vol.Optional("cutoff_certainty"): vol.In(["exact", "day_only", "unknown"]),
    vol.Optional("fold"): vol.In([0, 1]),
    vol.Optional("entry_local_date"): str,
    vol.Optional("entry_local_time"): str,
    vol.Optional("status", default="partial"): vol.In(["partial", "final"]),
    vol.Optional("heating_thermal_kwh"): _OPTIONAL_COUNTER,
    vol.Optional("dhw_thermal_kwh"): _OPTIONAL_COUNTER,
    vol.Optional("vendor_heating_electricity_kwh"): _OPTIONAL_COUNTER,
    vol.Optional("vendor_dhw_electricity_kwh"): _OPTIONAL_COUNTER,
    vol.Optional("vendor_combined_electricity_kwh"): _OPTIONAL_COUNTER,
    vol.Optional("vendor_combined_thermal_kwh"): _OPTIONAL_COUNTER,
    vol.Required("source_label"): str,
    vol.Optional("source_reference"): str,
    vol.Optional("note"): str,
    vol.Optional("unit", default="kWh"): vol.In(["Wh", "kWh", "MWh"]),
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_save_monthly_ecodan_snapshot(hass, connection, msg):
    coordinator = _coordinator(hass)
    if coordinator is None:
        connection.send_error(msg["id"], "not_configured", "Integration is not configured")
        return
    equipment = {item.equipment_id: item for item in equipment_from_config(coordinator.config)}
    selected = equipment.get(msg["ecodan_equipment_id"])
    if selected is None or selected.kind != "heat_pump":
        connection.send_error(msg["id"], "invalid_equipment", "Select a configured heat pump")
        return
    try:
        payload = normalize_monthly_snapshot(msg, coordinator.timezone)
        result = await hass.async_add_executor_job(
            coordinator.database.save_monthly_ecodan_snapshot,
            payload, msg.get("snapshot_id"),
        )
    except (ValueError, KeyError) as error:
        connection.send_error(msg["id"], "invalid_snapshot", str(error))
        return
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command({
    vol.Required("type"): f"{DOMAIN}/delete_monthly_ecodan_snapshot",
    vol.Required("snapshot_id"): int,
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_delete_monthly_ecodan_snapshot(hass, connection, msg):
    coordinator = _coordinator(hass)
    if coordinator is None:
        connection.send_error(msg["id"], "not_configured", "Integration is not configured")
        return
    deleted = await hass.async_add_executor_job(
        coordinator.database.delete_monthly_ecodan_snapshot, msg["snapshot_id"]
    )
    if not deleted:
        connection.send_error(msg["id"], "not_found", "Monthly snapshot was not found")
        return
    connection.send_result(msg["id"], {"deleted": True})


async def _current_day_snapshot(
    hass, coordinator, cutoff: datetime | None = None, *, now: datetime | None = None,
):
    """Build today's non-persisted partial exclusively from completed hourly rows."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    completed_cutoff = now.replace(minute=0, second=0, microsecond=0)
    cutoff = min(cutoff or completed_cutoff, completed_cutoff)
    zone = ZoneInfo(coordinator.timezone)
    today = now.astimezone(zone).date()
    today_start, _ = local_day_bounds(today, coordinator.timezone)
    if cutoff < today_start:
        raise ValueError("cutoff is outside the current Home Assistant local day")
    stored_hours = await hass.async_add_executor_job(
        coordinator.database.hourly_range, today_start, cutoff
    )
    energy_hours = [item for item in stored_hours if not item.get("temperature_only")]
    temperature_hours = await hass.async_add_executor_job(
        coordinator.database.temperature_history_range,
        "hourly", today_start, cutoff,
    )
    coverage = interval_coverage(energy_hours, today_start, cutoff)
    current = combine_interval_records(
        energy_hours, expected_start=today_start, expected_end=cutoff
    )
    temperature_current = combine_interval_records(
        temperature_hours, expected_start=today_start, expected_end=cutoff
    ) if temperature_hours else None
    if current is None and temperature_current is not None:
        current = {
            **temperature_current,
            "household_kwh": None, "pv_kwh": None, "import_kwh": None,
            "export_kwh": None, "non_hvac_kwh": None, "allocations": {},
            "interval_coverage": coverage, "temperature_only": True,
        }
    elif current is not None and temperature_current is not None:
        for source_id, summary in temperature_current.get("temperatures", {}).items():
            existing = current.setdefault("temperatures", {}).get(source_id)
            if not isinstance(existing, dict) or float(
                summary.get("coverage_seconds", 0) or 0
            ) > float(existing.get("coverage_seconds", 0) or 0):
                current["temperatures"][source_id] = summary
    hours_by_start = {item["period_start_utc"]: item for item in stored_hours}
    for item in temperature_hours:
        existing = hours_by_start.get(item["period_start_utc"])
        if existing is None:
            hours_by_start[item["period_start_utc"]] = item
        else:
            existing.setdefault("temperatures", {}).update(item.get("temperatures", {}))
    display_hours = sorted(hours_by_start.values(), key=lambda item: item["period_start_utc"])
    if current is not None:
        current.update({"partial": True, "finalized": False})
    return current, display_hours, coverage, cutoff, today


async def _provisional_completed_day_snapshot(hass, coordinator, day):
    """Return a completed local day from persisted hours without writing daily history."""
    start, end = local_day_bounds(day, coordinator.timezone)
    rows = await hass.async_add_executor_job(
        coordinator.database.hourly_range, start, end
    )
    return provisional_completed_day_from_hourly(
        rows, start, end, local_date=day.isoformat(), timezone=coordinator.timezone,
    )


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/overview"})
@websocket_api.async_response
async def ws_overview(hass, connection, msg):
    coordinator = _coordinator(hass)
    if coordinator is None:
        connection.send_result(msg["id"], {"configured": False, "latest": None, "equipment": []})
        return
    equipment = _equipment(coordinator)
    current, today_hours, today_coverage, cutoff, today = await _current_day_snapshot(
        hass, coordinator
    )
    yesterday = today - timedelta(days=1)
    yesterday_start, comparison_end = previous_day_same_local_cutoff(
        cutoff, coordinator.timezone
    )
    comparison_hours = await hass.async_add_executor_job(
        coordinator.database.hourly_range, yesterday_start, comparison_end
    )
    comparison_coverage = interval_coverage(comparison_hours, yesterday_start, comparison_end)
    comparison = combine_interval_records(
        comparison_hours, expected_start=yesterday_start, expected_end=comparison_end
    )
    if comparison is not None:
        comparison.update({"partial": True, "finalized": False})
    yesterday_full = await hass.async_add_executor_job(
        coordinator.database.daily, yesterday.isoformat()
    )
    if yesterday_full is None or yesterday_full.get("temperature_only"):
        provisional = await _provisional_completed_day_snapshot(
            hass, coordinator, yesterday
        )
        if provisional is not None:
            yesterday_full = prefer_authoritative_daily(
                [yesterday_full] if yesterday_full else [], provisional
            )[0]
    required_temperature_ids = _representative_temperature_source_ids(coordinator)
    if yesterday_full:
        yesterday_full = derive_record_quality(
            yesterday_full,
            equipment_ids=[item["equipment_id"] for item in equipment if item["enabled"]],
            temperature_source_ids=required_temperature_ids,
            timezone=coordinator.timezone,
            response_temperature_coverage_minimum=(
                _response_temperature_coverage_minimum(coordinator)
            ),
        )
    latest_hour_end = today_hours[-1]["period_end_utc"] if today_hours else None
    temperatures = [
        {
            "source_id": item.source_id,
            "display_name": item.display_name,
            "role": item.role,
            "enabled": item.enabled,
            "entity_id": item.entity_id,
            "attribute": item.attribute,
        }
        for item in _temperatures(coordinator)
    ]
    current_temperatures = (current or {}).get("temperatures", {})
    temperature_pipeline = []
    for item in _temperatures(coordinator):
        state = hass.states.get(item.entity_id)
        summary = current_temperatures.get(item.source_id)
        if not item.enabled:
            stage = "disabled"
        elif state is None:
            stage = "live_entity_unavailable"
        elif not today_hours:
            stage = "hourly_processing_missing"
        elif not isinstance(summary, dict):
            stage = "temperature_summary_not_persisted"
        elif summary.get("mean") is None:
            stage = "temperature_rejected_or_uncovered"
        else:
            stage = "available"
        temperature_pipeline.append({
            "source_id": item.source_id, "entity_id": item.entity_id,
            "configured": True, "enabled": item.enabled, "attribute": item.attribute,
            "pipeline_stage": stage,
            "live_state": ((state.attributes.get(item.attribute) if item.attribute else state.state)
                           if state is not None else None),
            "live_last_updated": state.last_updated.isoformat() if state is not None else None,
            "live_unit": ((state.attributes.get("unit_of_measurement")
                           or state.attributes.get("temperature_unit"))
                          if state is not None else None),
            "persisted_hourly_summaries": sum(
                1 for row in today_hours
                if isinstance(row.get("temperatures", {}).get(item.source_id), dict)
            ),
            "latest_quality": summary.get("quality") if isinstance(summary, dict) else None,
            "latest_coverage_percent": (
                summary.get("coverage_percent") if isinstance(summary, dict) else None
            ),
            "rejected_samples": (
                summary.get("rejections", []) if isinstance(summary, dict) else []
            ),
        })
    connection.send_result(msg["id"], {
        "configured": True,
        "latest": attach_canonical(current, equipment) if current else None,
        "today": attach_canonical(current, equipment) if current else None,
        "yesterday_same_cutoff": (attach_canonical(comparison, equipment)
                                   if comparison else None),
        "yesterday_full": (attach_canonical(yesterday_full, equipment)
                           if yesterday_full else None),
        "cutoff_utc": cutoff.isoformat(),
        "cutoff_local": cutoff.astimezone(ZoneInfo(coordinator.timezone)).isoformat(),
        "timezone": coordinator.timezone, "latest_hour_end_utc": latest_hour_end,
        "partial": True, "stale": not today_coverage["complete"],
        "today_interval_coverage": today_coverage,
        "yesterday_interval_coverage": comparison_coverage,
        "energy_season_start_month": int(coordinator.config.get("energy_season_start_month", 7)),
        "energy_season_start_day": int(coordinator.config.get("energy_season_start_day", 1)),
        "floor_area_m2": coordinator.config.get("floor_area_m2"),
        "equipment": equipment,
        "temperature_sources": temperatures,
        "representative_indoor_source_id": coordinator.config.get(
            "representative_indoor_source_id"
        ),
        "representative_outdoor_source_id": coordinator.config.get(
            "representative_outdoor_source_id"
        ),
        "temperature_timeline": [
            {"start": row["period_start_utc"], "end": row["period_end_utc"],
             "temperatures": row.get("temperatures", {})}
            for row in today_hours
        ],
        "temperature_pipeline": temperature_pipeline,
    })


@websocket_api.websocket_command({
    vol.Required("type"): f"{DOMAIN}/history",
    vol.Optional("limit", default=90): vol.All(vol.Coerce(int), vol.Range(min=1, max=3660)),
    vol.Optional("before"): str,
    vol.Optional("start_date"): str,
    vol.Optional("end_date"): str,
    vol.Optional("preset"): str,
    vol.Optional("older_month"): str,
    vol.Optional("cutoff_utc"): str,
    vol.Optional("offset", default=0): vol.All(vol.Coerce(int), vol.Range(min=0)),
})
@websocket_api.async_response
async def ws_history(hass, connection, msg):
    coordinator = _coordinator(hass)
    if coordinator is None:
        connection.send_result(msg["id"], {"records": []})
        return
    equipment = _equipment(coordinator)
    equipment_ids = [item["equipment_id"] for item in equipment if item["enabled"]]
    start_date, end_date = msg.get("start_date"), msg.get("end_date")
    selected_label = None
    if preset := msg.get("preset"):
        try:
            local_today = datetime.now(UTC).astimezone(ZoneInfo(coordinator.timezone)).date()
            start, end, selected_label = period_bounds(
                preset, local_today,
                season_month=int(coordinator.config.get("energy_season_start_month", 7)),
                season_day=int(coordinator.config.get("energy_season_start_day", 1)),
                older_month=msg.get("older_month"), custom_start=start_date, custom_end=end_date,
            )
            start_date, end_date = start.isoformat(), end.isoformat()
        except ValueError as error:
            connection.send_error(msg["id"], "invalid_period", str(error))
            return
    partial_today = None
    local_today, _local_yesterday = _local_calendar_dates(coordinator.timezone)
    if start_date and end_date and start_date <= local_today.isoformat() <= end_date:
        requested_cutoff = None
        if msg.get("cutoff_utc"):
            try:
                requested_cutoff = datetime.fromisoformat(msg["cutoff_utc"])
                if requested_cutoff.tzinfo is None:
                    raise ValueError("cutoff must include a timezone")
                requested_cutoff = requested_cutoff.astimezone(UTC)
            except ValueError as error:
                connection.send_error(msg["id"], "invalid_cutoff", str(error))
                return
        try:
            partial_today, _hours, _coverage, history_cutoff, _today = (
                await _current_day_snapshot(hass, coordinator, requested_cutoff)
            )
        except ValueError as error:
            connection.send_error(msg["id"], "invalid_cutoff", str(error))
            return
    else:
        history_cutoff = None

    if start_date and end_date:
        stored_records = await hass.async_add_executor_job(
            coordinator.database.history_period, start_date, end_date
        )
        local_yesterday = _local_yesterday
        yesterday_key = local_yesterday.isoformat()
        if (
            start_date <= yesterday_key <= end_date
            and not any(str(item.get("local_date")) == yesterday_key and not item.get("temperature_only") for item in stored_records)
        ):
            provisional_yesterday = await _provisional_completed_day_snapshot(
                hass, coordinator, local_yesterday
            )
            stored_records = prefer_authoritative_daily(
                stored_records, provisional_yesterday
            )
        period_records = replace_with_partial_day(
            stored_records, partial_today,
            local_date=local_today.isoformat()
            if start_date <= local_today.isoformat() <= end_date else None,
        )
        total = len(period_records)
        records = list(reversed(period_records))[msg["offset"]:msg["offset"] + msg["limit"]]
    elif start_date or end_date or msg.get("offset"):
        records, total = await hass.async_add_executor_job(
            lambda: coordinator.database.history_range(
                start_date=start_date, end_date=end_date,
                limit=msg["limit"], offset=msg["offset"],
            )
        )
    else:
        records = await hass.async_add_executor_job(
            lambda: coordinator.database.history(limit=msg["limit"], before=msg.get("before"))
        )
        total = len(records)
    temperature_source_ids = [item.source_id for item in _temperatures(coordinator) if item.enabled]
    required_temperature_ids = _representative_temperature_source_ids(coordinator)
    if start_date and end_date:
        combined = combine_daily_records(
            period_records, start_date, end_date,
            temperature_source_ids=temperature_source_ids,
            required_temperature_source_ids=required_temperature_ids,
            equipment_ids=equipment_ids, timezone=coordinator.timezone,
            response_temperature_coverage_minimum=(
                _response_temperature_coverage_minimum(coordinator)
            ),
        )
        selected_coverage = daily_coverage(
            period_records, start_date, end_date,
            equipment_ids=equipment_ids,
            temperature_source_ids=required_temperature_ids,
            timezone=coordinator.timezone,
            response_temperature_coverage_minimum=(
                _response_temperature_coverage_minimum(coordinator)
            ),
        )
        if combined:
            selected_coverage = combined["period_coverage"]
    elif records:
        period_records = records
        page_dates = [item["local_date"] for item in records]
        combined = combine_daily_records(
            records, min(page_dates), max(page_dates),
            temperature_source_ids=temperature_source_ids,
            required_temperature_source_ids=required_temperature_ids,
            equipment_ids=equipment_ids, timezone=coordinator.timezone,
            response_temperature_coverage_minimum=(
                _response_temperature_coverage_minimum(coordinator)
            ),
        )
        selected_coverage = combined["period_coverage"] if combined else None
    else:
        period_records = []
        combined = None
        selected_coverage = None
    representative_outdoor_id = coordinator.config.get(
        "representative_outdoor_source_id"
    )
    representative_outdoor = next((
        item for item in _temperatures(coordinator)
        if item.source_id == representative_outdoor_id
    ), None)
    response_profile = heating_response_profile(
        period_records, equipment,
        outdoor_source_id=representative_outdoor_id,
        outdoor_source_is_measured_air=bool(
            representative_outdoor and representative_outdoor.enabled
            and representative_outdoor.role == "outdoor"
        ),
        floor_area_m2=coordinator.config.get("floor_area_m2"),
        minimum_temperature_coverage=_response_temperature_coverage_minimum(coordinator),
    )
    connection.send_result(msg["id"], {
        "records": [attach_canonical(derive_record_quality(
            record, equipment_ids=equipment_ids,
            temperature_source_ids=required_temperature_ids,
            timezone=coordinator.timezone,
            response_temperature_coverage_minimum=(
                _response_temperature_coverage_minimum(coordinator)
            ),
        ), equipment) for record in records],
        "summary": attach_canonical(combined, equipment) if combined else None,
        "total": total, "offset": msg["offset"], "limit": msg["limit"],
        "summary_record_count": len(period_records),
        "period_coverage": selected_coverage,
        "selected_period": {"preset": msg.get("preset"), "label": selected_label,
                            "start_date": start_date, "end_date": end_date},
        "partial": partial_today is not None,
        "provisional_dates": [
            record["local_date"] for record in period_records
            if record.get("provisional") is True and record.get("finalized") is False
        ],
        "cutoff_utc": history_cutoff.isoformat() if history_cutoff else None,
        "cutoff_local": (history_cutoff.astimezone(ZoneInfo(coordinator.timezone)).isoformat()
                         if history_cutoff else None),
        "timezone": coordinator.timezone,
        "response_profile": response_profile,
    })


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/refresh"})
@websocket_api.async_response
async def ws_refresh(hass, connection, msg):
    coordinator = _coordinator(hass)
    if coordinator is None:
        connection.send_error(msg["id"], "not_configured", "Integration is not configured")
        return
    changed = await coordinator.async_refresh_hourly(reprocess_poor=True)
    connection.send_result(msg["id"], {"changed_intervals": changed})


def _diagnostic_dates(coordinator, msg):
    today = datetime.now(UTC).astimezone(ZoneInfo(coordinator.timezone)).date()
    try:
        start = datetime.fromisoformat(msg.get("start_date") or (
            today - timedelta(days=30)
        ).isoformat()).date()
        end = datetime.fromisoformat(msg.get("end_date") or today.isoformat()).date()
    except ValueError as error:
        raise ValueError("Dates must use YYYY-MM-DD") from error
    if end < start or (end - start).days > 30:
        raise ValueError("Range must be ordered and at most 31 days")
    return start, end


@websocket_api.websocket_command({
    vol.Required("type"): f"{DOMAIN}/temperature_diagnostics",
    vol.Optional("start_date"): str, vol.Optional("end_date"): str,
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_temperature_diagnostics(hass, connection, msg):
    coordinator = _coordinator(hass)
    if coordinator is None:
        connection.send_error(msg["id"], "not_configured", "Integration is not configured")
        return
    try:
        start, end = _diagnostic_dates(coordinator, msg)
        result = await coordinator.async_temperature_diagnostics(start, end)
    except ValueError as error:
        connection.send_error(msg["id"], "invalid_range", str(error))
        return
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command({
    vol.Required("type"): f"{DOMAIN}/reprocess_temperature_history",
    vol.Optional("start_date"): str, vol.Optional("end_date"): str,
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_reprocess_temperature_history(hass, connection, msg):
    coordinator = _coordinator(hass)
    if coordinator is None:
        connection.send_error(msg["id"], "not_configured", "Integration is not configured")
        return
    try:
        start, end = _diagnostic_dates(coordinator, msg)
        result = await coordinator.async_reprocess_temperature_history(start, end)
    except ValueError as error:
        connection.send_error(msg["id"], "invalid_range", str(error))
        return
    connection.send_result(msg["id"], result)


async def _start_temperature_backfill(hass, connection, msg, *, dry_run: bool):
    coordinator = _coordinator(hass)
    if coordinator is None:
        connection.send_error(msg["id"], "not_configured", "Integration is not configured")
        return
    try:
        start, end = _diagnostic_dates(coordinator, msg)
        result = await coordinator.async_start_temperature_backfill(
            start, end, dry_run=dry_run,
            attest_current_mapping=bool(msg.get("attest_current_mapping", False)),
        )
    except ValueError as error:
        connection.send_error(msg["id"], "invalid_range", str(error))
        return
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command({
    vol.Required("type"): f"{DOMAIN}/temperature_backfill_preview",
    vol.Optional("start_date"): str, vol.Optional("end_date"): str,
    vol.Optional("attest_current_mapping", default=False): bool,
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_temperature_backfill_preview(hass, connection, msg):
    await _start_temperature_backfill(hass, connection, msg, dry_run=True)


@websocket_api.websocket_command({
    vol.Required("type"): f"{DOMAIN}/temperature_backfill_start",
    vol.Optional("start_date"): str, vol.Optional("end_date"): str,
    vol.Optional("attest_current_mapping", default=False): bool,
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_temperature_backfill_start(hass, connection, msg):
    await _start_temperature_backfill(hass, connection, msg, dry_run=False)


@websocket_api.websocket_command({
    vol.Required("type"): f"{DOMAIN}/temperature_backfill_status",
    vol.Required("job_id"): vol.Coerce(int),
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_temperature_backfill_status(hass, connection, msg):
    coordinator = _coordinator(hass)
    if coordinator is None:
        connection.send_error(msg["id"], "not_configured", "Integration is not configured")
        return
    result = await hass.async_add_executor_job(
        coordinator.database.temperature_backfill_job, msg["job_id"]
    )
    if result is None:
        connection.send_error(msg["id"], "not_found", "Backfill job was not found")
        return
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command({
    vol.Required("type"): f"{DOMAIN}/temperature_backfill_cancel",
    vol.Required("job_id"): vol.Coerce(int),
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_temperature_backfill_cancel(hass, connection, msg):
    coordinator = _coordinator(hass)
    if coordinator is None:
        connection.send_error(msg["id"], "not_configured", "Integration is not configured")
        return
    requested = await hass.async_add_executor_job(
        coordinator.database.request_temperature_backfill_cancel, msg["job_id"]
    )
    result = await hass.async_add_executor_job(
        coordinator.database.temperature_backfill_job, msg["job_id"]
    )
    if result is None:
        connection.send_error(msg["id"], "not_found", "Backfill job was not found")
        return
    result["cancel_requested"] = bool(requested or result.get("cancel_requested"))
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command({
    vol.Required("type"): f"{DOMAIN}/history_csv",
    vol.Optional("start_date"): str, vol.Optional("end_date"): str,
    vol.Optional("cutoff_utc"): str,
})
@websocket_api.async_response
async def ws_csv(hass, connection, msg):
    """Return authenticated, bounded CSV using the canonical contract."""
    coordinator = _coordinator(hass)
    if coordinator is None:
        connection.send_error(msg["id"], "not_configured", "Integration is not configured")
        return
    start_date, end_date = msg.get("start_date"), msg.get("end_date")
    if not start_date or not end_date:
        first, last = await hass.async_add_executor_job(coordinator.database.daily_bounds)
        start_date, end_date = start_date or first, end_date or last
    records = [] if not start_date or not end_date else await hass.async_add_executor_job(
        coordinator.database.history_period, start_date, end_date
    )
    local_today = datetime.now(UTC).astimezone(ZoneInfo(coordinator.timezone)).date()
    if start_date and end_date and start_date <= local_today.isoformat() <= end_date:
        requested_cutoff = None
        try:
            if msg.get("cutoff_utc"):
                requested_cutoff = datetime.fromisoformat(msg["cutoff_utc"])
                if requested_cutoff.tzinfo is None:
                    raise ValueError("cutoff must include a timezone")
                requested_cutoff = requested_cutoff.astimezone(UTC)
            partial_today, _hours, _coverage, _cutoff, _today = await _current_day_snapshot(
                hass, coordinator, requested_cutoff
            )
        except ValueError as error:
            connection.send_error(msg["id"], "invalid_cutoff", str(error))
            return
        records = replace_with_partial_day(
            records, partial_today, local_date=local_today.isoformat()
        )
    equipment = _equipment(coordinator)
    output = io.StringIO()
    fields = ["date", "household_kwh", "confirmed_heat_pump_heating_kwh",
              "assumed_heat_pump_heating_kwh", "confirmed_ac_heating_kwh",
              "selected_ac_heating_kwh", "estimated_ac_heating_kwh", "space_heating_kwh",
              "dhw_kwh", "heating_and_dhw_kwh", "cooling_kwh",
              "idle_kwh", "other_hvac_kwh", "other_household_kwh", "unknown_kwh",
              "unallocated_discrepancy_kwh", "known_space_heating_kwh", "known_dhw_kwh",
              "known_cooling_kwh", "known_idle_kwh", "known_other_hvac_kwh",
              "pv_kwh", "import_kwh", "export_kwh",
              "complete", "quality", "partial", "finalized", "cutoff_utc",
              "date_coverage_percent", "meter_coverage_percent",
              "allocation_coverage_percent", "allocation_coverage_known"]
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for record in reversed(records):
        record = derive_record_quality(
            record,
            equipment_ids=[item["equipment_id"] for item in equipment if item["enabled"]],
            temperature_source_ids=_representative_temperature_source_ids(coordinator),
            timezone=coordinator.timezone,
            response_temperature_coverage_minimum=(
                _response_temperature_coverage_minimum(coordinator)
            ),
        )
        energy = attach_canonical(record, equipment)["energy"] or {}
        row_coverage = daily_coverage(
            [record], record["local_date"], record["local_date"],
            equipment_ids=[item["equipment_id"] for item in equipment if item["enabled"]],
            temperature_source_ids=_representative_temperature_source_ids(coordinator),
            timezone=coordinator.timezone,
            response_temperature_coverage_minimum=(
                _response_temperature_coverage_minimum(coordinator)
            ),
        )
        allocation_coverage = row_coverage["allocation_coverage"]
        writer.writerow({"date": record["local_date"], **{
            key: energy.get(key) for key in fields if key not in {
                "date", "quality", "partial", "finalized", "cutoff_utc",
                "date_coverage_percent", "meter_coverage_percent",
                "allocation_coverage_percent", "allocation_coverage_known",
            }
        }, "quality": record.get("quality"), "partial": record.get("partial", False),
           "finalized": record.get("finalized", True),
           "cutoff_utc": record.get("period_end_utc"),
           "date_coverage_percent": row_coverage["date_coverage"]["coverage_percent"],
           "meter_coverage_percent": row_coverage["meter_coverage"]["coverage_percent"],
           "allocation_coverage_percent": allocation_coverage["coverage_percent"],
           "allocation_coverage_known": allocation_coverage["coverage_known"]})
    connection.send_result(msg["id"], {
        "filename": "home-energy-intelligence.csv", "csv": output.getvalue(),
        "record_count": len(records), "start_date": start_date, "end_date": end_date,
    })


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/settings"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_settings(hass, connection, msg):
    coordinator = _coordinator(hass)
    if coordinator is None:
        connection.send_result(msg["id"], {"configured": False})
        return
    config = coordinator.config
    mapping = []
    for item in equipment_from_config(config):
        for source_type in ("energy", "power", "state", "room_temperature", "supply_temperature", "return_temperature"):
            entity_id = getattr(item, f"{source_type}_entity", None)
            if entity_id:
                mapping.append({"key": f"equipment.{item.equipment_id}.{source_type}",
                                "label": item.display_name, "entity_id": entity_id,
                                "enabled": item.enabled})
    for item in temperatures_from_config(config):
        mapping.append({"key": f"temperature.{item.source_id}", "label": item.display_name,
                        "entity_id": item.entity_id, "enabled": item.enabled})
    for key in (
        "heating_thermal_energy_entity", "dhw_thermal_energy_entity",
        "vendor_heating_electricity_entity", "vendor_dhw_electricity_entity",
    ):
        if config.get(key):
            mapping.append({"key": f"thermal.{key}", "label": key.replace("_", " ").title(),
                            "entity_id": config[key], "enabled": True})
    storage = await hass.async_add_executor_job(coordinator.database.storage_status)
    connection.send_result(msg["id"], {
        "configured": True,
        "entry_id": coordinator.entry.entry_id,
        "timezone": coordinator.timezone,
        "floor_area_m2": config.get("floor_area_m2"),
        "cycle_hours": config.get("cycle_hours", 6),
        "influx_enabled": bool(config.get("influx_enabled", False)),
        "thermal_source_mode": config.get("thermal_source_mode", "manual_only"),
        "ecodan_thermal_equipment_id": config.get("ecodan_thermal_equipment_id"),
        "mapping": mapping,
        "database_size_bytes": storage["database_size_bytes"],
        "storage": storage,
    })

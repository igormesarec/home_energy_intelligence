"""Small read-only WebSocket API for the dashboard."""

from __future__ import annotations

import csv
from datetime import UTC, datetime, timedelta
import io
from zoneinfo import ZoneInfo

import voluptuous as vol

from homeassistant.components import websocket_api

from .configuration import (
    equipment_from_config, equipment_temperature_mappings, temperatures_from_config,
)
from .analytics import (
    attach_canonical, combine_daily_records, combine_interval_records, daily_coverage,
    derive_record_quality, heating_response_profile,
    interval_coverage, replace_with_partial_day,
)
from .const import DOMAIN
from .periods import period_bounds, previous_day_same_local_cutoff
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
    energy_hours = await hass.async_add_executor_job(
        coordinator.database.hourly_range, today_start, cutoff
    )
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
    hours_by_start = {item["period_start_utc"]: item for item in energy_hours}
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
    local_today = datetime.now(UTC).astimezone(ZoneInfo(coordinator.timezone)).date()
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
    storage = await hass.async_add_executor_job(coordinator.database.storage_status)
    connection.send_result(msg["id"], {
        "configured": True,
        "entry_id": coordinator.entry.entry_id,
        "timezone": coordinator.timezone,
        "floor_area_m2": config.get("floor_area_m2"),
        "cycle_hours": config.get("cycle_hours", 6),
        "influx_enabled": bool(config.get("influx_enabled", False)),
        "mapping": mapping,
        "database_size_bytes": storage["database_size_bytes"],
        "storage": storage,
    })

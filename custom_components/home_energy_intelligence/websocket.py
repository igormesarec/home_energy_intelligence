"""Small read-only WebSocket API for the dashboard."""

from __future__ import annotations

import csv
from datetime import UTC, datetime, timedelta
import io
from zoneinfo import ZoneInfo

import voluptuous as vol

from homeassistant.components import websocket_api

from .configuration import equipment_from_config, temperatures_from_config
from .analytics import attach_canonical, combine_interval_records
from .const import DOMAIN
from .timeutils import local_day_bounds


def async_register_websocket(hass) -> None:
    websocket_api.async_register_command(hass, ws_overview)
    websocket_api.async_register_command(hass, ws_history)
    websocket_api.async_register_command(hass, ws_settings)
    websocket_api.async_register_command(hass, ws_refresh)
    websocket_api.async_register_command(hass, ws_csv)


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


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/overview"})
@websocket_api.async_response
async def ws_overview(hass, connection, msg):
    coordinator = _coordinator(hass)
    if coordinator is None:
        connection.send_result(msg["id"], {"configured": False, "latest": None, "equipment": []})
        return
    equipment = _equipment(coordinator)
    now = datetime.now(UTC)
    zone = ZoneInfo(coordinator.timezone)
    today = now.astimezone(zone).date()
    today_start, _ = local_day_bounds(today, coordinator.timezone)
    cutoff = now.replace(minute=0, second=0, microsecond=0)
    today_hours = await hass.async_add_executor_job(
        coordinator.database.hourly_range, today_start, cutoff
    )
    current = combine_interval_records(today_hours)
    yesterday = today - timedelta(days=1)
    yesterday_start, yesterday_end = local_day_bounds(yesterday, coordinator.timezone)
    elapsed = cutoff - today_start
    comparison_end = min(yesterday_end, yesterday_start + elapsed)
    comparison_hours = await hass.async_add_executor_job(
        coordinator.database.hourly_range, yesterday_start, comparison_end
    )
    comparison = combine_interval_records(comparison_hours)
    yesterday_full = await hass.async_add_executor_job(
        coordinator.database.daily, yesterday.isoformat()
    )
    latest_hour_end = today_hours[-1]["period_end_utc"] if today_hours else None
    temperatures = [
        {
            "source_id": item.source_id,
            "display_name": item.display_name,
            "role": item.role,
            "enabled": item.enabled,
        }
        for item in temperatures_from_config(coordinator.config)
    ]
    connection.send_result(msg["id"], {
        "configured": True,
        "latest": attach_canonical(current, equipment) if current else None,
        "today": attach_canonical(current, equipment) if current else None,
        "yesterday_same_cutoff": (attach_canonical(comparison, equipment)
                                   if comparison else None),
        "yesterday_full": (attach_canonical(yesterday_full, equipment)
                           if yesterday_full else None),
        "cutoff_utc": cutoff.isoformat(), "latest_hour_end_utc": latest_hour_end,
        "partial": True, "stale": latest_hour_end != cutoff.isoformat(),
        "energy_season_start_month": int(coordinator.config.get("energy_season_start_month", 7)),
        "energy_season_start_day": int(coordinator.config.get("energy_season_start_day", 1)),
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
    })


@websocket_api.websocket_command({
    vol.Required("type"): f"{DOMAIN}/history",
    vol.Optional("limit", default=90): vol.All(vol.Coerce(int), vol.Range(min=1, max=3660)),
    vol.Optional("before"): str,
    vol.Optional("start_date"): str,
    vol.Optional("end_date"): str,
    vol.Optional("offset", default=0): vol.All(vol.Coerce(int), vol.Range(min=0)),
})
@websocket_api.async_response
async def ws_history(hass, connection, msg):
    coordinator = _coordinator(hass)
    if coordinator is None:
        connection.send_result(msg["id"], {"records": []})
        return
    equipment = _equipment(coordinator)
    if msg.get("start_date") or msg.get("end_date") or msg.get("offset"):
        records, total = await hass.async_add_executor_job(
            lambda: coordinator.database.history_range(
                start_date=msg.get("start_date"), end_date=msg.get("end_date"),
                limit=msg["limit"], offset=msg["offset"],
            )
        )
    else:
        records = await hass.async_add_executor_job(
            lambda: coordinator.database.history(limit=msg["limit"], before=msg.get("before"))
        )
        total = len(records)
    combined = combine_interval_records(records)
    connection.send_result(msg["id"], {
        "records": [attach_canonical(record, equipment) for record in records],
        "summary": attach_canonical(combined, equipment) if combined else None,
        "total": total, "offset": msg["offset"], "limit": msg["limit"],
    })


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/refresh"})
@websocket_api.async_response
async def ws_refresh(hass, connection, msg):
    coordinator = _coordinator(hass)
    if coordinator is None:
        connection.send_error(msg["id"], "not_configured", "Integration is not configured")
        return
    changed = await coordinator.async_refresh_hourly()
    connection.send_result(msg["id"], {"changed_intervals": changed})


@websocket_api.websocket_command({
    vol.Required("type"): f"{DOMAIN}/history_csv",
    vol.Optional("start_date"): str, vol.Optional("end_date"): str,
})
@websocket_api.async_response
async def ws_csv(hass, connection, msg):
    """Return authenticated, bounded CSV using the canonical contract."""
    coordinator = _coordinator(hass)
    if coordinator is None:
        connection.send_error(msg["id"], "not_configured", "Integration is not configured")
        return
    records, _ = await hass.async_add_executor_job(
        lambda: coordinator.database.history_range(
            start_date=msg.get("start_date"), end_date=msg.get("end_date"), limit=366,
        )
    )
    equipment = _equipment(coordinator)
    output = io.StringIO()
    fields = ["date", "household_kwh", "confirmed_heat_pump_heating_kwh",
              "assumed_heat_pump_heating_kwh", "confirmed_ac_heating_kwh",
              "selected_ac_heating_kwh", "estimated_ac_heating_kwh", "space_heating_kwh",
              "dhw_kwh", "heating_and_dhw_kwh", "cooling_kwh",
              "idle_kwh", "other_household_kwh", "unknown_kwh",
              "unallocated_discrepancy_kwh", "pv_kwh", "import_kwh", "export_kwh", "quality"]
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for record in reversed(records):
        energy = attach_canonical(record, equipment)["energy"] or {}
        writer.writerow({"date": record["local_date"], **{
            key: energy.get(key) for key in fields if key not in {"date", "quality"}
        }, "quality": record.get("quality")})
    connection.send_result(msg["id"], {"filename": "home-energy-intelligence.csv",
                                        "csv": output.getvalue()})


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
    connection.send_result(msg["id"], {
        "configured": True,
        "entry_id": coordinator.entry.entry_id,
        "timezone": coordinator.timezone,
        "floor_area_m2": config.get("floor_area_m2"),
        "cycle_hours": config.get("cycle_hours", 6),
        "influx_enabled": bool(config.get("influx_enabled", False)),
        "mapping": mapping,
        "database_size_bytes": await hass.async_add_executor_job(coordinator.database.size_bytes),
    })

"""Small read-only WebSocket API for the dashboard."""

from __future__ import annotations

import voluptuous as vol

from homeassistant.components import websocket_api

from .const import DOMAIN, ENTITY_FIELDS


def async_register_websocket(hass) -> None:
    websocket_api.async_register_command(hass, ws_overview)
    websocket_api.async_register_command(hass, ws_history)
    websocket_api.async_register_command(hass, ws_settings)


def _coordinator(hass):
    entries = hass.config_entries.async_entries(DOMAIN)
    if not entries:
        return None
    return hass.data.get(DOMAIN, {}).get(entries[0].entry_id)


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/overview"})
@websocket_api.async_response
async def ws_overview(hass, connection, msg):
    coordinator = _coordinator(hass)
    if coordinator is None:
        connection.send_result(msg["id"], {"configured": False, "latest": None})
        return
    latest = await hass.async_add_executor_job(coordinator.database.latest)
    connection.send_result(msg["id"], {"configured": True, "latest": latest})


@websocket_api.websocket_command({
    vol.Required("type"): f"{DOMAIN}/history",
    vol.Optional("limit", default=90): vol.All(vol.Coerce(int), vol.Range(min=1, max=3660)),
    vol.Optional("before"): str,
})
@websocket_api.async_response
async def ws_history(hass, connection, msg):
    coordinator = _coordinator(hass)
    if coordinator is None:
        connection.send_result(msg["id"], {"records": []})
        return
    records = await hass.async_add_executor_job(
        lambda: coordinator.database.history(limit=msg["limit"], before=msg.get("before"))
    )
    connection.send_result(msg["id"], {"records": records})


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/settings"})
@websocket_api.async_response
async def ws_settings(hass, connection, msg):
    coordinator = _coordinator(hass)
    if coordinator is None:
        connection.send_result(msg["id"], {"configured": False})
        return
    config = coordinator.config
    mapping = [{"key": key, "label": label, "entity_id": config.get(key)}
               for key, (label, _domains) in ENTITY_FIELDS.items()]
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

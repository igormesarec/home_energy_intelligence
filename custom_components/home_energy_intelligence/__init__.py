"""Home Energy Intelligence integration."""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from pathlib import Path
from zoneinfo import ZoneInfo

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_time_change

from .const import (
    DATABASE_FILENAME, DOMAIN, PLATFORMS, SERVICE_EXPORT_DATABASE,
    SERVICE_REPROCESS_DATE, SERVICE_REPROCESS_RANGE,
)
from .coordinator import HomeEnergyCoordinator
from .database import EnergyDatabase, protected_export_path
from .panel import async_register_panel, async_remove_panel
from .websocket import async_register_websocket

_LOGGER = logging.getLogger(__name__)
DATE_SCHEMA = vol.Schema({vol.Required("date"): cv.date})
RANGE_SCHEMA = vol.Schema({vol.Required("start_date"): cv.date, vol.Required("end_date"): cv.date})


async def async_setup(hass: HomeAssistant, config) -> bool:
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Move V1 entries to the extensible schema without deleting legacy mappings."""
    if entry.version == 1:
        hass.config_entries.async_update_entry(entry, version=2)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    database_path = Path(hass.config.path(".storage", DOMAIN, DATABASE_FILENAME))
    database = EnergyDatabase(database_path)
    recovered = await hass.async_add_executor_job(database.initialize)
    if recovered:
        _LOGGER.error("Recovered from corrupt Home Energy Intelligence DB; preserved at %s", recovered)

    coordinator = HomeEnergyCoordinator(hass, entry, database)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await async_register_panel(hass)
    if not hass.data[DOMAIN].get("websocket_registered"):
        async_register_websocket(hass)
        hass.data[DOMAIN]["websocket_registered"] = True
    if not hass.data[DOMAIN].get("services_registered"):
        _register_services(hass)
        hass.data[DOMAIN]["services_registered"] = True

    async def _six_hour(_now):
        await coordinator.async_process_checkpoint(_now)

    async def _hourly(now):
        end = now.astimezone(ZoneInfo("UTC")).replace(minute=0, second=0, microsecond=0)
        await coordinator.async_process_hour(end - timedelta(hours=1), end)

    async def _daily_close(now):
        local_day = now.astimezone(ZoneInfo(hass.config.time_zone)).date() - timedelta(days=1)
        await coordinator.async_process_day(local_day, reason="05:00_daily_close")

    unsubscribers = [
        async_track_time_change(hass, _hourly, minute=4, second=0),
        async_track_time_change(hass, _six_hour, hour=[0, 6, 12, 18], minute=0, second=0),
        async_track_time_change(hass, _daily_close, hour=5, minute=0, second=0),
        entry.add_update_listener(_async_reload_entry),
    ]
    hass.data[DOMAIN][f"{entry.entry_id}_unsub"] = unsubscribers
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_create_background_task(hass, coordinator.async_catch_up(), "hei_startup_catchup")
    return True


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


def _register_services(hass: HomeAssistant) -> None:
    def active_coordinator():
        entries = hass.config_entries.async_entries(DOMAIN)
        if not entries:
            raise ValueError("Home Energy Intelligence is not configured")
        return hass.data[DOMAIN][entries[0].entry_id]

    async def reprocess(call: ServiceCall):
        day = call.data["date"]
        changed = await active_coordinator().async_process_day(day, reason="manual_reprocess")
        return {"changed": changed, "date": day.isoformat()}

    async def reprocess_range(call: ServiceCall):
        start = call.data["start_date"]
        end = call.data["end_date"]
        if end < start or (end - start).days > 31:
            raise ValueError("Range must be ordered and at most 31 days")
        changed = 0
        day = start
        while day <= end:
            changed += int(await active_coordinator().async_process_day(day, reason="manual_range"))
            day += timedelta(days=1)
        return {"changed_days": changed}

    async def export_database(call: ServiceCall):
        coordinator = active_coordinator()
        if call.context.user_id:
            user = await hass.auth.async_get_user(call.context.user_id)
            if user is None or not user.is_admin:
                raise PermissionError("Database export requires a Home Assistant administrator")
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        destination = protected_export_path(hass.config.config_dir, stamp)
        await hass.async_add_executor_job(coordinator.database.export_to, destination)
        return {"path": str(destination)}

    hass.services.async_register(DOMAIN, SERVICE_REPROCESS_DATE, reprocess,
                                 schema=DATE_SCHEMA, supports_response=SupportsResponse.ONLY)
    hass.services.async_register(DOMAIN, SERVICE_REPROCESS_RANGE, reprocess_range,
                                 schema=RANGE_SCHEMA, supports_response=SupportsResponse.ONLY)
    hass.services.async_register(DOMAIN, SERVICE_EXPORT_DATABASE, export_database,
                                 supports_response=SupportsResponse.ONLY)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        for unsubscribe in hass.data[DOMAIN].pop(f"{entry.entry_id}_unsub", []):
            unsubscribe()
        hass.data[DOMAIN].pop(entry.entry_id, None)
        if not [item for item in hass.config_entries.async_entries(DOMAIN) if item.entry_id != entry.entry_id]:
            async_remove_panel(hass)
    return unloaded

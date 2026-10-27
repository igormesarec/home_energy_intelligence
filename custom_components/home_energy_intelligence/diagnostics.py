"""Privacy-preserving diagnostics."""

from __future__ import annotations

from homeassistant.components.diagnostics import async_redact_data

from .const import DOMAIN

TO_REDACT = {
    "influx_host", "influx_database", "influx_username", "influx_password",
    "influx_mapping_json",
}


async def async_get_config_entry_diagnostics(hass, entry):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    latest = await hass.async_add_executor_job(coordinator.database.latest)
    return {
        "config": async_redact_data({**entry.data, **entry.options}, TO_REDACT),
        "database_size_bytes": await hass.async_add_executor_job(coordinator.database.size_bytes),
        "latest_record": latest,
        "notes": [
            "Recorder and InfluxDB are read-only sources.",
            "No tokens, raw history, or database contents are exported.",
        ],
    }

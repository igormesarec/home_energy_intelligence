"""Constants for Home Energy Intelligence."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "home_energy_intelligence"
NAME: Final = "Home Energy Intelligence"
VERSION: Final = "0.1.0"
PLATFORMS: Final = ["sensor"]

CONF_LOCALE: Final = "locale"
CONF_FLOOR_AREA: Final = "floor_area_m2"
CONF_CYCLE_HOURS: Final = "cycle_hours"
CONF_SEASONAL_FALLBACK: Final = "seasonal_fallback"
CONF_TEMPERATURE_FRESHNESS_MINUTES: Final = "temperature_freshness_minutes"
CONF_INFLUX_ENABLED: Final = "influx_enabled"
CONF_INFLUX_HOST: Final = "influx_host"
CONF_INFLUX_PORT: Final = "influx_port"
CONF_INFLUX_DATABASE: Final = "influx_database"
CONF_INFLUX_USERNAME: Final = "influx_username"
CONF_INFLUX_PASSWORD: Final = "influx_password"
CONF_INFLUX_MAPPING: Final = "influx_mapping_json"

DEFAULT_LOCALE: Final = "en"
DEFAULT_FLOOR_AREA: Final = 180.0
DEFAULT_CYCLE_HOURS: Final = 6
DEFAULT_TEMPERATURE_FRESHNESS_MINUTES: Final = 0
DATABASE_FILENAME: Final = "home_energy_intelligence.sqlite3"
PANEL_URL: Final = "home-energy-intelligence"
PANEL_TITLE: Final = "Home Energy Intelligence"
PANEL_ICON: Final = "mdi:home-lightning-bolt-outline"

ENTITY_FIELDS: Final[dict[str, tuple[str, tuple[str, ...]]]] = {
    "ecodan_energy": ("Ecodan electricity energy", ("sensor",)),
    "ecodan_power": ("Ecodan electricity power", ("sensor",)),
    "ecodan_mode": ("Ecodan operation mode", ("sensor",)),
    "ecodan_dhw": ("Ecodan DHW tank", ("water_heater",)),
    "ac_1_energy": ("AC 1 electricity energy", ("sensor",)),
    "ac_1_power": ("AC 1 electricity power", ("sensor",)),
    "ac_1_climate": ("AC 1 climate", ("climate",)),
    "ac_1_temperature": ("AC 1 room temperature", ("sensor",)),
    "ac_2_energy": ("AC 2 electricity energy", ("sensor",)),
    "ac_2_power": ("AC 2 electricity power", ("sensor",)),
    "ac_2_climate": ("AC 2 climate", ("climate",)),
    "ac_2_temperature": ("AC 2 room temperature", ("sensor",)),
    "ac_3_energy": ("AC 3 electricity energy", ("sensor",)),
    "ac_3_power": ("AC 3 electricity power", ("sensor",)),
    "ac_3_climate": ("AC 3 climate", ("climate",)),
    "pv_energy": ("PV generation energy", ("sensor",)),
    "grid_import": ("Grid imported energy", ("sensor",)),
    "grid_export": ("Grid exported energy", ("sensor",)),
    "outdoor_temperature": ("Outdoor temperature", ("sensor",)),
    "flow_temperature": ("Heating supply temperature", ("sensor",)),
    "representative_room_temperature": ("Representative room temperature", ("sensor",)),
    "weather": ("Weather forecast entity", ("weather",)),
}

REQUIRED_ENTITY_FIELDS: Final = {
    "ecodan_energy",
    "ecodan_power",
    "ecodan_mode",
    "ac_1_energy",
    "ac_1_power",
    "ac_1_climate",
    "ac_2_energy",
    "ac_2_power",
    "ac_2_climate",
    "ac_3_energy",
    "ac_3_power",
    "ac_3_climate",
    "pv_energy",
    "grid_import",
    "grid_export",
    "outdoor_temperature",
    "flow_temperature",
}

SERVICE_REPROCESS_DATE: Final = "reprocess_date"
SERVICE_REPROCESS_RANGE: Final = "reprocess_range"
SERVICE_EXPORT_DATABASE: Final = "export_database"

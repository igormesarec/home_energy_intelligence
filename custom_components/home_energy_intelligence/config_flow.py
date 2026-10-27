"""UI-only configuration for Home Energy Intelligence."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_CYCLE_HOURS, CONF_FLOOR_AREA, CONF_INFLUX_DATABASE, CONF_INFLUX_ENABLED,
    CONF_INFLUX_HOST, CONF_INFLUX_MAPPING, CONF_INFLUX_PASSWORD, CONF_INFLUX_PORT, CONF_INFLUX_USERNAME,
    CONF_LOCALE, CONF_SEASONAL_FALLBACK, CONF_TEMPERATURE_FRESHNESS_MINUTES,
    DEFAULT_CYCLE_HOURS, DEFAULT_FLOOR_AREA, DEFAULT_LOCALE,
    DEFAULT_TEMPERATURE_FRESHNESS_MINUTES, DOMAIN, ENTITY_FIELDS,
    REQUIRED_ENTITY_FIELDS,
)


def _entity_schema(values: dict[str, Any] | None = None) -> vol.Schema:
    values = values or {}
    fields: dict[Any, Any] = {}
    for key, (_label, domains) in ENTITY_FIELDS.items():
        marker_type = vol.Required if key in REQUIRED_ENTITY_FIELDS else vol.Optional
        description = {"suggested_value": values[key]} if key in values else None
        marker = marker_type(key, description=description)
        fields[marker] = selector.EntitySelector(selector.EntitySelectorConfig(domain=list(domains)))
    return vol.Schema(fields)


def _general_schema(values: dict[str, Any] | None = None) -> vol.Schema:
    values = values or {}
    return vol.Schema({
        vol.Required(CONF_LOCALE, default=values.get(CONF_LOCALE, DEFAULT_LOCALE)):
            selector.SelectSelector(selector.SelectSelectorConfig(options=["en", "sl"])),
        vol.Required(CONF_FLOOR_AREA, default=values.get(CONF_FLOOR_AREA, DEFAULT_FLOOR_AREA)):
            selector.NumberSelector(selector.NumberSelectorConfig(min=1, max=5000, step=1, unit_of_measurement="m²")),
        vol.Required(CONF_CYCLE_HOURS, default=values.get(CONF_CYCLE_HOURS, DEFAULT_CYCLE_HOURS)):
            selector.NumberSelector(selector.NumberSelectorConfig(min=6, max=6, step=1, unit_of_measurement="h")),
        vol.Required(CONF_TEMPERATURE_FRESHNESS_MINUTES,
                     default=values.get(CONF_TEMPERATURE_FRESHNESS_MINUTES, DEFAULT_TEMPERATURE_FRESHNESS_MINUTES)):
            selector.NumberSelector(selector.NumberSelectorConfig(min=0, max=1440, step=5, unit_of_measurement="min")),
        vol.Required(CONF_SEASONAL_FALLBACK, default=values.get(CONF_SEASONAL_FALLBACK, False)):
            selector.BooleanSelector(),
    })


def _has_duplicate_meters(values: dict[str, Any]) -> bool:
    keys = ["ecodan_energy", "ac_1_energy", "ac_2_energy", "ac_3_energy",
            "pv_energy", "grid_import", "grid_export"]
    selected = [values[key] for key in keys if values.get(key)]
    return len(selected) != len(set(selected))


def _options_schema(values: dict[str, Any]) -> vol.Schema:
    schema = dict(_general_schema(values).schema)
    schema.update(_entity_schema(values).schema)
    schema.update({
        vol.Required(CONF_INFLUX_ENABLED, default=values.get(CONF_INFLUX_ENABLED, False)): selector.BooleanSelector(),
        vol.Optional(CONF_INFLUX_HOST, description={"suggested_value": values.get(CONF_INFLUX_HOST, "")}): str,
        vol.Optional(CONF_INFLUX_PORT, default=values.get(CONF_INFLUX_PORT, 8086)): int,
        vol.Optional(CONF_INFLUX_DATABASE, description={"suggested_value": values.get(CONF_INFLUX_DATABASE, "")}): str,
        vol.Optional(CONF_INFLUX_USERNAME, description={"suggested_value": values.get(CONF_INFLUX_USERNAME, "")}): str,
        vol.Optional(CONF_INFLUX_PASSWORD): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        ),
        vol.Optional(CONF_INFLUX_MAPPING, description={"suggested_value": values.get(CONF_INFLUX_MAPPING, "{}")}):
            selector.TextSelector(selector.TextSelectorConfig(multiline=True)),
    })
    return vol.Schema(schema)


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._general: dict[str, Any] = {}

    async def async_step_user(self, user_input=None):
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        if user_input is not None:
            self._general = dict(user_input)
            return await self.async_step_entities()
        return self.async_show_form(step_id="user", data_schema=_general_schema())

    async def async_step_entities(self, user_input=None):
        if user_input is not None:
            if _has_duplicate_meters(user_input):
                return self.async_show_form(
                    step_id="entities", data_schema=_entity_schema(user_input),
                    errors={"base": "duplicate_meter"},
                )
            return self.async_create_entry(
                title="Home Energy Intelligence",
                data={**self._general, **user_input},
            )
        return self.async_show_form(step_id="entities", data_schema=_entity_schema())

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return OptionsFlow()


class OptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input=None):
        current = {**self.config_entry.data, **self.config_entry.options}
        if user_input is not None:
            if _has_duplicate_meters(user_input):
                return self.async_show_form(step_id="init", data_schema=_options_schema(user_input),
                                            errors={"base": "duplicate_meter"})
            if CONF_INFLUX_PASSWORD not in user_input and current.get(CONF_INFLUX_PASSWORD):
                user_input[CONF_INFLUX_PASSWORD] = current[CONF_INFLUX_PASSWORD]
            return self.async_create_entry(title="", data=user_input)
        return self.async_show_form(step_id="init", data_schema=_options_schema(current))

"""V2 guided configuration flow for all source mappings."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
import uuid

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .configuration import EquipmentMapping, parse_json_records, validate_no_duplicate_meters
from .const import DOMAIN
from .allocation import VALID_AC_CATEGORIES, VALID_HEAT_PUMP_CATEGORIES

ENERGY_UNITS = {"Wh", "kWh", "MWh"}
POWER_UNITS = {"W", "kW"}
TEMPERATURE_UNITS = {"°C", "°F"}
DEFAULT_HP_MAP = {"heating_zones": "confirmed_heating", "heating_water": "dhw", "cooling": "cooling", "idle": "idle"}
DEFAULT_AC_MAP = {"heating": "heating", "cooling": "cooling", "idle": "standby", "off": "standby"}


def _entity(domains, device_class=None):
    if isinstance(domains, str): domains = [domains]
    config = {"domain": domains}
    if device_class: config["device_class"] = device_class
    return selector.EntitySelector(selector.EntitySelectorConfig(**config))


def _general(values):
    return vol.Schema({
        vol.Required("locale", default=values.get("locale", "en")): selector.SelectSelector(selector.SelectSelectorConfig(options=["en", "sl"])),
        vol.Required("floor_area_m2", default=values.get("floor_area_m2", 180)): selector.NumberSelector(selector.NumberSelectorConfig(min=1, max=5000, step=1, unit_of_measurement="m²")),
    })


def _equipment(kind, values=None, allow_skip=False):
    values = values or {}
    schema = {
        vol.Required("configure", default=bool(values) or not allow_skip): selector.BooleanSelector(),
        vol.Optional("equipment_id", default=values.get("equipment_id", uuid.uuid4().hex[:12])): str,
        vol.Optional("display_name", default=values.get("display_name", "Heat pump" if kind == "heat_pump" else "Air conditioner")): str,
        vol.Optional("enabled", default=values.get("enabled", True)): selector.BooleanSelector(),
        vol.Optional("energy_entity", default=values.get("energy_entity")): _entity("sensor", "energy"),
        vol.Optional("power_entity", default=values.get("power_entity")): _entity("sensor", "power"),
        vol.Optional("state_entity", default=values.get("state_entity")): _entity(["sensor", "climate", "water_heater"]),
        vol.Optional("state_attribute", default=values.get("state_attribute")): str,
        vol.Optional("room_temperature_entity", default=values.get("room_temperature_entity")): _entity(["sensor", "climate"], "temperature"),
        vol.Optional("supply_temperature_entity", default=values.get("supply_temperature_entity")): _entity("sensor", "temperature"),
        vol.Optional("return_temperature_entity", default=values.get("return_temperature_entity")): _entity("sensor", "temperature"),
        vol.Optional("state_map", default=json.dumps(values.get("state_map") or (DEFAULT_HP_MAP if kind == "heat_pump" else DEFAULT_AC_MAP))): selector.TextSelector(selector.TextSelectorConfig(multiline=True)),
        vol.Required("add_another", default=False): selector.BooleanSelector(),
    }
    if values.get("equipment_id"):
        schema[vol.Required("action", default="update")] = selector.SelectSelector(
            selector.SelectSelectorConfig(options=["update", "disable", "remove"])
        )
    return vol.Schema(schema)


def _temperature(values=None):
    values = values or {}; quality = values.get("quality", {})
    return vol.Schema({
        vol.Required("configure", default=True): selector.BooleanSelector(),
        vol.Optional("source_id", default=values.get("source_id", uuid.uuid4().hex[:12])): str,
        vol.Optional("display_name", default=values.get("display_name", "Temperature")): str,
        vol.Optional("role", default=values.get("role", "indoor")): selector.SelectSelector(selector.SelectSelectorConfig(options=["outdoor", "indoor", "supply", "return", "dhw", "equipment_room"])),
        vol.Optional("entity_id", default=values.get("entity_id")): _entity(["sensor", "climate", "water_heater"]),
        vol.Optional("attribute", default=values.get("attribute")): str,
        vol.Optional("enabled", default=values.get("enabled", True)): selector.BooleanSelector(),
        vol.Optional("minimum", default=quality.get("minimum", -50)): vol.Coerce(float),
        vol.Optional("maximum", default=quality.get("maximum", 60)): vol.Coerce(float),
        vol.Optional("invalid_values", default=",".join(map(str, quality.get("invalid_values", [-55, 155])))): str,
        vol.Optional("maximum_change_per_minute", default=quality.get("maximum_change_per_minute", 8)): vol.Coerce(float),
        vol.Optional("freshness_minutes", default=quality.get("freshness_minutes", 360)): vol.Coerce(int),
        vol.Optional("missing_after_minutes", default=quality.get("missing_after_minutes", 360)): vol.Coerce(int),
        vol.Required("add_another", default=False): selector.BooleanSelector(),
        **({vol.Required("action", default="update"): selector.SelectSelector(
            selector.SelectSelectorConfig(options=["update", "disable", "remove"])
        )} if values.get("source_id") else {}),
    })


def _site(values):
    return vol.Schema({
        vol.Optional("pv", default=values.get("pv")): _entity("sensor", "energy"),
        vol.Optional("import", default=values.get("import")): _entity("sensor", "energy"),
        vol.Optional("export", default=values.get("export")): _entity("sensor", "energy"),
    })


def _sources(values):
    return vol.Schema({
        vol.Required("historical_source", default=values.get("historical_source", "recorder")): selector.SelectSelector(selector.SelectSelectorConfig(options=["recorder", "recorder_with_influx_backfill"])),
        vol.Required("influx_enabled", default=values.get("influx_enabled", False)): selector.BooleanSelector(),
        vol.Optional("influx_host", description={"suggested_value": values.get("influx_host", "")}): str,
        vol.Optional("influx_port", default=values.get("influx_port", 8086)): int,
        vol.Optional("influx_use_ssl", default=values.get("influx_use_ssl", False)): selector.BooleanSelector(),
        vol.Optional("influx_verify_ssl", default=values.get("influx_verify_ssl", True)): selector.BooleanSelector(),
        vol.Optional("influx_database", default=values.get("influx_database", "homeassistant")): str,
        vol.Optional("influx_retention_policy", default=values.get("influx_retention_policy", "autogen")): str,
        vol.Optional("influx_username", description={"suggested_value": values.get("influx_username", "")}): str,
        vol.Optional("influx_password"): selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)),
        vol.Optional("influx_mapping_json", default=values.get("influx_mapping_json", "{}")): selector.TextSelector(selector.TextSelectorConfig(multiline=True)),
    })


def _check_source(hass, entity_id, expected):
    if not entity_id: return None
    state = hass.states.get(entity_id)
    if state is None: return "source_unavailable"
    unit = state.attributes.get("unit_of_measurement")
    state_class = state.attributes.get("state_class")
    device_class = state.attributes.get("device_class")
    if expected == "energy" and (
        unit not in ENERGY_UNITS or device_class != "energy"
        or state_class not in {"total", "total_increasing"}
    ): return "incompatible_energy_source"
    if expected == "power" and (unit not in POWER_UNITS or device_class != "power"):
        return "incompatible_power_source"
    if expected == "temperature" and (
        unit not in TEMPERATURE_UNITS or device_class != "temperature"
    ): return "incompatible_temperature_source"
    return None


def _circuit_2k(hass, entity_id):
    if not entity_id: return False
    state = hass.states.get(entity_id)
    text = f"{entity_id} {state.attributes.get('friendly_name', '') if state else ''}".lower().replace(" ", "_")
    return any(token in text for token in ("circuit_2k", "circuit_2_k", "krog_2k"))


class _Wizard:
    def _load(self, values):
        self.result = dict(values)
        self.equipment = parse_json_records(self.result.get("equipment"))
        self.temperatures = parse_json_records(self.result.get("temperature_sources"))
        self._editing = False
        self._equipment_pending: dict[str, list[dict[str, Any]]] = {}
        self._temperature_pending: list[dict[str, Any]] | None = None

    async def async_step_general(self, user_input=None):
        if user_input is not None:
            self.result.update(user_input)
            if not self._editing:
                self.equipment = []
                self.temperatures = []
            return await self.async_step_heat_pumps()
        return self.async_show_form(step_id="general", data_schema=_general(self.result))

    def _accept_equipment(self, kind, values):
        if not values.get("configure"): return {}
        errors = {}
        for key, expected in (("energy_entity", "energy"), ("power_entity", "power")):
            if key == "energy_entity" and not values.get(key): errors[key] = "required_source"
            elif error := _check_source(self.hass, values.get(key), expected): errors[key] = error
        if not values.get("state_entity"): errors["state_entity"] = "required_source"
        elif self.hass.states.get(values["state_entity"]) is None: errors["state_entity"] = "source_unavailable"
        for key in ("supply_temperature_entity", "return_temperature_entity"):
            if _circuit_2k(self.hass, values.get(key)): errors[key] = "circuit_2k_forbidden"
        for key in ("room_temperature_entity", "supply_temperature_entity", "return_temperature_entity"):
            if key not in errors and (error := _check_source(self.hass, values.get(key), "temperature")):
                errors[key] = error
        try: state_map = json.loads(values.get("state_map", "{}"))
        except json.JSONDecodeError: errors["state_map"] = "invalid_json"; state_map = {}
        allowed = VALID_HEAT_PUMP_CATEGORIES if kind == "heat_pump" else VALID_AC_CATEGORIES
        if not isinstance(state_map, dict) or not set(state_map.values()).issubset(allowed):
            errors["state_map"] = "invalid_state_mapping"
        if not errors:
            record = {k: v for k, v in values.items() if k not in {"configure", "add_another", "state_map"} and v not in (None, "")}
            record.update(kind=kind, state_map=state_map); self.equipment.append(record)
        return errors

    async def _equipment_step(self, step_id, kind, user_input, allow_skip):
        if self._editing:
            if kind not in self._equipment_pending:
                self._equipment_pending[kind] = [item for item in self.equipment if item.get("kind") == kind]
                self.equipment = [item for item in self.equipment if item.get("kind") != kind]
            pending = self._equipment_pending[kind]
            current = pending[0] if pending else None
            if user_input is not None:
                action = user_input.pop("action", "update")
                add_another = bool(user_input.get("add_another"))
                if current is not None:
                    pending.pop(0)
                    user_input["equipment_id"] = current["equipment_id"]
                if action != "remove":
                    if action == "disable":
                        user_input["enabled"] = False
                    errors = self._accept_equipment(kind, user_input)
                    if errors:
                        if current is not None:
                            pending.insert(0, current)
                        return self.async_show_form(
                            step_id=step_id, data_schema=_equipment(kind, user_input, allow_skip),
                            errors=errors,
                        )
                if pending:
                    return self.async_show_form(
                        step_id=step_id, data_schema=_equipment(kind, pending[0], allow_skip)
                    )
                if add_another:
                    return self.async_show_form(
                        step_id=step_id, data_schema=_equipment(kind, allow_skip=allow_skip)
                    )
                return await (self.async_step_air_conditioners() if kind == "heat_pump" else self.async_step_solar_grid())
            if current is not None:
                return self.async_show_form(
                    step_id=step_id, data_schema=_equipment(kind, current, allow_skip)
                )
        if user_input is not None:
            errors = self._accept_equipment(kind, user_input)
            if errors: return self.async_show_form(step_id=step_id, data_schema=_equipment(kind, user_input, allow_skip), errors=errors)
            if user_input.get("add_another"): return await self._equipment_step(step_id, kind, None, allow_skip)
            return await (self.async_step_air_conditioners() if kind == "heat_pump" else self.async_step_solar_grid())
        return self.async_show_form(step_id=step_id, data_schema=_equipment(kind, allow_skip=allow_skip))

    async def async_step_heat_pumps(self, user_input=None): return await self._equipment_step("heat_pumps", "heat_pump", user_input, False)
    async def async_step_air_conditioners(self, user_input=None): return await self._equipment_step("air_conditioners", "air_conditioner", user_input, True)

    async def async_step_solar_grid(self, user_input=None):
        values = self.result.get("site_sources", {})
        if isinstance(values, str): values = json.loads(values)
        if user_input is not None:
            errors = {k: e for k in ("pv", "import", "export") if (e := _check_source(self.hass, user_input.get(k), "energy"))}
            if not validate_no_duplicate_meters([EquipmentMapping(**item) for item in self.equipment], user_input): errors["base"] = "duplicate_meter"
            if errors: return self.async_show_form(step_id="solar_grid", data_schema=_site(user_input), errors=errors)
            self.result["site_sources"] = user_input
            return await self.async_step_temperatures()
        return self.async_show_form(step_id="solar_grid", data_schema=_site(values))

    async def async_step_temperatures(self, user_input=None):
        if self._editing:
            if self._temperature_pending is None:
                self._temperature_pending = list(self.temperatures)
                self.temperatures = []
            current = self._temperature_pending[0] if self._temperature_pending else None
            if user_input is None and current is not None:
                return self.async_show_form(step_id="temperatures", data_schema=_temperature(current))
            if user_input is not None:
                action = user_input.pop("action", "update")
                add_another = bool(user_input.get("add_another"))
                if current is not None:
                    self._temperature_pending.pop(0)
                    user_input["source_id"] = current["source_id"]
                if action == "remove":
                    if self._temperature_pending:
                        return self.async_show_form(
                            step_id="temperatures", data_schema=_temperature(self._temperature_pending[0])
                        )
                    return await (self.async_step_temperatures() if add_another else self.async_step_weather())
                if action == "disable":
                    user_input["enabled"] = False
        if user_input is not None:
            if not user_input.get("configure"): return await self.async_step_weather()
            errors = {}
            if error := _check_source(self.hass, user_input.get("entity_id"), "temperature"): errors["entity_id"] = error
            if user_input.get("role") in {"supply", "return"} and _circuit_2k(self.hass, user_input.get("entity_id")): errors["entity_id"] = "circuit_2k_forbidden"
            if user_input.get("minimum", 0) >= user_input.get("maximum", 0): errors["base"] = "invalid_temperature_limits"
            if errors:
                if self._editing and current is not None:
                    self._temperature_pending.insert(0, current)
                return self.async_show_form(step_id="temperatures", data_schema=_temperature(user_input), errors=errors)
            add_another = user_input.pop("add_another"); user_input.pop("configure")
            invalid = tuple(float(x.strip()) for x in user_input.pop("invalid_values").split(",") if x.strip())
            quality_keys = ("minimum", "maximum", "maximum_change_per_minute", "freshness_minutes", "missing_after_minutes")
            quality = {k: user_input.pop(k) for k in quality_keys}; quality["invalid_values"] = invalid
            user_input["quality"] = quality; self.temperatures.append(user_input)
            if self._editing and self._temperature_pending:
                return self.async_show_form(
                    step_id="temperatures", data_schema=_temperature(self._temperature_pending[0])
                )
            return await (self.async_step_temperatures() if add_another else self.async_step_weather())
        return self.async_show_form(step_id="temperatures", data_schema=_temperature())

    async def async_step_weather(self, user_input=None):
        if user_input is not None: self.result.update(user_input); return await self.async_step_data_sources()
        return self.async_show_form(step_id="weather", data_schema=vol.Schema({vol.Optional("weather_entity", description={"suggested_value": self.result.get("weather_entity")}): _entity("weather")}))

    async def async_step_data_sources(self, user_input=None):
        if user_input is not None:
            errors = {}
            if user_input.get("influx_enabled") and (not user_input.get("influx_host") or not user_input.get("influx_database")): errors["base"] = "influx_incomplete"
            try: json.loads(user_input.get("influx_mapping_json", "{}"))
            except json.JSONDecodeError: errors["influx_mapping_json"] = "invalid_json"
            if errors: return self.async_show_form(step_id="data_sources", data_schema=_sources(user_input), errors=errors)
            self.result.update(user_input); return await self.async_step_data_quality()
        return self.async_show_form(step_id="data_sources", data_schema=_sources(self.result))

    async def async_step_data_quality(self, user_input=None):
        if user_input is not None:
            if not any(item.get("kind") == "heat_pump" for item in self.equipment):
                return self.async_show_form(
                    step_id="data_quality", data_schema=self._data_quality_schema(),
                    errors={"base": "heat_pump_required"},
                )
            self.result.update(user_input); self.result["equipment"] = json.dumps(self.equipment); self.result["temperature_sources"] = json.dumps(self.temperatures)
            self.result["mapping_effective_at"] = datetime.now(UTC).isoformat()
            return self._finish(self.result)
        return self.async_show_form(step_id="data_quality", data_schema=self._data_quality_schema())

    def _data_quality_schema(self):
        return vol.Schema({
            vol.Required("maximum_power_gap_minutes", default=self.result.get("maximum_power_gap_minutes", 30)): vol.Coerce(int),
            vol.Required("counter_jump_limit_kwh", default=self.result.get("counter_jump_limit_kwh", 500)): vol.Coerce(float),
            vol.Required("assumed_heating_reporting", default=self.result.get("assumed_heating_reporting", "separate")): selector.SelectSelector(selector.SelectSelectorConfig(options=["separate", "include_in_heating_total"])),
        })


class ConfigFlow(_Wizard, config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 2
    def __init__(self): self._load({})
    async def async_step_user(self, user_input=None):
        if self._async_current_entries(): return self.async_abort(reason="single_instance_allowed")
        return await self.async_step_general(user_input)
    def _finish(self, data): return self.async_create_entry(title="Home Energy Intelligence", data=data)
    @staticmethod
    @callback
    def async_get_options_flow(config_entry): return OptionsFlow(config_entry)


class OptionsFlow(_Wizard, config_entries.OptionsFlow):
    def __init__(self, config_entry):
        self.config_entry = config_entry
        self._load({**config_entry.data, **config_entry.options})
        self._editing = True
    async def async_step_init(self, user_input=None): return await self.async_step_general(user_input)
    def _finish(self, data):
        current = {**self.config_entry.data, **self.config_entry.options}
        if current.get("influx_password") and not data.get("influx_password"): data["influx_password"] = current["influx_password"]
        return self.async_create_entry(title="", data=data)

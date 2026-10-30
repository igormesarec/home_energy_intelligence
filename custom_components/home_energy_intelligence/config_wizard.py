"""V2 guided configuration flow for all source mappings."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from typing import Any
import uuid

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .configuration import EquipmentMapping, parse_json_records, validate_no_duplicate_meters
from .const import DOMAIN
from .allocation import VALID_AC_CATEGORIES, VALID_HEAT_PUMP_CATEGORIES
from .units import IncompatibleUnitError, normalize_value

ENERGY_UNITS = {"Wh", "kWh", "MWh"}
POWER_UNITS = {"W", "kW"}
TEMPERATURE_UNITS = {"°C", "°F"}
DEFAULT_HP_MAP = {"heating_zones": "confirmed_heating", "heating_water": "dhw", "cooling": "cooling", "idle": "idle"}
DEFAULT_AC_MAP = {"heating": "heating", "cooling": "cooling", "idle": "standby", "off": "standby"}
MONTHS = {
    "en": ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"),
    "sl": ("januar", "februar", "marec", "april", "maj", "junij", "julij", "avgust", "september", "oktober", "november", "december"),
}


def _optional(name: str, value: Any = None, **kwargs):
    """Return an optional key without manufacturing an invalid None default."""
    if value is None:
        return vol.Optional(name, **kwargs)
    return vol.Optional(name, default=value, **kwargs)


def _entity(domains, device_class=None):
    if isinstance(domains, str): domains = [domains]
    config = {"domain": domains}
    if device_class: config["device_class"] = device_class
    return selector.EntitySelector(selector.EntitySelectorConfig(**config))


def _general(values):
    locale = values.get("locale", "en")
    month_names = MONTHS.get(locale, MONTHS["en"])
    month_options = [
        {"value": str(month), "label": label}
        for month, label in enumerate(month_names, start=1)
    ]
    return vol.Schema({
        vol.Required("locale", default=values.get("locale", "en")): selector.SelectSelector(selector.SelectSelectorConfig(options=["en", "sl"])),
        vol.Required("floor_area_m2", default=values.get("floor_area_m2", 180)): selector.NumberSelector(selector.NumberSelectorConfig(min=1, max=5000, step=1, unit_of_measurement="m²")),
        vol.Required("energy_season_start_month", default=str(values.get("energy_season_start_month", 7))): selector.SelectSelector(selector.SelectSelectorConfig(options=month_options)),
        vol.Required("energy_season_start_day", default=values.get("energy_season_start_day", 1)): selector.NumberSelector(selector.NumberSelectorConfig(min=1, max=28, step=1, mode=selector.NumberSelectorMode.BOX)),
    })


def _season_preview(values: dict[str, Any]) -> str:
    locale = values.get("locale", "en")
    months = MONTHS.get(locale, MONTHS["en"])
    month = int(values.get("energy_season_start_month", 7))
    day = int(values.get("energy_season_start_day", 1))
    start = date(2026, month, day)
    end = date(2027, month, day) - timedelta(days=1)
    if locale == "sl":
        return f"{start.day}. {months[start.month - 1]} – {end.day}. {months[end.month - 1]}"
    return f"{months[start.month - 1]} {start.day} – {months[end.month - 1]} {end.day}"


def _equipment(kind, values=None, allow_skip=False):
    values = values or {}
    schema = {
        vol.Required("configure", default=bool(values) or not allow_skip): selector.BooleanSelector(),
        vol.Optional("equipment_id", default=values.get("equipment_id", uuid.uuid4().hex[:12])): str,
        vol.Optional("display_name", default=values.get("display_name", "Heat pump" if kind == "heat_pump" else "Air conditioner")): str,
        vol.Optional("enabled", default=values.get("enabled", True)): selector.BooleanSelector(),
        _optional("energy_entity", values.get("energy_entity")): _entity("sensor", "energy"),
        _optional("power_entity", values.get("power_entity")): _entity("sensor", "power"),
        _optional("state_entity", values.get("state_entity")): _entity(["sensor", "climate", "water_heater"]),
        _optional("state_attribute", values.get("state_attribute")): str,
        _optional("room_temperature_entity", values.get("room_temperature_entity")): _entity(["sensor", "climate"], "temperature"),
        _optional("supply_temperature_entity", values.get("supply_temperature_entity")): _entity("sensor", "temperature"),
        _optional("return_temperature_entity", values.get("return_temperature_entity")): _entity("sensor", "temperature"),
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
        _optional("entity_id", values.get("entity_id")): _entity(["sensor", "climate", "water_heater"]),
        _optional("attribute", values.get("attribute")): str,
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
        _optional("pv", values.get("pv")): _entity("sensor", "energy"),
        _optional("import", values.get("import")): _entity("sensor", "energy"),
        _optional("export", values.get("export")): _entity("sensor", "energy"),
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


def _check_source(hass, entity_id, expected, *, attribute=None):
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
    if expected == "temperature":
        inferred_attribute = attribute
        if inferred_attribute is None and str(entity_id).startswith("climate."):
            inferred_attribute = "current_temperature"
        if inferred_attribute:
            try:
                float(state.attributes.get(inferred_attribute))
            except (TypeError, ValueError):
                return "incompatible_temperature_source"
            unit = unit or state.attributes.get("temperature_unit")
        if unit not in TEMPERATURE_UNITS:
            return "incompatible_temperature_source"
        if not inferred_attribute and device_class != "temperature":
            return "incompatible_temperature_source"
    return None


def _circuit_2k(hass, entity_id):
    if not entity_id: return False
    state = hass.states.get(entity_id)
    text = f"{entity_id} {state.attributes.get('friendly_name', '') if state else ''}".lower().replace(" ", "_")
    return any(token in text for token in ("circuit_2k", "circuit_2_k", "krog_2k"))


class _Wizard:
    def _load(self, values):
        # Options Flow edits an isolated draft. Home Assistant receives no
        # mutation until _finish() creates the validated options entry.
        self._original = deepcopy(dict(values))
        self.result = deepcopy(dict(values))
        self.equipment = parse_json_records(self.result.get("equipment"))
        self.temperatures = parse_json_records(self.result.get("temperature_sources"))
        self._editing = False
        self._equipment_pending: dict[str, list[dict[str, Any]]] = {}
        self._temperature_pending: list[dict[str, Any]] | None = None

    async def async_step_general(self, user_input=None):
        if user_input is not None:
            user_input["energy_season_start_month"] = int(
                user_input.get(
                    "energy_season_start_month",
                    self.result.get("energy_season_start_month", 7),
                )
            )
            user_input.setdefault(
                "energy_season_start_day",
                self.result.get("energy_season_start_day", 1),
            )
            self.result.update(user_input)
            if not self._editing:
                self.equipment = []
                self.temperatures = []
            return await self.async_step_heat_pumps()
        display_values = dict(self.result)
        display_values.setdefault(
            "locale",
            self.hass.config.language if self.hass.config.language in MONTHS else "en",
        )
        return self.async_show_form(
            step_id="general", data_schema=_general(display_values),
            description_placeholders={
                "energy_season_preview": _season_preview(display_values)
            },
        )

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
                if not user_input.get("configure"):
                    # Skipping means "leave this section unchanged". Deletion
                    # is only the explicit remove action.
                    self.equipment.extend(deepcopy(pending))
                    pending.clear()
                    return await (
                        self.async_step_air_conditioners()
                        if kind == "heat_pump" else self.async_step_solar_grid()
                    )
                action = user_input.pop("action", "update")
                add_another = bool(user_input.get("add_another"))
                if current is not None:
                    pending.pop(0)
                    user_input["equipment_id"] = current["equipment_id"]
                if action != "remove":
                    if action == "disable":
                        disabled = deepcopy(current or user_input)
                        disabled["enabled"] = False
                        disabled["kind"] = kind
                        self.equipment.append(disabled)
                        errors = {}
                    else:
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
                if not user_input.get("configure"):
                    # Preserve every queued mapping on a skipped page.
                    self.temperatures.extend(deepcopy(self._temperature_pending))
                    self._temperature_pending.clear()
                    return await self.async_step_weather()
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
                    disabled = deepcopy(current or user_input)
                    disabled["enabled"] = False
                    self.temperatures.append(disabled)
                    if self._temperature_pending:
                        return self.async_show_form(
                            step_id="temperatures",
                            data_schema=_temperature(self._temperature_pending[0]),
                        )
                    return await (
                        self.async_step_temperatures() if add_another
                        else self.async_step_weather()
                    )
        if user_input is not None:
            if not user_input.get("configure"): return await self.async_step_weather()
            errors = {}
            if error := _check_source(
                self.hass, user_input.get("entity_id"), "temperature",
                attribute=user_input.get("attribute"),
            ): errors["entity_id"] = error
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
        return self.async_show_form(step_id="weather", data_schema=vol.Schema({
            _optional("weather_entity", self.result.get("weather_entity")): _entity("weather")
        }))

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
            values = {**self.result, **user_input}
            errors = self._representative_errors(values)
            if user_input.get("test_representative_sources") or errors:
                return self.async_show_form(
                    step_id="data_quality", data_schema=self._data_quality_schema(values),
                    errors=errors,
                    description_placeholders={
                        "representative_preview": self._representative_preview(values)
                    },
                )
            values.pop("test_representative_sources", None)
            for key in (
                "representative_indoor_source_id", "representative_outdoor_source_id",
            ):
                if values.get(key) == "__none__":
                    values.pop(key, None)
                    self.result.pop(key, None)
            self.result.update(values); self.result["equipment"] = json.dumps(self.equipment); self.result["temperature_sources"] = json.dumps(self.temperatures)
            self.result["mapping_effective_at"] = datetime.now(UTC).isoformat()
            return self._finish(self.result)
        return self.async_show_form(
            step_id="data_quality", data_schema=self._data_quality_schema(),
            description_placeholders={
                "representative_preview": self._representative_preview(self.result)
            },
        )

    def _representative_candidates(self):
        candidates = list(self.temperatures)
        candidates.extend({
            "source_id": f"{item['equipment_id']}_equipment_room",
            "display_name": f"{item.get('display_name', item['equipment_id'])} room",
            "role": "equipment_room", "enabled": item.get("enabled", True),
            "entity_id": item.get("room_temperature_entity"),
            "quality": {
                "minimum": -10, "maximum": 60, "invalid_values": [-55, 155],
            },
        } for item in self.equipment if item.get("enabled", True)
          and item.get("room_temperature_entity"))
        return [item for item in candidates if item.get("enabled", True)]

    def _representative_errors(self, values):
        candidates = {item["source_id"]: item for item in self._representative_candidates()}
        errors = {}
        for role, accepted_roles in (
            ("indoor", {"indoor", "equipment_room"}), ("outdoor", {"outdoor"}),
        ):
            key = f"representative_{role}_source_id"
            source_id = values.get(key)
            if not source_id or source_id == "__none__":
                continue
            source = candidates.get(source_id)
            if source is None or source.get("role") not in accepted_roles:
                errors[key] = "invalid_representative_source"
                continue
            if error := _check_source(
                self.hass, source.get("entity_id"), "temperature",
                attribute=source.get("attribute"),
            ):
                errors[key] = error
        return errors

    def _representative_preview(self, values):
        candidates = {item["source_id"]: item for item in self._representative_candidates()}
        previews = []
        for role in ("indoor", "outdoor"):
            source_id = values.get(f"representative_{role}_source_id")
            source = candidates.get(source_id)
            if source is None:
                previews.append(f"{role.title()}: Not configured")
                continue
            state = self.hass.states.get(source.get("entity_id"))
            if state is None or state.state in {"unknown", "unavailable", ""}:
                previews.append(f"{role.title()}: Unavailable ({source.get('entity_id')})")
                continue
            attribute = source.get("attribute")
            if attribute is None and str(source.get("entity_id", "")).startswith("climate."):
                attribute = "current_temperature"
            raw = state.attributes.get(attribute) if attribute else state.state
            unit = (state.attributes.get("unit_of_measurement")
                    or state.attributes.get("temperature_unit"))
            try:
                value = normalize_value(float(raw), unit, "temperature").value
                quality = source.get("quality", {})
                invalid = tuple(float(item) for item in quality.get("invalid_values", (-55, 155)))
                valid = value not in invalid and float(quality.get("minimum", -50)) <= value <= float(quality.get("maximum", 60))
                previews.append(
                    f"{role.title()}: {value:.2f} °C · {'valid' if valid else 'rejected'} · "
                    f"updated {state.last_updated.isoformat()} · {source.get('entity_id')}"
                )
            except (TypeError, ValueError, IncompatibleUnitError):
                previews.append(
                    f"{role.title()}: Incompatible value/unit ({source.get('entity_id')})"
                )
        return " | ".join(previews)

    def _data_quality_schema(self, values=None):
        values = values or self.result
        schema = {
            vol.Required("maximum_power_gap_minutes", default=values.get("maximum_power_gap_minutes", 30)): vol.Coerce(int),
            vol.Required("counter_jump_limit_kwh", default=values.get("counter_jump_limit_kwh", 500)): vol.Coerce(float),
            vol.Required(
                "response_temperature_coverage_minimum",
                default=values.get("response_temperature_coverage_minimum", 95),
            ): vol.All(vol.Coerce(float), vol.Range(min=0, max=100)),
            vol.Required("assumed_heating_reporting", default=values.get("assumed_heating_reporting", "separate")): selector.SelectSelector(selector.SelectSelectorConfig(options=["separate", "include_in_heating_total"])),
            vol.Required("test_representative_sources", default=False): selector.BooleanSelector(),
        }
        candidates = self._representative_candidates()
        indoor = [item for item in candidates if item.get("role") in {"indoor", "equipment_room"}]
        if indoor:
            choices = [{"value": "__none__", "label": "Not configured"}, *[
                {"value": item["source_id"], "label": item.get("display_name", item["source_id"])}
                       for item in indoor]
            ]
            current = values.get("representative_indoor_source_id") or "__none__"
            schema[vol.Required("representative_indoor_source_id", default=current)] = selector.SelectSelector(
                selector.SelectSelectorConfig(options=choices)
            )
        outdoor = [item for item in candidates if item.get("role") == "outdoor"]
        if outdoor:
            choices = [{"value": "__none__", "label": "Not configured"}, *[
                {"value": item["source_id"], "label": item.get("display_name", item["source_id"])}
                       for item in outdoor]
            ]
            current = values.get("representative_outdoor_source_id") or "__none__"
            schema[vol.Required("representative_outdoor_source_id", default=current)] = selector.SelectSelector(
                selector.SelectSelectorConfig(options=choices)
            )
        return vol.Schema(schema)


class ConfigFlow(_Wizard, config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 2
    def __init__(self): self._load({})
    async def async_step_user(self, user_input=None):
        if self._async_current_entries(): return self.async_abort(reason="single_instance_allowed")
        return await self.async_step_general(user_input)
    def _finish(self, data): return self.async_create_entry(title="Home Energy Intelligence", data=data)
    @staticmethod
    @callback
    def async_get_options_flow(config_entry): return OptionsFlow()


class OptionsFlow(_Wizard, config_entries.OptionsFlow):
    def __init__(self):
        self._loaded_entry = False

    async def async_step_init(self, user_input=None):
        # Home Assistant supplies config_entry through the OptionsFlow handler
        # after initialization.  It is a read-only property in current HA.
        if not self._loaded_entry:
            self._load({**self.config_entry.data, **self.config_entry.options})
            self._editing = True
            self._loaded_entry = True
        return await self.async_step_general(user_input)
    def _finish(self, data):
        current = {**self.config_entry.data, **self.config_entry.options}
        committed = deepcopy(data)
        mapping_keys = (
            "equipment", "temperature_sources", "site_sources",
            "representative_indoor_source_id", "representative_outdoor_source_id",
        )
        def comparable(key, value):
            # JSON whitespace/key formatting is not a mapping revision.
            if key in {"equipment", "temperature_sources"}:
                return parse_json_records(value)
            return value

        if all(
            comparable(key, committed.get(key))
            == comparable(key, self._original.get(key))
            for key in mapping_keys
        ):
            if "mapping_effective_at" in self._original:
                committed["mapping_effective_at"] = self._original["mapping_effective_at"]
            else:
                committed.pop("mapping_effective_at", None)
        if current.get("influx_password") and not committed.get("influx_password"):
            committed["influx_password"] = current["influx_password"]
        return self.async_create_entry(title="", data=committed)

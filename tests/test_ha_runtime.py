"""Runtime contract tests; executed in CI with current Home Assistant."""

from __future__ import annotations

import inspect
import importlib.util
from unittest.mock import AsyncMock, patch

import pytest

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("homeassistant") is None,
    reason="current Home Assistant test harness is installed in CI",
)


def test_current_recorder_read_api_contract():
    from homeassistant.components.recorder import history, statistics

    history_parameters = inspect.signature(history.get_significant_states).parameters
    assert {"hass", "start_time", "end_time", "entity_ids", "include_start_time_state"} <= set(history_parameters)
    statistics_parameters = inspect.signature(statistics.statistics_during_period).parameters
    assert {"hass", "start_time", "end_time", "statistic_ids", "period", "types"} <= set(statistics_parameters)


@pytest.mark.asyncio
async def test_config_options_setup_reload_unload_panel_and_sensor_refresh(
    hass, enable_custom_integrations
):
    from homeassistant.config_entries import ConfigEntryState
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.home_energy_intelligence.const import DOMAIN, PANEL_URL
    from custom_components.home_energy_intelligence.coordinator import HomeEnergyCoordinator

    flow = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert flow["type"].value == "form" and flow["step_id"] == "general"

    entry = MockConfigEntry(domain=DOMAIN, title="Home Energy Intelligence", data={
        "locale": "en", "floor_area_m2": 180, "equipment": "[]",
        "temperature_sources": "[]", "site_sources": {}, "historical_source": "recorder",
    })
    entry.add_to_hass(hass)
    with patch.object(HomeEnergyCoordinator, "async_catch_up", new=AsyncMock()):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        assert entry.entry_id in hass.data[DOMAIN]
        assert PANEL_URL in hass.data.get("frontend_panels", {})
        assert any(state.entity_id.endswith("latest_data_quality") for state in hass.states.async_all())

        options = await hass.config_entries.options.async_init(entry.entry_id)
        assert options["type"].value == "form" and options["step_id"] == "general"

        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert entry.state is ConfigEntryState.NOT_LOADED


@pytest.mark.asyncio
async def test_options_flow_preserves_stable_ids_and_existing_mappings(
    hass, enable_custom_integrations
):
    import json
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.home_energy_intelligence.const import DOMAIN

    hass.states.async_set("sensor.hp_energy", "100", {
        "device_class": "energy", "state_class": "total_increasing", "unit_of_measurement": "kWh",
    })
    hass.states.async_set("sensor.hp_power", "500", {
        "device_class": "power", "unit_of_measurement": "W",
    })
    hass.states.async_set("sensor.hp_mode", "heating")
    hass.states.async_set("sensor.outdoor", "5", {
        "device_class": "temperature", "unit_of_measurement": "°C",
    })
    equipment = [{
        "equipment_id": "stable-hp", "display_name": "Main heat pump", "kind": "heat_pump",
        "enabled": True, "energy_entity": "sensor.hp_energy", "power_entity": "sensor.hp_power",
        "state_entity": "sensor.hp_mode", "state_map": {"heating": "confirmed_heating"},
    }]
    temperatures = [{
        "source_id": "stable-outdoor", "display_name": "Outdoor", "role": "outdoor",
        "entity_id": "sensor.outdoor", "enabled": True,
        "quality": {"minimum": -40, "maximum": 55, "invalid_values": [-55, 155],
                    "maximum_change_per_minute": 5, "freshness_minutes": 360,
                    "missing_after_minutes": 360},
    }]
    entry = MockConfigEntry(domain=DOMAIN, title="HEI", version=2, data={
        "locale": "en", "floor_area_m2": 180, "equipment": json.dumps(equipment),
        "temperature_sources": json.dumps(temperatures), "site_sources": {},
        "historical_source": "recorder",
    })
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"locale": "sl", "floor_area_m2": 181},
    )
    assert result["step_id"] == "heat_pumps"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {
        "configure": True, "equipment_id": "attempted-change", "display_name": "Main heat pump",
        "enabled": True, "energy_entity": "sensor.hp_energy", "power_entity": "sensor.hp_power",
        "state_entity": "sensor.hp_mode", "state_map": json.dumps({"heating": "confirmed_heating"}),
        "add_another": False, "action": "update",
    })
    assert result["step_id"] == "air_conditioners"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"configure": False, "add_another": False},
    )
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["step_id"] == "temperatures"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {
        "configure": True, "source_id": "attempted-change", "display_name": "Outdoor",
        "role": "outdoor", "entity_id": "sensor.outdoor", "enabled": True,
        "minimum": -40, "maximum": 55, "invalid_values": "-55,155",
        "maximum_change_per_minute": 5, "freshness_minutes": 360,
        "missing_after_minutes": 360, "add_another": False, "action": "update",
    })
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {
        "historical_source": "recorder", "influx_enabled": False,
        "influx_port": 8086, "influx_use_ssl": False, "influx_verify_ssl": True,
        "influx_database": "homeassistant", "influx_retention_policy": "autogen",
        "influx_mapping_json": "{}",
    })
    result = await hass.config_entries.options.async_configure(result["flow_id"], {
        "maximum_power_gap_minutes": 30, "counter_jump_limit_kwh": 500,
        "assumed_heating_reporting": "separate",
    })
    assert result["type"].value == "create_entry"
    saved_equipment = json.loads(result["data"]["equipment"])
    saved_temperatures = json.loads(result["data"]["temperature_sources"])
    assert saved_equipment[0]["equipment_id"] == "stable-hp"
    assert saved_temperatures[0]["source_id"] == "stable-outdoor"
    assert saved_temperatures[0]["quality"]["invalid_values"] == [-55.0, 155.0]

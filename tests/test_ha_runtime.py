"""Runtime contract tests; executed in CI with current Home Assistant."""

from __future__ import annotations

import inspect
import importlib.util
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from datetime import UTC, datetime

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
    from homeassistant.helpers import entity_registry as er
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.home_energy_intelligence.const import DOMAIN, PANEL_URL
    from custom_components.home_energy_intelligence.coordinator import HomeEnergyCoordinator
    from custom_components.home_energy_intelligence.panel import async_register_panel

    # The isolated HA fixture does not bootstrap a full Recorder/frontend stack.
    # Mark declared platform dependencies loaded so the test exercises this
    # integration's real flow handler and runtime setup rather than failing in
    # dependency discovery. Recorder helper signatures are tested separately.
    hass.config.components.update({"frontend", "http", "recorder"})
    static_paths = AsyncMock()
    hass.http = SimpleNamespace(async_register_static_paths=static_paths)
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
        static_paths.assert_awaited_once()
        panel_count = len(hass.data.get("frontend_panels", {}))
        await async_register_panel(hass)
        assert len(hass.data.get("frontend_panels", {})) == panel_count
        quality_entity = er.async_get(hass).async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_latest_data_quality"
        )
        assert quality_entity is not None
        assert hass.states.get(quality_entity) is not None

        options = await hass.config_entries.options.async_init(entry.entry_id)
        assert options["type"].value == "form" and options["step_id"] == "general"

        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        assert PANEL_URL in hass.data.get("frontend_panels", {})
        assert hass.data[DOMAIN]["panel_static_path_registered"] is True
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert entry.state is ConfigEntryState.NOT_LOADED
        assert PANEL_URL not in hass.data.get("frontend_panels", {})


@pytest.mark.asyncio
async def test_options_flow_preserves_stable_ids_and_existing_mappings(
    hass, enable_custom_integrations
):
    import json
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.home_energy_intelligence.const import DOMAIN

    hass.config.components.update({"frontend", "http", "recorder"})

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


@pytest.mark.asyncio
async def test_current_day_snapshot_uses_completed_hourly_rows_without_daily_write(hass):
    from custom_components.home_energy_intelligence.websocket import _current_day_snapshot

    start = datetime(2025, 12, 31, 23, tzinfo=UTC)
    row = {
        "local_date": "2026-01-01", "timezone": "Europe/Ljubljana",
        "period_start_utc": start.isoformat(),
        "period_end_utc": datetime(2026, 1, 1, tzinfo=UTC).isoformat(),
        "day_seconds": 3600, "household_kwh": 1.5, "pv_kwh": None,
        "import_kwh": 1.5, "export_kwh": 0, "non_hvac_kwh": 1.5,
        "quality": "good", "allocations": {}, "temperatures": {},
    }

    class ReadOnlyDatabase:
        def __init__(self):
            self.daily_writes = 0

        def hourly_range(self, range_start, range_end):
            assert range_start == start
            assert range_end == datetime(2026, 1, 1, tzinfo=UTC)
            return [row]

    database = ReadOnlyDatabase()
    coordinator = SimpleNamespace(database=database, timezone="Europe/Ljubljana")
    current, hours, coverage, cutoff, local_day = await _current_day_snapshot(
        hass, coordinator, datetime(2026, 1, 1, tzinfo=UTC),
        now=datetime(2026, 1, 1, 0, 30, tzinfo=UTC),
    )
    assert current["household_kwh"] == 1.5
    assert current["partial"] is True and current["finalized"] is False
    assert hours == [row] and coverage["complete"] is True
    assert cutoff == datetime(2026, 1, 1, tzinfo=UTC)
    assert local_day.isoformat() == "2026-01-01"
    assert database.daily_writes == 0

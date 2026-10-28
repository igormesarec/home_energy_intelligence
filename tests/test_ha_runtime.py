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
async def test_config_options_setup_reload_unload_panel_and_sensor_refresh(hass):
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

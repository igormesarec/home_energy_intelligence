"""Runtime contract tests; executed in CI with current Home Assistant."""

from __future__ import annotations

import asyncio
import inspect
import importlib.util
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("homeassistant") is None,
    reason="current Home Assistant test harness is installed in CI",
)


@pytest.mark.asyncio
async def test_temperature_pipeline_preserves_recorder_rows_and_collection_evidence(hass):
    from custom_components.home_energy_intelligence.coordinator import HomeEnergyCoordinator

    start = datetime(2026, 10, 29, 8, tzinfo=UTC)
    end = start + timedelta(hours=1)
    hass.states.async_set("sensor.representative_indoor", "22", {
        "device_class": "temperature", "unit_of_measurement": "°C",
    })
    rows = [
        SimpleNamespace(last_updated=start, state="21", attributes={
            "unit_of_measurement": "°C",
        }),
        SimpleNamespace(last_updated=start + timedelta(minutes=30), state="22", attributes={
            "unit_of_measurement": "°C",
        }),
    ]
    database = SimpleNamespace(store_mapping_revision=lambda _revision: True)
    entry = SimpleNamespace(entry_id="entry", data={
        "equipment": "[]", "site_sources": {},
        "temperature_sources": json.dumps([{
            "source_id": "indoor", "display_name": "Indoor", "role": "indoor",
            "entity_id": "sensor.representative_indoor", "enabled": True,
            "quality": {"minimum": -10, "maximum": 60,
                        "invalid_values": [-55, 155],
                        "maximum_change_per_minute": 8,
                        "freshness_minutes": 360, "missing_after_minutes": 360},
        }]),
    }, options={})
    coordinator = HomeEnergyCoordinator(hass, entry, database)
    coordinator.reader = SimpleNamespace(
        async_history=AsyncMock(return_value={"sensor.representative_indoor": rows}),
        async_statistics=AsyncMock(return_value={}),
    )
    collected = await coordinator._async_collect_range(start, end)
    evidence = collected.provenance["temperature_collection"]["indoor"]
    assert evidence["entity_id"] == "sensor.representative_indoor"
    assert evidence["recorder_state_rows"] == 2
    assert evidence["valid_samples"] == 2
    assert evidence["source_units"] == ["°C"]
    record = coordinator._aggregator().aggregate_range(
        start.date(), start, end, collected
    )
    assert record.temperatures["indoor"]["mean"] == pytest.approx(21.5)
    assert record.temperatures["indoor"]["coverage_percent"] == 100


@pytest.mark.asyncio
async def test_temperature_only_collector_never_requests_energy_mode_pv_or_grid(hass):
    from custom_components.home_energy_intelligence.coordinator import HomeEnergyCoordinator

    start = datetime(2026, 10, 29, 8, tzinfo=UTC)
    end = start + timedelta(hours=1)
    entry = SimpleNamespace(entry_id="entry", data={
        "equipment": json.dumps([{
            "equipment_id": "hp", "display_name": "Heat pump", "kind": "heat_pump",
            "enabled": True, "energy_entity": "sensor.hp_energy",
            "power_entity": "sensor.hp_power", "state_entity": "sensor.hp_mode",
        }]),
        "site_sources": {"pv": "sensor.pv", "import": "sensor.import", "export": "sensor.export"},
        "temperature_sources": json.dumps([{
            "source_id": "outdoor", "display_name": "Outdoor", "role": "outdoor",
            "entity_id": "sensor.outdoor", "enabled": True,
        }]),
    }, options={})
    coordinator = HomeEnergyCoordinator(
        hass, entry, SimpleNamespace(store_mapping_revision=lambda _revision: True)
    )
    coordinator.reader = SimpleNamespace(
        async_history=AsyncMock(return_value={"sensor.outdoor": []}),
        async_statistics=AsyncMock(return_value={}),
    )
    await coordinator._async_collect_temperatures(start, end, {"outdoor"})
    history_ids = coordinator.reader.async_history.await_args.args[0]
    statistic_ids = coordinator.reader.async_statistics.await_args.args[0]
    assert history_ids == ["sensor.outdoor"]
    assert statistic_ids == {"sensor.outdoor"}
    forbidden = {"sensor.hp_energy", "sensor.hp_power", "sensor.hp_mode",
                 "sensor.pv", "sensor.import", "sensor.export"}
    assert forbidden.isdisjoint(history_ids)
    assert forbidden.isdisjoint(statistic_ids)


@pytest.mark.asyncio
async def test_bounded_temperature_repair_is_lean_idempotent_and_skips_complete_sources(
    hass, tmp_path,
):
    from custom_components.home_energy_intelligence.coordinator import HomeEnergyCoordinator
    from custom_components.home_energy_intelligence.database import EnergyDatabase
    from custom_components.home_energy_intelligence.models import DailyRecord

    target = date(2026, 10, 29)
    start = datetime(2026, 10, 29, tzinfo=UTC)
    end = start + timedelta(days=1)
    database = EnergyDatabase(tmp_path / "energy.sqlite3")
    database.initialize()
    database.upsert_daily(DailyRecord(
        target.isoformat(), "UTC", start.isoformat(), end.isoformat(), 86400,
        6.7, None, None, None, None, "partial", temperatures={}, allocations={},
    ))
    entry = SimpleNamespace(entry_id="entry", data={
        "equipment": json.dumps([{
            "equipment_id": "hp", "display_name": "Heat pump", "kind": "heat_pump",
            "enabled": True, "energy_entity": "sensor.hp_energy",
            "power_entity": "sensor.hp_power", "state_entity": "sensor.hp_mode",
        }]),
        "site_sources": {"pv": "sensor.pv", "import": "sensor.import"},
        "temperature_sources": json.dumps([{
            "source_id": "outdoor", "display_name": "Outdoor", "role": "outdoor",
            "entity_id": "sensor.outdoor", "enabled": True,
            "quality": {"minimum": -40, "maximum": 55,
                        "invalid_values": [-55, 155]},
        }]),
    }, options={})
    hass.states.async_set("sensor.outdoor", "8", {
        "device_class": "temperature", "unit_of_measurement": "°C",
    })
    rows = [
        SimpleNamespace(last_updated=start, state="8", attributes={
            "unit_of_measurement": "°C",
        }),
        SimpleNamespace(last_updated=end, state="9", attributes={
            "unit_of_measurement": "°C",
        }),
    ]
    coordinator = HomeEnergyCoordinator(hass, entry, database)
    coordinator.reader = SimpleNamespace(
        async_history=AsyncMock(return_value={"sensor.outdoor": rows}),
        async_statistics=AsyncMock(return_value={}),
    )
    coordinator._async_collect_range = AsyncMock(
        side_effect=AssertionError("repair must not invoke the general collector")
    )
    result = await coordinator.async_reprocess_temperature_history(target, target)
    assert result["changed_daily_records"] == 1
    assert result["sources_with_available_history"] == 1
    assert result["error_count"] == 0
    assert coordinator.reader.async_history.await_count == 1
    assert coordinator.reader.async_statistics.await_count == 1
    queried = coordinator.reader.async_history.await_args.args[0]
    assert queried == ["sensor.outdoor"]
    assert {"sensor.hp_energy", "sensor.hp_power", "sensor.hp_mode",
            "sensor.pv", "sensor.import"}.isdisjoint(queried)
    assert coordinator._async_collect_range.await_count == 0
    repaired = database.daily(target.isoformat())
    assert repaired["household_kwh"] == 6.7
    assert repaired["temperatures"]["outdoor"]["mean"] == pytest.approx(8)

    second = await coordinator.async_reprocess_temperature_history(target, target)
    assert second["changed_daily_records"] == 0
    assert second["sources_skipped_complete"] == 1
    assert coordinator.reader.async_history.await_count == 1
    assert coordinator.reader.async_statistics.await_count == 1

    await coordinator._temperature_repair_lock.acquire()
    try:
        overlap = await coordinator.async_reprocess_temperature_history(target, target)
    finally:
        coordinator._temperature_repair_lock.release()
    assert overlap["errors"][0]["reason"] == "temperature_repair_already_running"


@pytest.mark.asyncio
async def test_startup_temperature_repair_failure_does_not_block_other_recovery(hass):
    from custom_components.home_energy_intelligence.coordinator import HomeEnergyCoordinator

    local_today = datetime.now(UTC).astimezone(ZoneInfo(hass.config.time_zone)).date()
    background = []
    entry = SimpleNamespace(entry_id="entry", data={
        "equipment": "[]", "temperature_sources": "[]", "site_sources": {},
    }, options={})
    entry.async_create_background_task = (
        lambda target_hass, coroutine, _name:
        background.append(target_hass.async_create_task(coroutine))
    )
    database = SimpleNamespace(latest=lambda: {
        "local_date": (local_today - timedelta(days=1)).isoformat(),
    })
    coordinator = HomeEnergyCoordinator(hass, entry, database)
    coordinator.async_refresh_hourly = AsyncMock(return_value=0)
    coordinator.async_recover_checkpoints = AsyncMock(return_value=None)
    coordinator.async_reprocess_temperature_history = AsyncMock(
        side_effect=RuntimeError("Recorder unavailable")
    )
    coordinator.async_process_day = AsyncMock(return_value=False)

    await coordinator.async_catch_up()
    await asyncio.gather(*background)
    coordinator.async_refresh_hourly.assert_awaited_once()
    coordinator.async_recover_checkpoints.assert_awaited_once()
    coordinator.async_reprocess_temperature_history.assert_awaited_once()


@pytest.mark.asyncio
async def test_slow_startup_temperature_repair_runs_outside_normal_recovery(hass):
    from custom_components.home_energy_intelligence.coordinator import HomeEnergyCoordinator

    local_today = datetime.now(UTC).astimezone(ZoneInfo(hass.config.time_zone)).date()
    gate = asyncio.Event()
    started = asyncio.Event()
    background = []

    async def slow_repair(*_args):
        started.set()
        await gate.wait()
        return {"changed_daily_records": 0, "changed_hourly_records": 0}

    entry = SimpleNamespace(entry_id="entry", data={
        "equipment": "[]", "temperature_sources": "[]", "site_sources": {},
    }, options={})
    entry.async_create_background_task = (
        lambda target_hass, coroutine, _name:
        background.append(target_hass.async_create_task(coroutine))
    )
    database = SimpleNamespace(latest=lambda: {
        "local_date": (local_today - timedelta(days=1)).isoformat(),
    })
    coordinator = HomeEnergyCoordinator(hass, entry, database)
    coordinator.async_refresh_hourly = AsyncMock(return_value=0)
    coordinator.async_recover_checkpoints = AsyncMock(return_value=None)
    coordinator.async_reprocess_temperature_history = AsyncMock(side_effect=slow_repair)
    coordinator.async_process_day = AsyncMock(return_value=False)

    await coordinator.async_catch_up()
    await started.wait()
    assert background and not background[0].done()
    coordinator.async_refresh_hourly.assert_awaited_once()
    coordinator.async_recover_checkpoints.assert_awaited_once()
    gate.set()
    await asyncio.gather(*background)


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
    representative_field = next(
        field for field in result["data_schema"].schema
        if str(field) == "representative_outdoor_source_id"
    )
    assert representative_field.default() == "__none__"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {
        "maximum_power_gap_minutes": 30, "counter_jump_limit_kwh": 500,
        "response_temperature_coverage_minimum": 95,
        "assumed_heating_reporting": "separate",
        "representative_outdoor_source_id": "stable-outdoor",
        "test_representative_sources": True,
    })
    assert result["step_id"] == "data_quality"
    assert "Outdoor" in result["description_placeholders"]["representative_preview"]
    assert "5.00 °C" in result["description_placeholders"]["representative_preview"]
    result = await hass.config_entries.options.async_configure(result["flow_id"], {
        "maximum_power_gap_minutes": 30, "counter_jump_limit_kwh": 500,
        "response_temperature_coverage_minimum": 95,
        "assumed_heating_reporting": "separate",
        "representative_outdoor_source_id": "__none__",
        "test_representative_sources": False,
    })
    assert result["type"].value == "create_entry"
    saved_equipment = json.loads(result["data"]["equipment"])
    saved_temperatures = json.loads(result["data"]["temperature_sources"])
    assert saved_equipment[0]["equipment_id"] == "stable-hp"
    assert saved_temperatures[0]["source_id"] == "stable-outdoor"
    assert saved_temperatures[0]["quality"]["invalid_values"] == [-55.0, 155.0]
    assert "representative_outdoor_source_id" not in result["data"]


@pytest.mark.asyncio
async def test_options_draft_skip_disable_remove_and_validation_are_transactional(hass):
    from custom_components.home_energy_intelligence.config_wizard import OptionsFlow

    hass.states.async_set("sensor.energy", "10", {
        "device_class": "energy", "state_class": "total_increasing",
        "unit_of_measurement": "kWh",
    })
    hass.states.async_set("sensor.mode", "off")
    for entity_id in ("sensor.indoor", "sensor.outdoor", "sensor.supply"):
        hass.states.async_set(entity_id, "20", {
            "device_class": "temperature", "unit_of_measurement": "°C",
        })
    equipment = [{
        "equipment_id": "ecodan", "display_name": "Ecodan", "kind": "heat_pump",
        "enabled": True, "energy_entity": "sensor.energy",
        "state_entity": "sensor.mode", "state_map": {"off": "idle"},
    }, *[{
        "equipment_id": equipment_id, "display_name": name,
        "kind": "air_conditioner", "enabled": True,
        "energy_entity": "sensor.energy", "state_entity": "sensor.mode",
        "state_map": {"off": "standby"},
    } for equipment_id, name in (
        ("kuhinja", "Kuhinja"), ("aurora", "Aurora"), ("dnevna", "Dnevna"),
    )]]
    temperatures = [{
        "source_id": source_id, "display_name": name, "role": role,
        "entity_id": entity_id, "enabled": True,
        "quality": {"minimum": -40, "maximum": 90,
                    "invalid_values": [-55, 155]},
    } for source_id, name, role, entity_id in (
        ("indoor", "Indoor", "indoor", "sensor.indoor"),
        ("outdoor", "Outdoor", "outdoor", "sensor.outdoor"),
        ("supply", "Centrometal supply", "supply", "sensor.supply"),
    )]
    original = {
        "locale": "en", "floor_area_m2": 180,
        "equipment": json.dumps(equipment),
        "temperature_sources": json.dumps(temperatures),
        "representative_indoor_source_id": "indoor",
        "representative_outdoor_source_id": "outdoor",
        "site_sources": {"pv": "sensor.pv", "import": "sensor.import",
                         "export": "sensor.export"},
        "historical_source": "recorder", "mapping_effective_at": "2026-01-01T00:00:00Z",
    }

    skipped = OptionsFlow(); skipped.hass = hass
    skipped._load(deepcopy(original)); skipped._editing = True
    await skipped.async_step_heat_pumps(None)
    await skipped.async_step_heat_pumps({"configure": False})
    await skipped.async_step_air_conditioners({"configure": False})
    assert skipped.equipment == equipment
    await skipped.async_step_temperatures(None)
    await skipped.async_step_temperatures({"configure": False})
    assert skipped.temperatures == temperatures
    assert skipped.result["representative_indoor_source_id"] == "indoor"
    assert skipped.result["representative_outdoor_source_id"] == "outdoor"
    assert skipped.result["site_sources"] == original["site_sources"]

    disabled = OptionsFlow(); disabled.hass = hass
    disabled._load(deepcopy(original)); disabled._editing = True
    await disabled.async_step_air_conditioners(None)
    await disabled.async_step_air_conditioners({
        "configure": True, "action": "disable", "add_another": False,
    })
    await disabled.async_step_air_conditioners({"configure": False})
    disabled_ac = next(item for item in disabled.equipment
                       if item["equipment_id"] == "kuhinja")
    assert disabled_ac["enabled"] is False
    assert disabled_ac["state_map"] == {"off": "standby"}
    assert {item["equipment_id"] for item in disabled.equipment} == {
        "ecodan", "kuhinja", "aurora", "dnevna",
    }

    removed = OptionsFlow(); removed.hass = hass
    removed._load(deepcopy(original)); removed._editing = True
    await removed.async_step_air_conditioners(None)
    await removed.async_step_air_conditioners({
        "configure": True, "action": "remove", "add_another": False,
    })
    await removed.async_step_air_conditioners({"configure": False})
    assert {item["equipment_id"] for item in removed.equipment} == {
        "ecodan", "aurora", "dnevna",
    }

    invalid = OptionsFlow(); invalid.hass = hass
    invalid._load(deepcopy(original)); invalid._editing = True
    await invalid.async_step_temperatures(None)
    result = await invalid.async_step_temperatures({
        "configure": True, "action": "update", "add_another": False,
        "source_id": "changed", "display_name": "Indoor", "role": "indoor",
        "entity_id": "sensor.indoor", "enabled": True,
        "minimum": 30, "maximum": 20, "invalid_values": "-55,155",
        "maximum_change_per_minute": 8, "freshness_minutes": 360,
        "missing_after_minutes": 360,
    })
    assert result["errors"]["base"] == "invalid_temperature_limits"
    assert invalid._temperature_pending[0]["source_id"] == "indoor"
    assert invalid.temperatures == []


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

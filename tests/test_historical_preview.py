import json
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.home_energy_intelligence.coordinator import HomeEnergyCoordinator
from custom_components.home_energy_intelligence.engine import DailyInput, DeviceHistory
from custom_components.home_energy_intelligence.models import NumericSample, StateSample


def test_historical_preview_requires_administrator():
    from homeassistant.exceptions import Unauthorized
    from custom_components.home_energy_intelligence.websocket import ws_historical_energy_preview
    with pytest.raises(Unauthorized):
        ws_historical_energy_preview(None, SimpleNamespace(user=SimpleNamespace(is_admin=False)), {"id": 1})


@pytest.mark.asyncio
async def test_read_only_preview_retains_measured_total_and_separates_dhw():
    day = date(2026, 10, 9)
    start = datetime(2026, 10, 9, tzinfo=UTC)
    hours = [start + timedelta(hours=i) for i in range(25)]
    source = DailyInput(devices={"hp": DeviceHistory(
        kind="heat_pump", counter=[NumericSample(t, 100+i) for i, t in enumerate(hours)],
        power=[NumericSample(start+timedelta(minutes=30*i), 1000) for i in range(48)],
        mode=[StateSample(t, "heating_water" if 10 <= i < 12 else "heating_zones") for i,t in enumerate(hours[:-1])],
    )}, site_counters={}, temperatures={}, provenance={"counter_collection": {}})
    collector = AsyncMock(return_value=source)
    coordinator = SimpleNamespace(
        timezone="UTC", mapping_revision={"effective_at": "2026-01-01T00:00:00+00:00"},
        config={"equipment": [{"equipment_id": "hp", "display_name": "HP", "kind": "heat_pump",
                               "state_map": {"heating_water": "dhw", "heating_zones": "confirmed_heating"}}]},
        _async_collect_range=collector,
    )
    result = await HomeEnergyCoordinator.async_preview_historical_energy(coordinator, day)
    hp = result["allocations"]["hp"]
    assert result["preview"] is True and hp["meter_kwh"] == pytest.approx(24)
    assert hp["categories"]["dhw"] == pytest.approx(2)
    assert hp["categories"]["confirmed_heating"] == pytest.approx(22)
    assert hp["residual_kwh"] == pytest.approx(0)
    collector.assert_awaited_once_with(start, start+timedelta(days=1), store_mapping_revision=False)


@pytest.mark.asyncio
async def test_preview_rejects_unattested_newer_mapping_before_collection():
    collector = AsyncMock()
    coordinator = SimpleNamespace(timezone="UTC", mapping_revision={"effective_at": "2026-10-01T00:00:00+00:00"},
                                  config={}, _async_collect_range=collector)
    with pytest.raises(ValueError, match="attestation"):
        await HomeEnergyCoordinator.async_preview_historical_energy(coordinator, date(2026, 9, 1))
    collector.assert_not_awaited()


@pytest.mark.asyncio
async def test_preview_reads_configured_influx_when_recorder_missing_without_double_count(monkeypatch):
    day = date(2026, 10, 9)
    start = datetime(2026, 10, 9, tzinfo=UTC)
    calls = []
    class FakeInflux:
        def __init__(self, **_kwargs): pass
        def query_states(self, left, right, *, entity_id, domain, maximum_age):
            calls.append(("state", entity_id))
            return [(left+timedelta(hours=i), "heating_water" if i == 10 else "heating_zones") for i in range(24)]
        def query_counter(self, measurement, field, left, right, *, entity_tag, include_following=False):
            calls.append(("counter", entity_tag))
            return [(left+timedelta(hours=i), 100+i) for i in range(25)]
    monkeypatch.setattr("custom_components.home_energy_intelligence.coordinator.InfluxBackfillClient", FakeInflux)
    source = DailyInput(devices={"hp": DeviceHistory(kind="heat_pump", power=[
        NumericSample(start+timedelta(minutes=30*i), 1000) for i in range(48)])},
        site_counters={}, temperatures={}, provenance={"counter_collection": {}})
    async def execute(func, *args): return func(*args)
    coordinator = SimpleNamespace(
        timezone="UTC", mapping_revision={"effective_at": "2026-01-01T00:00:00+00:00"},
        config={"equipment": [{"equipment_id": "hp", "display_name": "HP", "kind": "heat_pump",
                               "energy_entity": "sensor.energy", "state_entity": "sensor.mode",
                               "state_map": {"heating_water": "dhw", "heating_zones": "confirmed_heating"}}],
                "influx_enabled": True, "influx_host": "localhost", "influx_database": "homeassistant",
                "influx_mapping_json": json.dumps({"equipment.hp.state": {"entity_id": "sensor.mode"},
                    "equipment.hp.energy": {"entity_id": "sensor.energy", "entity_tag": "energy", "measurement": "kWh", "unit": "kWh"}})},
        hass=SimpleNamespace(async_add_executor_job=execute), _async_collect_range=AsyncMock(return_value=source))
    report = await HomeEnergyCoordinator.async_preview_historical_energy(coordinator, day)
    assert calls == [("state", "sensor.mode"), ("counter", "energy")]
    assert report["allocations"]["hp"]["meter_kwh"] == pytest.approx(24)
    assert report["allocations"]["hp"]["categories"]["dhw"] == pytest.approx(1)
    assert report["provenance"]["counter_collection"]["equipment.hp.energy"] == "influx"


@pytest.mark.asyncio
async def test_recorder_wins_over_conflicting_influx_at_same_state_timestamp(monkeypatch):
    day = date(2026, 10, 9)
    start = datetime(2026, 10, 9, tzinfo=UTC)
    class FakeInflux:
        def __init__(self, **_kwargs): pass
        def query_states(self, left, right, *, entity_id, domain, maximum_age):
            return [(left+timedelta(hours=i), "heating_zones") for i in range(24)]
    monkeypatch.setattr("custom_components.home_energy_intelligence.coordinator.InfluxBackfillClient", FakeInflux)
    source = DailyInput(devices={"hp": DeviceHistory(kind="heat_pump",
        counter=[NumericSample(start+timedelta(hours=i), i) for i in range(25)],
        power=[NumericSample(start+timedelta(minutes=30*i), 1000) for i in range(48)],
        mode=[StateSample(start+timedelta(hours=10), "heating_water")])},
        site_counters={}, temperatures={}, provenance={"counter_collection": {}})
    async def execute(func, *args): return func(*args)
    coordinator = SimpleNamespace(timezone="UTC", mapping_revision={"effective_at": "2026-01-01T00:00:00+00:00"},
        config={"equipment": [{"equipment_id": "hp", "display_name": "HP", "kind": "heat_pump",
              "state_entity": "sensor.mode", "state_map": {"heating_water": "dhw", "heating_zones": "confirmed_heating"}}],
              "influx_enabled": True, "influx_host": "localhost", "influx_database": "homeassistant",
              "influx_mapping_json": json.dumps({"equipment.hp.state": {"entity_id": "sensor.mode"}})},
        hass=SimpleNamespace(async_add_executor_job=execute), _async_collect_range=AsyncMock(return_value=source))
    report = await HomeEnergyCoordinator.async_preview_historical_energy(coordinator, day)
    assert report["allocations"]["hp"]["categories"]["dhw"] == pytest.approx(1)
    assert report["allocations"]["hp"]["meter_kwh"] == pytest.approx(24)


@pytest.mark.asyncio
async def test_preview_uses_partial_recorder_counter_without_power_or_writes():
    day = date(2026, 9, 28)
    start = datetime(2026, 9, 28, tzinfo=UTC)
    entity = "sensor.energy"
    rows = [SimpleNamespace(last_updated=start + timedelta(minutes=m), state=str(v),
                            attributes={"unit_of_measurement": "kWh"})
            for m, v in [(5, 100), (35, 101), (65, 101.99)]]
    source = DailyInput(devices={"ac": DeviceHistory(kind="air_conditioner", mode=[
        StateSample(start, "heat"), StateSample(start + timedelta(hours=1), "heat")])},
        site_counters={}, temperatures={}, provenance={"counter_collection": {}})
    collector = AsyncMock(return_value=source)
    coordinator = SimpleNamespace(timezone="UTC", mapping_revision={"effective_at": "2026-01-01T00:00:00+00:00"},
        config={"equipment": [{"equipment_id": "ac", "display_name": "AC", "kind": "air_conditioner",
                               "energy_entity": entity}]},
        hass=SimpleNamespace(states=SimpleNamespace(get=lambda _: None)),
        reader=SimpleNamespace(async_history=AsyncMock(return_value={entity: rows})), _async_collect_range=collector)
    result = await HomeEnergyCoordinator.async_preview_historical_energy(coordinator, day)
    ac = result["allocations"]["ac"]
    assert ac["meter_kwh"] == pytest.approx(1.99)
    assert sum(ac["categories"].values()) + ac["residual_kwh"] == pytest.approx(1.99)
    assert ac["source_provenance"]["meter_coverage_seconds"] == 3600
    assert ac["authoritative_counter_source"] == "recorder_states_partial"
    collector.assert_awaited_once_with(start, start + timedelta(days=1), store_mapping_revision=False)

import json
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

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
    collector.assert_awaited_once_with(start, start+timedelta(days=1), store_mapping_revision=False, include_temperatures=False, strict_sources=True, attest_sources=False)


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
    collector.assert_awaited_once_with(start, start + timedelta(days=1), store_mapping_revision=False, include_temperatures=False, strict_sources=True, attest_sources=False)


@pytest.mark.asyncio
async def test_site_preview_reports_partial_and_missing_meter_evidence():
    day = date(2026, 9, 18)
    start = datetime(2026, 9, 18, tzinfo=UTC)
    source = DailyInput(devices={}, site_counters={"pv": [NumericSample(start, 10),
        NumericSample(start + timedelta(minutes=30), 12)]}, temperatures={},
        provenance={"counter_collection": {"site.pv": "recorder_states"}})
    coordinator = SimpleNamespace(timezone="UTC", mapping_revision={"effective_at": "2026-01-01T00:00:00+00:00"},
        config={}, _async_collect_range=AsyncMock(return_value=source))
    report = await HomeEnergyCoordinator.async_preview_historical_energy(coordinator, day)
    assert report["site"]["pv"]["meter_kwh"] == pytest.approx(2)
    assert report["site"]["pv"]["status"] == "partial_known_subtotal"
    assert report["site"]["import"]["meter_kwh"] is None
    assert report["site"]["export"]["status"] == "unavailable"


@pytest.mark.asyncio
async def test_conflicting_displayed_timestamps_preserve_remaining_preview_evidence():
    day = date(2026, 9, 1)
    start = datetime(2026, 9, 1, tzinfo=UTC)
    source = DailyInput(devices={"ac": DeviceHistory(kind="air_conditioner", counter=[
        NumericSample(start + timedelta(minutes=i), value)
        for i, value in [(0, 100), (10, 101), (20, 102), (20, 102.001), (30, 103), (40, 104)]])},
        site_counters={}, temperatures={}, provenance={"counter_collection": {"equipment.ac.energy": "recorder_states"}})
    coordinator = SimpleNamespace(timezone="UTC", mapping_revision={"effective_at": "2026-01-01T00:00:00+00:00"},
        config={"equipment": [{"equipment_id": "ac", "display_name": "AC", "kind": "air_conditioner"}]},
        _async_collect_range=AsyncMock(return_value=source))
    result = await HomeEnergyCoordinator.async_preview_historical_energy(coordinator, day)
    ac = result["allocations"]["ac"]
    assert ac["meter_kwh"] == pytest.approx(2)
    assert ac["source_provenance"]["meter_coverage_seconds"] == 1200
    assert ac["source_provenance"]["conflicting_timestamps_utc"] == [(start + timedelta(minutes=20)).isoformat()]
    assert any(item["error"] == "conflicting_counter_timestamps" for item in result["errors"])


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_history", [False, True])
async def test_preview_does_not_write_database_or_read_temperature_sources(hass, tmp_path, fail_history):
    """Exercise real collection with preserved temperature and mapping records."""
    import sqlite3
    from custom_components.home_energy_intelligence.database import EnergyDatabase

    path = tmp_path / "preview.sqlite3"
    database = EnergyDatabase(path)
    database.initialize()
    start = datetime(2026, 9, 25, tzinfo=UTC)
    identity = {"source_id": "indoor", "entity_id": "sensor.indoor", "attribute": None,
                "role": "indoor", "enabled": True}
    mapping = {"effective_at": "2026-01-01T00:00:00+00:00", "temperatures": [identity]}
    database.ensure_mapping_revision(mapping)
    end = start + timedelta(hours=1)
    applicability = database.temperature_mapping_applicability("indoor", start, end, identity,
                                                                 allow_historical_identity=True)
    assert database.upsert_temperature_history("hourly", start, end, "2026-09-25", "UTC", "indoor",
        {"mean": 22, "minimum": 22, "maximum": 22, "coverage_seconds": 3600,
         "period_seconds": 3600, "coverage_percent": 100, "quality": "good",
         "period_start_utc": start.isoformat(), "period_end_utc": end.isoformat(),
         "source_provenance": {**identity, "mapping_effective_at": applicability["mapping_effective_at"]}},
        applicability) == "inserted"
    def snapshot():
        with sqlite3.connect(path) as connection:
            return list(connection.iterdump())
    before = snapshot()
    entry = SimpleNamespace(entry_id="entry", data={"equipment": "[]", "site_sources": {},
        "temperature_sources": json.dumps([identity])}, options={})
    coordinator = HomeEnergyCoordinator(hass, entry, database, registered_mapping=mapping)
    coordinator.timezone = "UTC"
    history = AsyncMock(side_effect=RuntimeError("unavailable") if fail_history else None,
                        return_value={})
    coordinator.reader = SimpleNamespace(async_history=history, async_statistics=AsyncMock(return_value={}))
    database.store_mapping_revision = Mock(side_effect=AssertionError("unexpected write"))
    if fail_history:
        with pytest.raises(RuntimeError):
            await coordinator.async_preview_historical_energy(start.date())
    else:
        await coordinator.async_preview_historical_energy(start.date())
    assert "sensor.indoor" not in history.await_args.args[0]
    database.store_mapping_revision.assert_not_called()
    assert snapshot() == before


@pytest.mark.asyncio
async def test_influx_failure_keeps_recorder_data_and_never_writes(monkeypatch):
    day = date(2026, 9, 25)
    start = datetime(2026, 9, 25, tzinfo=UTC)
    class ReadOnlyInflux:
        def __init__(self, **kwargs): pass
        def query_counter(self, *args, **kwargs): raise TimeoutError("offline")
    monkeypatch.setattr("custom_components.home_energy_intelligence.coordinator.InfluxBackfillClient", ReadOnlyInflux)
    async def execute(fn, *args): return fn(*args)
    source = DailyInput(devices={"hp": DeviceHistory(kind="heat_pump", counter=[
        NumericSample(start, 10), NumericSample(start + timedelta(hours=1), 11)])},
        site_counters={}, temperatures={}, provenance={"counter_collection": {"equipment.hp.energy": "recorder_states"}})
    database = Mock()
    coordinator = SimpleNamespace(timezone="UTC", mapping_revision={"effective_at": "2026-01-01T00:00:00+00:00"},
        config={"equipment": [{"equipment_id": "hp", "display_name": "HP", "kind": "heat_pump",
                              "energy_entity": "sensor.hp"}], "influx_enabled": True, "influx_host": "localhost",
                "influx_database": "homeassistant", "influx_mapping_json": json.dumps({"equipment.hp.energy": {
                    "entity_id": "sensor.hp", "entity_tag": "hp", "measurement": "kWh", "unit": "kWh"}})},
        hass=SimpleNamespace(async_add_executor_job=execute), database=database,
        _async_collect_range=AsyncMock(return_value=source))
    report = await HomeEnergyCoordinator.async_preview_historical_energy(coordinator, day)
    assert report["allocations"]["hp"]["meter_kwh"] == pytest.approx(1)
    assert any(e["error"] == "TimeoutError" for e in report["errors"])
    assert database.mock_calls == []

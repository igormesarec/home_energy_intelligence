import asyncio
import json
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.home_energy_intelligence.evidence_export import build_evidence_export
from custom_components.home_energy_intelligence.influx import InfluxBackfillClient, InfluxBackfillError


def test_influx_diagnostic_preserves_nanoseconds_and_conflicts(monkeypatch):
    from custom_components.home_energy_intelligence import influx
    ns = 1792886400123456789
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, *args): return json.dumps({"results": [{"series": [{"values": [[ns, "heat"], [ns, "dhw"]]}]}]}).encode()
    monkeypatch.setattr(influx, "urlopen", lambda *args, **kwargs: Response())
    client = InfluxBackfillClient(host="localhost", port=8086, database="homeassistant")
    start = datetime(2026, 10, 25, tzinfo=UTC)
    rows = client.diagnostic_states_ns(start, datetime(2026, 10, 26, tzinfo=UTC), entity_id="sensor.operation", limit=3)
    assert rows == [(ns, "heat"), (ns, "dhw")]
    with pytest.raises(InfluxBackfillError, match="limit"):
        client.diagnostic_states_ns(start, datetime(2026, 10, 26, tzinfo=UTC), entity_id="sensor.operation", limit=1)


def test_export_is_read_only_whitelisted_and_handles_missing_and_dst(monkeypatch):
    from custom_components.home_energy_intelligence import evidence_export as module
    record = {"local_date": "2026-10-25", "source_key": "equipment.hp", "source_entity_id": "sensor.meter", "meter_kwh": 3, "categories": {"unknown": 2}, "source_provenance": {"energy_entity_id": "sensor.meter", "password": "SECRET"}}
    database = SimpleNamespace(electricity_recovery_evidence_range=lambda *_, **__: {"records": [record], "ambiguous": []})
    state = SimpleNamespace(last_updated=datetime(2026, 10, 25, 1, tzinfo=UTC), state="idle", attributes={"hvac_action": "idle", "token": "SECRET"})
    reader = SimpleNamespace(async_history=AsyncMock(return_value={"sensor.operation": [state]}))
    coordinator = SimpleNamespace(timezone="Europe/Ljubljana", config={"equipment": [{"equipment_id": "hp", "display_name": "HP", "kind": "heat_pump", "energy_entity": "sensor.meter"}]}, database=database, reader=reader)
    async def executor(fn, *args): return fn(*args)
    hass = SimpleNamespace(async_add_executor_job=executor)
    async def run(start=date(2026, 10, 25), end=date(2026, 10, 25)):
        return await build_evidence_export(hass, coordinator, start, end, "hp", "sensor.operation")
    text = asyncio.run(run())
    output = json.loads(text)
    assert "SECRET" not in text and output["persisted_recovered_records"][0]["unknown_kwh"] == 2
    assert output["status"] == "incomplete" and output["influx_status"] == "not configured"
    args = reader.async_history.await_args.args
    assert (args[2] - args[1]).total_seconds() == 25 * 3600
    asyncio.run(run(date(2026, 3, 29), date(2026, 3, 29)))
    args = reader.async_history.await_args.args
    assert (args[2] - args[1]).total_seconds() == 23 * 3600
    with pytest.raises(ValueError, match="31"):
        asyncio.run(run(date(2026, 10, 1), date(2026, 11, 1)))
    monkeypatch.setattr(module, "MAX_OBSERVATIONS", 0)
    with pytest.raises(ValueError, match="Observation limit"):
        asyncio.run(run())
    monkeypatch.setattr(module, "MAX_OBSERVATIONS", 10000)
    monkeypatch.setattr(module, "MAX_BYTES", 10)
    with pytest.raises(ValueError, match="size"):
        asyncio.run(run())


def test_export_command_requires_admin():
    from homeassistant.exceptions import Unauthorized
    from custom_components.home_energy_intelligence.websocket import ws_ecodan_evidence_export
    with pytest.raises(Unauthorized):
        ws_ecodan_evidence_export(None, SimpleNamespace(user=SimpleNamespace(is_admin=False)), {"id": 1})


def test_export_command_admin_receives_json(monkeypatch):
    import inspect
    from unittest.mock import Mock
    from custom_components.home_energy_intelligence import websocket
    monkeypatch.setattr(websocket, "_coordinator", lambda _: SimpleNamespace(timezone="Europe/Ljubljana"))
    monkeypatch.setattr(websocket, "build_evidence_export", AsyncMock(return_value='{"status":"incomplete"}'))
    connection = SimpleNamespace(send_result=Mock(), send_error=Mock())
    command = inspect.unwrap(websocket.ws_ecodan_evidence_export)
    asyncio.run(command(None, connection, {"id": 1, "equipment_id": "hp", "state_entity": "sensor.operation", "start_date": "2026-10-01", "end_date": "2026-10-31"}))
    assert connection.send_result.call_args.args[1]["json"] == '{"status":"incomplete"}'


def test_export_keeps_separate_sources_and_conflicts(monkeypatch):
    from custom_components.home_energy_intelligence import evidence_export as module
    stamp = datetime(2026, 10, 25, 1, tzinfo=UTC)
    ns = 1792890000000000000
    class FakeInflux:
        def __init__(self, **kwargs): pass
        def diagnostic_states_ns(self, *args, **kwargs): return [(ns, "heat"), (ns, "dhw"), (ns+500, "dhw")]
    monkeypatch.setattr(module, "InfluxBackfillClient", FakeInflux)
    state = SimpleNamespace(last_updated=stamp, state="idle", attributes={"token": "secret"})
    coord = SimpleNamespace(timezone="Europe/Ljubljana", config={"equipment": [{"equipment_id": "hp", "display_name": "HP", "kind": "heat_pump"}], "influx_enabled": True, "influx_host": "host", "influx_database": "homeassistant", "influx_password": "secret"}, database=SimpleNamespace(electricity_recovery_evidence_range=lambda *_, **__:{"records": [], "ambiguous": []}), reader=SimpleNamespace(async_history=AsyncMock(return_value={"sensor.operation": [state]})))
    async def executor(fn, *args): return fn(*args)
    text = asyncio.run(build_evidence_export(SimpleNamespace(async_add_executor_job=executor), coord, date(2026, 10, 25), date(2026, 10, 25), "hp", "sensor.operation"))
    result = json.loads(text)
    assert len(result["influx_observations"]) == 3 and len(result["recorder_observations"]) == 1
    assert any(x.get("influx_values") == ["dhw", "heat"] for x in result["exact_conflicts"])
    assert result["near_timestamp_differences"]
    assert "secret" not in text


def test_export_leaves_actual_sqlite_evidence_unchanged(tmp_path):
    import sqlite3
    from custom_components.home_energy_intelligence.database import EnergyDatabase
    db = EnergyDatabase(tmp_path / "evidence.sqlite3")
    db.initialize()
    with db.connect() as connection:
        before = list(connection.iterdump())
    with db.connect_readonly() as connection:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("DELETE FROM electricity_recovery_evidence")
    coordinator = SimpleNamespace(timezone="Europe/Ljubljana", config={"equipment": [{"equipment_id": "hp", "display_name": "HP", "kind": "heat_pump"}]}, database=db, reader=SimpleNamespace(async_history=AsyncMock(return_value={})))
    async def executor(fn, *args): return fn(*args)
    asyncio.run(build_evidence_export(SimpleNamespace(async_add_executor_job=executor), coordinator, date(2026, 10, 25), date(2026, 10, 25), "hp", "sensor.operation"))
    with db.connect() as connection:
        assert list(connection.iterdump()) == before

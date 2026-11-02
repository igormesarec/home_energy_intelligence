from datetime import UTC, datetime, timedelta

import pytest

from custom_components.home_energy_intelligence.influx import InfluxBackfillClient, InfluxBackfillError
import custom_components.home_energy_intelligence.influx as influx_module


def test_influx_backfill_builds_read_only_query():
    client = InfluxBackfillClient(host="localhost", port=8086, database="homeassistant")
    query = client.build_query("temperature", "value", datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC), entity_tag="outdoor_temperature")
    assert query.startswith('SELECT "value"')
    assert "INTO" not in query
    assert '"entity_id" = \'outdoor_temperature\'' in query


def test_unicode_measurement_and_identifier_escaping_with_autogen_policy():
    client = InfluxBackfillClient(host="localhost", port=8086, database="homeassistant",
                                  retention_policy="autogen")
    query = client.build_query('temperature °C "outside"', 'value °C',
                               datetime(2026, 1, 1, tzinfo=UTC),
                               datetime(2026, 1, 2, tzinfo=UTC),
                               entity_tag="sensor.o'hare")
    assert 'FROM "autogen"."temperature °C \\"outside\\""' in query
    assert 'SELECT "value °C"' in query
    assert "sensor.o\\'hare" in query


def test_credentials_are_not_placed_in_query_url(monkeypatch):
    captured = {}
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_args): return None
        def read(self): return b'{"results":[{}]}'
    def fake_open(request, **_kwargs):
        captured["url"] = request.full_url
        captured["authorization"] = request.headers.get("Authorization")
        captured["method"] = request.method
        return Response()
    monkeypatch.setattr(influx_module, "urlopen", fake_open)
    client = InfluxBackfillClient(host="localhost", port=8086, database="homeassistant",
                                  username="user", password="secret")
    client._query("SELECT 1")
    assert "secret" not in captured["url"] and "user" not in captured["url"]
    assert captured["authorization"].startswith("Basic ")
    assert captured["method"] == "POST"


def test_bounded_historical_temperature_backfill_preserves_timestamps_and_deduplicates(monkeypatch):
    client = InfluxBackfillClient(host="localhost", port=8086, database="homeassistant",
                                  retention_policy="autogen")
    first = datetime(2026, 10, 24, 12, tzinfo=UTC)
    second = datetime(2026, 10, 25, 12, tzinfo=UTC)
    calls = []
    def fake_query(query):
        calls.append(query)
        return [(first, 10.5), (second, 11.0)]
    monkeypatch.setattr(client, "_query", fake_query)
    rows = client.query("temperature °C", "value",
                        datetime(2026, 10, 24, tzinfo=UTC),
                        datetime(2026, 10, 26, tzinfo=UTC),
                        entity_tag="sensor.outdoor")
    assert rows == [(first, 10.5), (second, 11.0)]
    assert len(calls) == 2
    assert all("LIMIT" in query and "time >=" in query and "time <" in query for query in calls)


def test_text_states_keep_fresh_preceding_value_and_exact_timestamps(monkeypatch):
    start = datetime(2026, 10, 25, tzinfo=UTC)
    client = InfluxBackfillClient(host="localhost", port=8086, database="homeassistant")
    queries = []
    def query(statement):
        queries.append(statement)
        if "ORDER BY time DESC" in statement:
            return [(start-timedelta(minutes=30), "heat")]
        return [(start+timedelta(minutes=5), "heating_water"),
                (start+timedelta(minutes=10), "unavailable")]
    monkeypatch.setattr(client, "_query_values", query)
    rows = client.query_states(start, start+timedelta(hours=1), entity_id="climate.aurora", domain="climate")
    assert [state for _, state in rows] == ["heat", "heating_water", "unavailable"]
    assert '"entity_id" = \'aurora\'' in queries[0]
    assert '"domain" = \'climate\'' in queries[0]
    assert all('FROM "autogen"."state"' in item and "SELECT" in item for item in queries)


def test_state_queries_reject_conflicting_duplicates_and_budget_overflow(monkeypatch):
    start = datetime(2026, 1, 1, tzinfo=UTC)
    client = InfluxBackfillClient(host="localhost", port=8086, database="homeassistant", max_points=2)
    def conflicting(query):
        return [] if "ORDER BY time DESC" in query else [(start, "heat"), (start, "cool")]
    monkeypatch.setattr(client, "_query_values", conflicting)
    with pytest.raises(InfluxBackfillError, match="conflicting"):
        client.query_states(start, start+timedelta(hours=1), entity_id="sensor.mode", domain="sensor")
    client.max_points = 1
    monkeypatch.setattr(client, "_query_values", lambda q: [] if "DESC" in q else [(start, "heat"), (start+timedelta(minutes=1), "cool")])
    with pytest.raises(InfluxBackfillError, match="point limit"):
        client.query_states(start, start+timedelta(hours=1), entity_id="sensor.mode", domain="sensor")


def test_counter_baseline_is_bounded_and_missing_is_not_invented(monkeypatch):
    start = datetime(2026, 9, 1, tzinfo=UTC)
    client = InfluxBackfillClient(host="localhost", port=8086, database="homeassistant")
    calls = []
    monkeypatch.setattr(client, "_query", lambda q: calls.append(q) or [(start-timedelta(minutes=15), 100.0)])
    monkeypatch.setattr(client, "query", lambda *args, **kwargs: [(start+timedelta(minutes=30), 101.0)])
    result = client.query_counter("kWh", "value", start, start+timedelta(hours=1), entity_tag="energy")
    assert result[0] == (start-timedelta(minutes=15), 100.0)
    assert "ORDER BY time DESC LIMIT 1" in calls[0]
    monkeypatch.setattr(client, "_query", lambda q: [])
    assert client.query_counter("kWh", "value", start, start+timedelta(hours=1), entity_tag="energy") == [(start+timedelta(minutes=30), 101.0)]

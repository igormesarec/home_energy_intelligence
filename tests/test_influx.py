from datetime import UTC, datetime

from custom_components.home_energy_intelligence.influx import InfluxBackfillClient


def test_influx_backfill_builds_read_only_query():
    client = InfluxBackfillClient(host="localhost", port=8086, database="ha")
    query = client.build_query("temperature", "value", datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC), entity_tag="outdoor_temperature")
    assert query.startswith('SELECT "value"')
    assert "INTO" not in query
    assert '"entity_id" = \'outdoor_temperature\'' in query

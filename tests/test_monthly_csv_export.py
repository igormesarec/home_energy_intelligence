import csv
import inspect
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import voluptuous as vol

from custom_components.home_energy_intelligence import websocket
from custom_components.home_energy_intelligence.monthly import monthly_metric_catalog


@pytest.mark.asyncio
async def test_full_catalog_csv_validation_and_evidence(hass, monkeypatch):
    catalog = monthly_metric_catalog([])
    metrics = [item["id"] for item in catalog]
    assert len(metrics) > 30
    reference = next(item for item in catalog if item["id"].startswith("recovered_reference_"))
    present = metrics[0]
    row = {"month": "2026-09", present: 12.073, reference["id"]: 2.741,
           "quality": "partial", "primary_cop_quality": "unavailable",
           "recovered_metric_evidence": {reference["id"]: [{"source_entity_id": "sensor.ac", "coverage_seconds": 3600}]},
           "recovered_excluded": [{"reason": "existing_electrical_record"}]}
    report = {"metric_catalog": catalog, "rows": [row]}
    monkeypatch.setattr(websocket, "_coordinator", lambda _: SimpleNamespace(config={}))
    monkeypatch.setattr(websocket, "_monthly_report_payload", AsyncMock(return_value=report))
    message = {"id": 1, "type": "home_energy_intelligence/monthly_report_csv", "end_month": "2026-09", "count": 1, "metric_ids": metrics}
    validated = websocket.ws_monthly_report_csv._ws_schema(message)
    connection = SimpleNamespace(send_result=Mock(), send_error=Mock())
    await inspect.unwrap(websocket.ws_monthly_report_csv)(hass, connection, validated)
    result = connection.send_result.call_args.args[1]
    parsed = next(csv.DictReader(io.StringIO(result["csv"])))
    assert result["metric_ids"] == metrics
    assert parsed[f"{present} ({catalog[0]['unit']})"] == "12.073"
    assert parsed[f"{reference['id']} ({reference['unit']})"] == "2.741"
    assert "sensor.ac" in parsed[f"{reference['id']}_evidence"]
    assert "existing_electrical_record" in parsed["recovered_excluded"]
    assert parsed["quality"] == "partial" and parsed[f"{metrics[1]} ({catalog[1]['unit']})"] == ""
    connection.send_result.reset_mock()
    await inspect.unwrap(websocket.ws_monthly_report_csv)(hass, connection, {**message, "metric_ids": ["invalid_metric"]})
    connection.send_error.assert_called_once()
    connection.send_result.assert_not_called()
    with pytest.raises(vol.Invalid):
        websocket.ws_monthly_report_csv._ws_schema({**message, "metric_ids": []})

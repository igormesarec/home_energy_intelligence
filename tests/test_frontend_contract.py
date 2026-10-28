import json
from pathlib import Path


ROOT = Path(__file__).parents[1]
INTEGRATION = ROOT / "custom_components" / "home_energy_intelligence"
GUIDED_STEPS = {
    "general", "heat_pumps", "air_conditioners", "solar_grid", "temperatures",
    "weather", "data_sources", "data_quality",
}


def test_dashboard_has_no_installation_specific_equipment_keys():
    source = (INTEGRATION / "frontend" / "hei-panel.js").read_text(encoding="utf-8")
    for forbidden in ('"ecodan"', '"ac_1"', '"ac_2"', '"ac_3"'):
        assert forbidden not in source
    assert "overview.equipment" in source
    assert "temperature_sources" in source


def test_english_and_slovenian_cover_every_guided_step_and_field():
    translations = [
        json.loads((INTEGRATION / "translations" / name).read_text(encoding="utf-8"))
        for name in ("en.json", "sl.json")
    ]
    for document in translations:
        for section in ("config", "options"):
            steps = document[section]["step"]
            assert GUIDED_STEPS <= set(steps)
            for step in GUIDED_STEPS:
                assert steps[step].get("title")
                assert steps[step].get("description")
                assert steps[step].get("data") is not None
        assert set(document["config"]["error"]) == set(document["options"]["error"])


def test_dashboard_role_contract_keeps_settings_admin_only_not_history():
    source = (INTEGRATION / "websocket.py").read_text(encoding="utf-8")
    history_prefix = source[source.rindex(
        "@websocket_api.websocket_command", 0, source.index("async def ws_history")
    ):source.index("async def ws_history")]
    settings_prefix = source[source.rindex("@websocket_api.websocket_command", 0, source.index("async def ws_settings")):source.index("async def ws_settings")]
    assert "require_admin" not in history_prefix
    assert "require_admin" in settings_prefix

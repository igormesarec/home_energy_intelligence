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


def test_indoor_and_outdoor_cards_share_one_component_contract():
    source = (INTEGRATION / "frontend" / "hei-panel.js").read_text(encoding="utf-8")
    assert 'this.temperatureCard("indoor")' in source
    assert 'this.temperatureCard("outdoor")' in source
    assert "latest_value" in source and "latest_timestamp" in source
    assert "coverage_percent" in source
    assert "representative_${role}_source_id" in source


def test_all_history_presets_are_backend_resolved_and_refresh_state_is_preserved():
    frontend = (INTEGRATION / "frontend" / "hei-panel.js").read_text(encoding="utf-8")
    backend = (INTEGRATION / "periods.py").read_text(encoding="utf-8")
    for preset in (
        "today", "yesterday", "day_2", "day_7", "this_week", "previous_week",
        "last_7_completed", "this_month", "previous_month", "older_month",
        "this_year", "previous_year", "current_season", "previous_season", "custom",
    ):
        assert preset in frontend or preset in backend
    assert "new Date(" not in frontend
    assert "preset:this.historyPreset" in frontend


def test_csv_uses_unpaginated_selected_period_query():
    source = (INTEGRATION / "websocket.py").read_text(encoding="utf-8")
    csv_handler = source[source.index("async def ws_csv"):source.index("async def ws_settings")]
    assert "history_period" in csv_handler
    assert "limit=366" not in csv_handler

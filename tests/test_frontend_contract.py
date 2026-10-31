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
    for handler in (
        "ws_temperature_diagnostics", "ws_reprocess_temperature_history",
        "ws_temperature_backfill_preview", "ws_temperature_backfill_start",
        "ws_temperature_backfill_status", "ws_temperature_backfill_cancel",
    ):
        prefix = source[source.rindex(
            "@websocket_api.websocket_command", 0, source.index(f"async def {handler}")
        ):source.index(f"async def {handler}")]
        assert "require_admin" in prefix


def test_v02_frontend_cache_key_uses_content_hash_and_keeps_sidebar_path():
    panel = (INTEGRATION / "panel.py").read_text(encoding="utf-8")
    manifest = json.loads((INTEGRATION / "manifest.json").read_text(encoding="utf-8"))
    const = (INTEGRATION / "const.py").read_text(encoding="utf-8")
    assert manifest["version"] == "0.2.1"
    assert 'VERSION: Final = "0.2.1"' in const
    assert "sha256" in panel and "frontend_build" in panel
    assert "?v={VERSION}-{frontend_build}" in panel
    assert "frontend_url_path=PANEL_URL" in panel


def test_temperature_recovery_ui_has_distinct_truthful_admin_actions():
    frontend = (INTEGRATION / "frontend" / "hei-panel.js").read_text(encoding="utf-8")
    for label in (
        "Diagnose", "Repair existing summaries",
        "Preview temperature-only backfill", "Start temperature-only backfill",
        "No records recovered",
    ):
        assert label.lower() in frontend.lower()
    assert "no_records_recovered_action_required" in frontend
    assert "preview_no_recoverable_records_action_required" in frontend
    assert "temperature_summaries_before" in frontend
    assert "temperature_summaries_after" in frontend


def test_indoor_and_outdoor_cards_share_one_component_contract():
    source = (INTEGRATION / "frontend" / "hei-panel.js").read_text(encoding="utf-8")
    assert 'this.temperatureCard("indoor")' in source
    assert 'this.temperatureCard("outdoor")' in source
    assert "latest_value" in source and "latest_timestamp" in source
    assert "coverage_percent" in source
    assert "representative_${role}_source_id" in source


def test_single_day_temperature_charts_share_bounded_hourly_endpoint():
    frontend = (INTEGRATION / "frontend" / "hei-panel.js").read_text(encoding="utf-8")
    backend = (INTEGRATION / "websocket.py").read_text(encoding="utf-8")
    database = (INTEGRATION / "database.py").read_text(encoding="utf-8")
    assert 'type: "home_energy_intelligence/temperature_timeline"' in frontend
    assert 'period.start_date === period.end_date ? "hourly" : "daily"' in frontend
    assert "this.historyPreset === \"today\" ? this.temperatureTimeline" not in frontend
    assert "return this.temperatureTimeline(role)" in frontend
    assert "Daily summary available, but hourly temperature history has not been archived/backfilled" in frontend
    assert "Gaps are not interpolated" in frontend
    assert 'tabindex="0"' in frontend and "temp-hit" in frontend
    assert 'f"{DOMAIN}/temperature_timeline"' in backend
    endpoint_prefix = backend[backend.rindex(
        "@websocket_api.websocket_command", 0, backend.index("async def ws_temperature_timeline")
    ):backend.index("async def ws_temperature_timeline")]
    assert "require_admin" not in endpoint_prefix
    assert "maximum_days=7 if resolution == \"hourly\" else 3660" in backend
    assert "result.extend(" in database and "temperature_history_range(\"hourly\"" in database


def test_temperature_timeline_refreshes_with_period_and_keeps_mobile_targets_readable():
    source = (INTEGRATION / "frontend" / "hei-panel.js").read_text(encoding="utf-8")
    assert source.count("await this.loadTemperatureTimeline(") >= 2
    assert ".temp-hit{fill:transparent" in source
    assert 'r="8"' in source
    assert ".temp-chart-shell{width:100%;max-width:100%;margin-top:10px;overflow:hidden}" in source
    assert "aspect-ratio:320/190" in source
    assert "clipPath" in source and 'class="temp-plot" clip-path=' in source
    assert "new Date(" not in source


def test_response_charts_keep_normalized_units_and_zero_cooling_distinct():
    source = (INTEGRATION / "frontend" / "hei-panel.js").read_text(encoding="utf-8")
    assert 'item.axis==="normalized"' in source
    assert 'Measured AC cooling electricity: 0.0 kWh' in source
    assert 'this.axisTicks(0,maxY)' in source and 'kWh/m²/day' in source


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


def test_overview_and_history_share_today_period_controls_and_cutoff():
    frontend = (INTEGRATION / "frontend" / "hei-panel.js").read_text(encoding="utf-8")
    websocket = (INTEGRATION / "websocket.py").read_text(encoding="utf-8")
    assert 'this.historyPreset = "today"' in frontend
    assert frontend.count("${this.periodControls()}") == 4
    assert "cutoff_utc:this.overviewData.cutoff_utc" in frontend
    assert "replace_with_partial_day(" in websocket and "stored_records, partial_today" in websocket
    assert websocket.count("_current_day_snapshot(") >= 3
    assert '"partial": True, "finalized": False' in websocket


def test_energy_split_exposes_other_hvac_unallocated_and_signed_discrepancy():
    source = (INTEGRATION / "frontend" / "hei-panel.js").read_text(encoding="utf-8")
    assert "known_other_hvac_kwh" in source
    assert "Other HVAC (fan/dry)" in source
    assert "Unallocated" in source
    assert "Δ discrepancy (signed)" in source


def test_representative_outdoor_source_is_not_replaced_by_first_role_match():
    source = (INTEGRATION / "frontend" / "hei-panel.js").read_text(encoding="utf-8")
    method = source[source.index("representativeSource(role)"):source.index("selectedRecord()")]
    assert "representative_${role}_source_id" in method
    assert "item.role === role" not in method
    assert "this.representativeSource(role)" in source[source.index("scatterChart()") - 500:]


def test_cutoff_is_rendered_in_home_assistant_timezone():
    frontend = (INTEGRATION / "frontend" / "hei-panel.js").read_text(encoding="utf-8")
    websocket = (INTEGRATION / "websocket.py").read_text(encoding="utf-8")
    assert "cutoff_local" in frontend and "overviewData?.timezone" in frontend
    assert '"cutoff_local"' in websocket and '"timezone": coordinator.timezone' in websocket


def test_unconfigured_representatives_are_explicit_and_never_claim_coverage():
    source = (INTEGRATION / "frontend" / "hei-panel.js").read_text(encoding="utf-8")
    assert "Not configured" in source
    assert "No coverage percentage is inferred" in source
    assert 'return "Not configured"' in source
    assert "weather provider or water-circuit sensor is never substituted" in source


def test_v02_thermal_profile_uses_measured_heat_and_never_ac_cop():
    source = (INTEGRATION / "frontend" / "hei-panel.js").read_text(encoding="utf-8")
    backend = (INTEGRATION / "websocket.py").read_text(encoding="utf-8")
    analytics = (INTEGRATION / "analytics.py").read_text(encoding="utf-8")
    assert "Whole-House Heating Electricity" in source
    assert "Whole-House Energy / Toplotni profil hiše" in source
    assert "minimum outdoor coverage" in source
    assert "Insufficient validated data" in source
    assert "Ecodan Space-Heating Performance Curve" in source
    assert "Ecodan DHW Performance Curve" in source
    assert "AC COP is never inferred" in source
    assert '"response_profile"' in backend
    assert '"representative_outdoor_source_id"' in backend
    assert "heating_response_profile" in backend
    assert "thermal_performance_points" in backend
    assert "heat_pump_heating_kwh" in analytics and "ac_heating_kwh" in analytics


def test_mobile_controls_hide_irrelevant_fields_and_pager_has_real_bounds():
    source = (INTEGRATION / "frontend" / "hei-panel.js").read_text(encoding="utf-8")
    assert 'this.historyPreset==="older_month"?"visible":""' in source
    assert 'this.historyPreset==="custom"?"visible":""' in source
    assert "this.historyTotal" in source and "button:disabled" in source
    assert "-webkit-overflow-scrolling:touch" in source
    assert 'querySelector(".older-month")?.addEventListener("input"' in source
    assert 'querySelector(".start-date")?.addEventListener("input"' in source
    assert 'querySelector(".end-date")?.addEventListener("input"' in source
    assert "Math.floor(((response.total||1)-1)/90)*90" in source


def test_partial_equipment_is_not_rendered_as_a_normal_zero_bar():
    source = (INTEGRATION / "frontend" / "hei-panel.js").read_text(encoding="utf-8")
    method = source[source.index("equipmentComparison(category)"):source.index(
        "temperatureCard(role)"
    )]
    assert "row.available&&row.classification_complete" in method
    assert "Partial device meters remain in the evidence table" in method
    assert "not rendered as zero" in method


def test_temperature_pipeline_diagnostics_and_bounded_repair_are_exposed():
    frontend = (INTEGRATION / "frontend" / "hei-panel.js").read_text(encoding="utf-8")
    backend = (INTEGRATION / "websocket.py").read_text(encoding="utf-8")
    coordinator = (INTEGRATION / "coordinator.py").read_text(encoding="utf-8")
    assert "temperature_pipeline" in frontend and "temperature_pipeline" in backend
    assert "temperature_diagnostics" in frontend and "temperature_diagnostics" in backend
    assert "reprocess_temperature_history" in frontend and "reprocess_temperature_history" in backend
    assert "Range must be ordered and at most 31 days" in backend
    assert "merge_daily_temperatures" in coordinator
    assert "merge_hourly_temperatures" in coordinator
    assert "bounded_recorder_temperature_repair" in coordinator
    assert "pipeline_stage" in frontend and "missing_intervals" in coordinator


def test_energy_split_quality_does_not_call_an_intentional_partial_day_missing():
    source = (INTEGRATION / "frontend" / "hei-panel.js").read_text(encoding="utf-8")
    method = source[source.index("splitQuality(record)"):source.index("splitChart()")]
    chart = source[source.index("splitChart()"):source.index("scatterChart()")]
    for label in (
        "incomplete period", "missing meter", "incomplete classification",
        "estimated allocation", "temperature evidence missing",
    ):
        assert label in method
    assert "!r.energy?.complete" not in chart
    assert "this.splitQuality(r)" in chart
    assert "known_selected_heating_kwh" in method
    assert "known_space_heating_kwh ?? null" in chart
    assert "known_dhw_kwh ?? null" in chart
    assert "known_cooling_kwh ?? null" in chart
    assert "Unavailable is not measured zero" in chart


def test_overview_and_thermal_use_the_same_backend_response_points():
    source = (INTEGRATION / "frontend" / "hei-panel.js").read_text(encoding="utf-8")
    scatter = source[source.index("scatterChart()"):source.index("responseProfile()")]
    thermal = source[source.index("responseProfile()"):source.index("thermalView()")]
    assert "this.history" not in scatter
    assert "responseProfileData" in scatter
    assert 'status==="validated"' in scatter
    assert "exclusion_reasons" in scatter
    assert "responseProfileData" in thermal


def test_quality_ui_exposes_independent_derived_layers_and_legacy_provenance():
    source = (INTEGRATION / "frontend" / "hei-panel.js").read_text(encoding="utf-8")
    analytics = (INTEGRATION / "analytics.py").read_text(encoding="utf-8")
    assert "Derived from independent coverage layers" in source
    assert "stored legacy label" in source
    assert 'layer("dates"' in source
    assert 'layer("meter"' in source
    assert 'layer("allocation"' in source
    assert 'temperatureLayer("indoor")' in source
    assert 'temperatureLayer("outdoor")' in source
    assert "derived_from_independent_coverage_layers_v2" in analytics
    assert "stored_quality_provenance" in analytics

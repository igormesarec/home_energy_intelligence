import json
import pytest

from custom_components.home_energy_intelligence.configuration import (
    equipment_from_config, mapping_revision, temperatures_from_config,
    validate_no_duplicate_meters,
)


def test_variable_equipment_counts_and_missing_optional_sources():
    config = {"equipment": json.dumps([
        {"equipment_id": "hp_a", "display_name": "Main HP", "kind": "heat_pump",
         "energy_entity": "sensor.hp_energy", "state_entity": "sensor.hp_state"},
        {"equipment_id": "ac_b", "display_name": "Office AC", "kind": "air_conditioner",
         "enabled": False},
    ])}
    items = equipment_from_config(config)
    assert [item.equipment_id for item in items] == ["hp_a", "ac_b"]
    assert items[0].return_temperature_entity is None
    assert items[1].enabled is False


def test_reconfiguration_changes_entity_without_changing_equipment_identity():
    first = {"equipment": json.dumps([{"equipment_id": "hp", "display_name": "HP",
              "kind": "heat_pump", "energy_entity": "sensor.old"}])}
    second = {"equipment": json.dumps([{"equipment_id": "hp", "display_name": "HP",
               "kind": "heat_pump", "energy_entity": "sensor.new"}])}
    assert equipment_from_config(first)[0].equipment_id == equipment_from_config(second)[0].equipment_id
    assert mapping_revision(first)["equipment"][0]["energy_entity"] == "sensor.old"
    assert mapping_revision(second)["equipment"][0]["energy_entity"] == "sensor.new"


def test_optional_verified_dates_and_source_mapping_remain_per_equipment():
    mappings = [{"entity_id": "sensor.old", "start_date": "2026-08-13", "end_date": "2026-09-30"}, {"entity_id": "sensor.new", "start_date": "2026-10-01", "end_date": "2026-12-31"}]
    items = equipment_from_config({"equipment": json.dumps([
        {"equipment_id": "hp", "display_name": "HP", "kind": "heat_pump", "commissioning_date": "2026-08-13", "historical_energy_mappings": mappings},
        {"equipment_id": "ac", "display_name": "AC", "kind": "air_conditioner"}])})
    assert items[0].commissioning_date == "2026-08-13" and items[0].historical_energy_mappings == mappings
    assert items[1].commissioning_date is None and items[1].historical_energy_mappings == []


def test_overlapping_historical_mappings_are_rejected(monkeypatch):
    from types import SimpleNamespace
    from custom_components.home_energy_intelligence import config_wizard
    monkeypatch.setattr(config_wizard, "_check_source", lambda *args, **kwargs: None)
    monkeypatch.setattr(config_wizard, "_circuit_2k", lambda *args: False)
    handler = SimpleNamespace(hass=SimpleNamespace(states=SimpleNamespace(get=lambda _: object())), equipment=[])
    values = {"configure": True, "energy_entity": "sensor.hp", "state_entity": "sensor.mode", "state_map": "{}", "commissioning_date": "2026-08-13", "historical_energy_mappings": json.dumps([
        {"entity_id": "sensor.one", "start_date": "2026-08-01", "end_date": "2026-09-30"},
        {"entity_id": "sensor.two", "start_date": "2026-09-15", "end_date": "2026-12-31"}])}
    errors = config_wizard._Wizard._accept_equipment(handler, "heat_pump", values)
    assert errors and handler.equipment == []


@pytest.mark.parametrize(("mappings", "valid"), [
    ([{"entity_id": "sensor.one", "start_date": "2026-01-01"}], True),
    ([{"entity_id": "sensor.one", "start_date": "2026-01-01", "end_date": "2026-09-30"}, {"entity_id": "sensor.two", "start_date": "2026-10-01"}], True),
    ([{"entity_id": "sensor.one", "start_date": "2026-01-01"}, {"entity_id": "sensor.two", "start_date": "2026-10-01"}], False),
    ([{"entity_id": "sensor.one", "start_date": "2026-10-01"}, {"entity_id": "sensor.two", "start_date": "2026-01-01", "end_date": "2026-10-01"}], False),
    ([{"entity_id": "sensor.one", "start_date": "2026-01-01", "end_date": None}], False),
])
def test_optional_mapping_end_date_validation(monkeypatch, mappings, valid):
    from types import SimpleNamespace
    from custom_components.home_energy_intelligence import config_wizard
    monkeypatch.setattr(config_wizard, "_check_source", lambda *args, **kwargs: None)
    monkeypatch.setattr(config_wizard, "_circuit_2k", lambda *args: False)
    handler = SimpleNamespace(hass=SimpleNamespace(states=SimpleNamespace(get=lambda _: object())), equipment=[])
    values = {"configure": True, "energy_entity": "sensor.hp", "state_entity": "sensor.mode", "state_map": "{}", "historical_energy_mappings": json.dumps(mappings)}
    errors = config_wizard._Wizard._accept_equipment(handler, "heat_pump", values)
    assert bool(errors) is not valid
    if valid:
        assert handler.equipment[0]["historical_energy_mappings"] == mappings


def test_duplicate_physical_meter_is_rejected_and_temperature_quality_is_per_source():
    config = {"equipment": json.dumps([
        {"equipment_id": "hp", "display_name": "HP", "kind": "heat_pump",
         "energy_entity": "sensor.shared"}
    ]), "temperature_sources": json.dumps([
        {"source_id": "outside", "display_name": "Outside", "entity_id": "sensor.outside",
         "role": "outdoor", "quality": {"minimum": -35, "maximum": 50,
         "invalid_values": [-55, 155], "freshness_minutes": 20}}
    ])}
    assert not validate_no_duplicate_meters(equipment_from_config(config), {"pv": "sensor.shared"})
    quality = temperatures_from_config(config)[0].quality
    assert quality.minimum == -35 and quality.freshness_minutes == 20


def test_multiple_heat_pumps_zero_ac_no_pv_and_no_temperatures_are_valid_shapes():
    config = {"equipment": json.dumps([
        {"equipment_id": "hp-1", "display_name": "Downstairs", "kind": "heat_pump",
         "enabled": True, "energy_entity": "sensor.hp1"},
        {"equipment_id": "hp-2", "display_name": "Upstairs", "kind": "heat_pump",
         "enabled": False, "energy_entity": "sensor.hp2"},
    ]), "site_sources": {}, "temperature_sources": "[]"}
    equipment = equipment_from_config(config)
    assert [item.kind for item in equipment] == ["heat_pump", "heat_pump"]
    assert not [item for item in equipment if item.kind == "air_conditioner"]
    assert temperatures_from_config(config) == []
    assert validate_no_duplicate_meters(equipment, {})


def test_climate_temperature_source_uses_current_temperature_attribute_explicitly():
    config = {"temperature_sources": json.dumps([{
        "source_id": "living-room", "display_name": "Living room", "role": "indoor",
        "entity_id": "climate.living_room", "enabled": True,
    }])}
    source = temperatures_from_config(config)[0]
    assert source.attribute == "current_temperature"
    revision = mapping_revision(config, effective_at=None)["temperatures"][0]
    assert revision == {
        "source_id": "living-room", "entity_id": "climate.living_room",
        "attribute": "current_temperature", "role": "indoor", "enabled": True,
    }

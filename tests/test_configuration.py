import json

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

"""Versioned, fully user-configurable source mappings."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
import json
from typing import Any


@dataclass(frozen=True, slots=True)
class TemperatureQuality:
    minimum: float = -50.0
    maximum: float = 60.0
    invalid_values: tuple[float, ...] = (-55.0, 155.0)
    maximum_change_per_minute: float | None = 8.0
    freshness_minutes: int = 360
    missing_after_minutes: int = 360


@dataclass(frozen=True, slots=True)
class EquipmentMapping:
    equipment_id: str
    display_name: str
    kind: str
    enabled: bool = True
    energy_entity: str | None = None
    power_entity: str | None = None
    state_entity: str | None = None
    room_temperature_entity: str | None = None
    supply_temperature_entity: str | None = None
    return_temperature_entity: str | None = None
    state_attribute: str | None = None
    state_map: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TemperatureMapping:
    source_id: str
    display_name: str
    entity_id: str
    role: str
    attribute: str | None = None
    enabled: bool = True
    quality: TemperatureQuality = field(default_factory=TemperatureQuality)


def parse_json_records(value: str | list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if value in (None, ""):
        return []
    parsed = json.loads(value) if isinstance(value, str) else value
    if not isinstance(parsed, list) or not all(isinstance(item, dict) for item in parsed):
        raise ValueError("mapping must be a JSON list of objects")
    return parsed


def equipment_from_config(config: dict[str, Any]) -> list[EquipmentMapping]:
    records = parse_json_records(config.get("equipment"))
    if not records:
        # One-way compatibility for the reviewed V0.1 configuration. No runtime
        # calculation contains installation-specific entity IDs.
        legacy = []
        if config.get("ecodan_energy"):
            legacy.append({
                "equipment_id": "legacy_heat_pump", "display_name": "Heat pump",
                "kind": "heat_pump", "energy_entity": config.get("ecodan_energy"),
                "power_entity": config.get("ecodan_power"), "state_entity": config.get("ecodan_mode"),
                "state_map": {"heating_zones": "confirmed_heating", "heating_water": "dhw", "cooling": "cooling", "idle": "idle"},
            })
        for index in range(1, 4):
            if config.get(f"ac_{index}_energy"):
                legacy.append({
                    "equipment_id": f"legacy_ac_{index}", "display_name": f"AC {index}",
                    "kind": "air_conditioner", "energy_entity": config.get(f"ac_{index}_energy"),
                    "power_entity": config.get(f"ac_{index}_power"),
                    "state_entity": config.get(f"ac_{index}_climate"),
                    "room_temperature_entity": config.get(f"ac_{index}_temperature"),
                    "state_map": {"heating": "heating", "cooling": "cooling", "idle": "standby", "off": "standby"},
                })
        records = legacy
    return [EquipmentMapping(**record) for record in records]


def temperatures_from_config(config: dict[str, Any]) -> list[TemperatureMapping]:
    result = []
    for source_record in parse_json_records(config.get("temperature_sources")):
        record = dict(source_record)
        quality = TemperatureQuality(**record.pop("quality", {}))
        if not record.get("attribute") and str(record.get("entity_id", "")).startswith("climate."):
            record["attribute"] = "current_temperature"
        result.append(TemperatureMapping(**record, quality=quality))
    return result


def equipment_temperature_mappings(equipment: list[EquipmentMapping]) -> list[TemperatureMapping]:
    """Expose optional equipment temperature selectors to the common collector."""
    result = []
    roles = {
        "room_temperature_entity": ("equipment_room", TemperatureQuality(-10, 60)),
        "supply_temperature_entity": ("supply", TemperatureQuality(-10, 110)),
        "return_temperature_entity": ("return", TemperatureQuality(-10, 110)),
    }
    for item in equipment:
        if not item.enabled:
            continue
        for attribute, (role, quality) in roles.items():
            entity_id = getattr(item, attribute)
            if entity_id:
                result.append(TemperatureMapping(
                    source_id=f"{item.equipment_id}_{role}",
                    display_name=f"{item.display_name} {role.replace('_', ' ')}",
                    entity_id=entity_id, role=role,
                    attribute=("current_temperature"
                               if role == "equipment_room" and entity_id.startswith("climate.")
                               else None),
                    quality=quality,
                ))
    return result


def mapping_revision(config: dict[str, Any], *, effective_at: datetime | None = None) -> dict[str, Any]:
    """Non-secret provenance stored with derived records."""
    equipment = equipment_from_config(config)
    explicit = list(temperatures_from_config(config))
    explicit_keys = {(item.entity_id, item.role) for item in explicit}
    temperature_mappings = explicit + [
        item for item in equipment_temperature_mappings(equipment)
        if (item.entity_id, item.role) not in explicit_keys
    ]
    return {
        "effective_at": config.get("mapping_effective_at") or (effective_at or datetime.now(UTC)).isoformat(),
        "equipment": [
            asdict(item)
            for item in equipment
        ],
        "temperatures": [
            {"source_id": item.source_id, "entity_id": item.entity_id,
             "attribute": item.attribute, "role": item.role, "enabled": item.enabled}
            for item in temperature_mappings
        ],
        "thermal": {
            "source_mode": config.get("thermal_source_mode", "manual_only"),
            "ecodan_equipment_id": config.get("ecodan_thermal_equipment_id"),
            "heating_thermal_entity": config.get("heating_thermal_energy_entity"),
            "dhw_thermal_entity": config.get("dhw_thermal_energy_entity"),
            "vendor_heating_electricity_entity": config.get(
                "vendor_heating_electricity_entity"
            ),
            "vendor_dhw_electricity_entity": config.get(
                "vendor_dhw_electricity_entity"
            ),
        },
    }


def validate_no_duplicate_meters(equipment: list[EquipmentMapping], site: dict[str, str | None]) -> bool:
    meters = [item.energy_entity for item in equipment if item.enabled and item.energy_entity]
    meters += [site.get(key) for key in ("pv", "import", "export") if site.get(key)]
    return len(meters) == len(set(meters))

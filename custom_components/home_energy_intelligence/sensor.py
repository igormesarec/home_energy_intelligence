"""Low-frequency summary entities backed by daily records."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.const import UnitOfEnergy
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .analytics import derive_record_quality
from .configuration import (
    equipment_from_config, equipment_temperature_mappings, temperatures_from_config,
)


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[entry.domain][entry.entry_id]
    async_add_entities([
        LatestDailyEnergySensor(coordinator),
        LatestDataQualitySensor(coordinator),
    ], True)


class _BaseSensor(SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator) -> None:
        self.coordinator = coordinator
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{self.key}"

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(
            self.hass, f"{self.coordinator.entry.entry_id}_updated", self.async_write_ha_state
        ))

    async def async_update(self) -> None:
        self.record = await self.hass.async_add_executor_job(self.coordinator.database.latest)
        if self.record:
            equipment_ids = [
                item.equipment_id for item in equipment_from_config(self.coordinator.config)
                if item.enabled
            ]
            explicit = temperatures_from_config(self.coordinator.config)
            seen = {(item.entity_id, item.role) for item in explicit}
            generated = [
                item for item in equipment_temperature_mappings(
                    equipment_from_config(self.coordinator.config)
                ) if (item.entity_id, item.role) not in seen
            ]
            sources = {item.source_id: item for item in [*explicit, *generated]}
            representative_ids = [
                source_id for role, source_id in (
                    ("indoor", self.coordinator.config.get("representative_indoor_source_id")),
                    ("outdoor", self.coordinator.config.get("representative_outdoor_source_id")),
                )
                if source_id and source_id in sources and sources[source_id].enabled
                and sources[source_id].role == role
            ]
            self.record = derive_record_quality(
                self.record, equipment_ids=equipment_ids,
                temperature_source_ids=representative_ids,
                timezone=self.coordinator.timezone,
                response_temperature_coverage_minimum=float(
                    self.coordinator.config.get(
                        "response_temperature_coverage_minimum", 95
                    )
                ),
            )


class LatestDailyEnergySensor(_BaseSensor):
    key = "latest_daily_energy"
    _attr_name = "Latest daily whole-house electricity"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_icon = "mdi:home-lightning-bolt-outline"

    @property
    def native_value(self):
        return None if not getattr(self, "record", None) else self.record.get("household_kwh")

    @property
    def extra_state_attributes(self):
        return {} if not getattr(self, "record", None) else {
            "local_date": self.record["local_date"], "quality": self.record["quality"]
        }


class LatestDataQualitySensor(_BaseSensor):
    key = "latest_data_quality"
    _attr_name = "Latest daily data quality"
    _attr_icon = "mdi:database-check-outline"

    @property
    def native_value(self):
        return "missing" if not getattr(self, "record", None) else self.record["quality"]

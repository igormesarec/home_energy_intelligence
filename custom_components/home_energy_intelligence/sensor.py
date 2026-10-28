"""Low-frequency summary entities backed by daily records."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.const import UnitOfEnergy
from homeassistant.helpers.dispatcher import async_dispatcher_connect


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

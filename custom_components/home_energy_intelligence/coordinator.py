"""Collection, catch-up, and daily-close orchestration."""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from functools import partial
import json
from zoneinfo import ZoneInfo

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .allocation import ac_category, allocate_interval, ecodan_category
from .const import (
    CONF_CYCLE_HOURS, CONF_INFLUX_DATABASE, CONF_INFLUX_ENABLED, CONF_INFLUX_HOST,
    CONF_INFLUX_MAPPING, CONF_INFLUX_PASSWORD, CONF_INFLUX_PORT, CONF_INFLUX_USERNAME,
    CONF_SEASONAL_FALLBACK, CONF_TEMPERATURE_FRESHNESS_MINUTES,
)
from .database import EnergyDatabase
from .engine import DailyAggregator, DailyInput, DeviceHistory
from .influx import InfluxBackfillClient
from .models import NumericSample
from .recorder import (
    RecorderReader, states_to_modes, states_to_numeric,
    statistics_to_counter_samples, statistics_to_numeric_samples,
)
from .timeutils import local_day_bounds

DEVICE_KEYS = {
    "ecodan": ("ecodan_energy", "ecodan_power", "ecodan_mode"),
    "ac_1": ("ac_1_energy", "ac_1_power", "ac_1_climate"),
    "ac_2": ("ac_2_energy", "ac_2_power", "ac_2_climate"),
    "ac_3": ("ac_3_energy", "ac_3_power", "ac_3_climate"),
}
SITE_KEYS = {"pv": "pv_energy", "import": "grid_import", "export": "grid_export"}
TEMPERATURE_SOURCES = {
    "outdoor": ("outdoor_temperature", None),
    "flow": ("flow_temperature", None),
    "representative_indoor": ("representative_room_temperature", None),
    "ac_1_room": ("ac_1_temperature", None),
    "ac_2_room": ("ac_2_temperature", None),
    "ac_3_room": ("ac_3_climate", "current_temperature"),
    "dhw_tank": ("ecodan_dhw", "current_temperature"),
}


class HomeEnergyCoordinator:
    def __init__(self, hass: HomeAssistant, entry, database: EnergyDatabase) -> None:
        self.hass = hass
        self.entry = entry
        self.database = database
        self.config = {**entry.data, **entry.options}
        self.reader = RecorderReader(hass)
        self.timezone = hass.config.time_zone

    async def async_process_day(self, day: date, *, reason: str = "daily_close") -> bool:
        job_id = await self.hass.async_add_executor_job(
            self.database.start_job, "daily", {"date": day.isoformat(), "reason": reason}
        )
        try:
            source = await self._async_collect(day)
            freshness_minutes = int(self.config.get(CONF_TEMPERATURE_FRESHNESS_MINUTES, 0))
            freshness = timedelta(minutes=freshness_minutes) if freshness_minutes else None
            aggregator = DailyAggregator(
                self.timezone,
                cycle_hours=int(self.config.get(CONF_CYCLE_HOURS, 6)),
                temperature_freshness=freshness,
                seasonal_fallback=bool(self.config.get(CONF_SEASONAL_FALLBACK, False)),
            )
            record = await self.hass.async_add_executor_job(aggregator.aggregate, day, source)
            changed = await self.hass.async_add_executor_job(
                lambda: self.database.upsert_daily(record, reason=reason)
            )
        except Exception as error:
            await self.hass.async_add_executor_job(
                self.database.finish_job, job_id, "failed", {"error": type(error).__name__}
            )
            raise
        await self.hass.async_add_executor_job(
            self.database.finish_job, job_id, "success", {"changed": changed, "quality": record.quality}
        )
        if changed:
            async_dispatcher_send(self.hass, f"{self.entry.entry_id}_updated")
        return changed

    async def async_process_checkpoint(self, scheduled_at: datetime) -> int:
        """Persist the last completed wall-clock cycle using UTC instants."""
        zone = ZoneInfo(self.timezone)
        local_end = scheduled_at.astimezone(zone).replace(minute=0, second=0, microsecond=0)
        local_start = local_end - timedelta(hours=int(self.config.get(CONF_CYCLE_HOURS, 6)))
        start, end = local_start.astimezone(UTC), local_end.astimezone(UTC)
        job_id = await self.hass.async_add_executor_job(
            self.database.start_job, "checkpoint", {"start": start.isoformat(), "end": end.isoformat()}
        )
        try:
            source = await self._async_collect_range(start, end)
            changed = 0
            for device, history in source.devices.items():
                classifier = ecodan_category if device == "ecodan" else partial(
                    ac_category, seasonal_fallback=bool(self.config.get(CONF_SEASONAL_FALLBACK, False))
                )
                result = await self.hass.async_add_executor_job(partial(
                    allocate_interval, start=start, end=end, mode_samples=history.mode,
                    power_samples=history.power, counter_samples=history.counter,
                    classifier=classifier,
                ))
                payload = {**asdict(result), "coverage_percent": result.coverage_percent}
                changed += int(await self.hass.async_add_executor_job(
                    self.database.upsert_checkpoint, start, end, local_start.date().isoformat(),
                    device, payload,
                ))
        except Exception as error:
            await self.hass.async_add_executor_job(
                self.database.finish_job, job_id, "failed", {"error": type(error).__name__}
            )
            raise
        await self.hass.async_add_executor_job(
            self.database.finish_job, job_id, "success", {"changed_devices": changed}
        )
        return changed

    async def async_catch_up(self) -> None:
        local_today = datetime.now(UTC).astimezone(ZoneInfo(self.hass.config.time_zone)).date()
        yesterday = local_today - timedelta(days=1)
        latest = await self.hass.async_add_executor_job(self.database.latest)
        next_day = yesterday if latest is None else date.fromisoformat(latest["local_date"]) + timedelta(days=1)
        next_day = max(next_day, yesterday - timedelta(days=30))
        while next_day <= yesterday:
            await self.async_process_day(next_day, reason="startup_catchup")
            next_day += timedelta(days=1)

    async def _async_collect(self, day: date) -> DailyInput:
        start, end = local_day_bounds(day, self.timezone)
        return await self._async_collect_range(start, end)

    async def _async_collect_range(self, start: datetime, end: datetime) -> DailyInput:
        entity_ids = [self.config[key] for keys in DEVICE_KEYS.values() for key in keys if self.config.get(key)]
        entity_ids += [self.config[key] for key in SITE_KEYS.values() if self.config.get(key)]
        entity_ids += [self.config[key] for key, _attribute in TEMPERATURE_SOURCES.values() if self.config.get(key)]
        history = await self.reader.async_history(sorted(set(entity_ids)), start, end)

        statistic_entities = {
            self.config[key] for keys in DEVICE_KEYS.values() for key in keys[:1] if self.config.get(key)
        } | {self.config[key] for key in SITE_KEYS.values() if self.config.get(key)} | {
            self.config[key] for key, attribute in TEMPERATURE_SOURCES.values()
            if attribute is None and self.config.get(key)
        }
        statistics = await self.reader.async_statistics(statistic_entities, start, end)
        used_influx: list[str] = []
        try:
            influx_mapping = json.loads(self.config.get(CONF_INFLUX_MAPPING, "{}") or "{}")
        except (TypeError, json.JSONDecodeError):
            influx_mapping = {}
        influx = None
        if self.config.get(CONF_INFLUX_ENABLED) and self.config.get(CONF_INFLUX_HOST) and self.config.get(CONF_INFLUX_DATABASE):
            influx = InfluxBackfillClient(
                host=self.config[CONF_INFLUX_HOST], port=int(self.config.get(CONF_INFLUX_PORT, 8086)),
                database=self.config[CONF_INFLUX_DATABASE], username=self.config.get(CONF_INFLUX_USERNAME),
                password=self.config.get(CONF_INFLUX_PASSWORD),
            )

        async def numeric(key: str, *, statistics_type: str | None = None,
                          attribute: str | None = None) -> list[NumericSample]:
            entity_id = self.config.get(key)
            if not entity_id:
                return []
            raw = states_to_numeric(history.get(entity_id, []), attribute=attribute)
            if len(raw) >= 2:
                return raw
            if statistics_type:
                from_statistics = (
                    statistics_to_counter_samples(statistics.get(entity_id, []), start)
                    if statistics_type == "counter"
                    else statistics_to_numeric_samples(statistics.get(entity_id, []))
                )
                if len(from_statistics) >= 2:
                    return from_statistics
            mapping = influx_mapping.get(key) if isinstance(influx_mapping, dict) else None
            if influx is not None and isinstance(mapping, dict):
                rows = await self.hass.async_add_executor_job(partial(
                    influx.query, mapping["measurement"], mapping.get("field", "value"),
                    start, end, entity_tag=mapping.get("entity_tag"),
                ))
                if rows:
                    used_influx.append(key)
                    return [NumericSample(timestamp, value) for timestamp, value in rows]
            return raw

        numeric_values = {}
        for keys in DEVICE_KEYS.values():
            numeric_values[keys[0]] = await numeric(keys[0], statistics_type="counter")
            numeric_values[keys[1]] = await numeric(keys[1])
        for key in SITE_KEYS.values():
            numeric_values[key] = await numeric(key, statistics_type="counter")

        devices = {
            device: DeviceHistory(
                counter=numeric_values[energy_key],
                power=numeric_values[power_key],
                mode=states_to_modes(history.get(self.config[mode_key], [])),
            )
            for device, (energy_key, power_key, mode_key) in DEVICE_KEYS.items()
        }
        site_counters = {name: numeric_values[key] for name, key in SITE_KEYS.items()}
        temperatures = {}
        for name, (key, attribute) in TEMPERATURE_SOURCES.items():
            if self.config.get(key):
                temperatures[name] = await numeric(
                    key, statistics_type="mean" if attribute is None else None,
                    attribute=attribute,
                )
        return DailyInput(devices, site_counters, temperatures, provenance={
            "primary_source": "home_assistant_recorder",
            "raw_history_includes_preceding_state": True,
            "long_term_statistics_fallback": True,
            "influx_backfill_keys": used_influx,
            "collected_at": datetime.now(UTC).isoformat(),
        })

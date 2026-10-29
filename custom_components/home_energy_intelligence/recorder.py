"""Read-only Recorder collection through Home Assistant's supported helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from functools import partial
from typing import Any

from .models import NumericSample, StateSample
from .units import IncompatibleUnitError, normalize_value, normalized_unit


class RecorderReader:
    """Executor-backed Recorder reads; never opens the Recorder DB directly."""

    def __init__(self, hass) -> None:
        self.hass = hass

    async def async_history(self, entity_ids: list[str], start: datetime,
                            end: datetime) -> dict[str, list[Any]]:
        if not entity_ids:
            return {}
        from homeassistant.components.recorder import get_instance, history

        instance = get_instance(self.hass)
        return await instance.async_add_executor_job(
            partial(
                history.get_significant_states,
                self.hass, start, end, entity_ids=entity_ids,
                include_start_time_state=True,
                significant_changes_only=False,
                minimal_response=False,
                no_attributes=False,
            )
        )

    async def async_statistics(self, statistic_ids: set[str], start: datetime,
                               end: datetime) -> dict[str, list[dict[str, Any]]]:
        if not statistic_ids:
            return {}
        from homeassistant.components.recorder import get_instance, statistics

        return await get_instance(self.hass).async_add_executor_job(
            statistics.statistics_during_period, self.hass, start, end,
            statistic_ids, "hour", None, {"change", "state", "sum", "mean", "min", "max"},
        )


def states_to_numeric(
    rows: list[Any], *, attribute: str | None = None, dimension: str | None = None
) -> list[NumericSample]:
    result = []
    for row in rows:
        value = row.attributes.get(attribute) if attribute else row.state
        unit = row.attributes.get("unit_of_measurement")
        if unit is None and dimension == "temperature":
            unit = row.attributes.get("temperature_unit")
        if value in (None, "unknown", "unavailable", ""):
            result.append(NumericSample(
                row.last_updated, None, str(value or "unavailable"),
                source_unit=unit, normalized_unit=normalized_unit(dimension) if dimension else unit,
            ))
            continue
        try:
            if dimension:
                converted = normalize_value(float(value), unit, dimension)
                result.append(NumericSample(
                    row.last_updated, converted.value, source_unit=converted.source_unit,
                    normalized_unit=converted.normalized_unit,
                ))
            else:
                result.append(NumericSample(row.last_updated, float(value), source_unit=unit,
                                            normalized_unit=unit))
        except IncompatibleUnitError:
            result.append(NumericSample(
                row.last_updated, None, f"incompatible_{dimension}_unit:{unit or 'missing'}",
                source_unit=unit, normalized_unit=normalized_unit(dimension),
            ))
        except (TypeError, ValueError):
            result.append(NumericSample(row.last_updated, None, "non_numeric", source_unit=unit))
    return result


def states_to_modes(rows: list[Any]) -> list[StateSample]:
    return [StateSample(row.last_updated, row.state, dict(row.attributes)) for row in rows]


def statistics_to_counter_samples(rows: list[dict[str, Any]], start: datetime,
                                  initial_sum: float = 0.0,
                                  source_unit: str = "kWh") -> list[NumericSample]:
    """Convert LTS sum/change rows to a monotonic synthetic counter.

    This preserves authoritative changes for old history but is not used for
    sub-hour mode boundaries; the resulting provenance is therefore partial.
    """
    factor = normalize_value(1.0, source_unit, "energy").value
    total = initial_sum * factor
    result = [NumericSample(start, total, source_unit=source_unit, normalized_unit="kWh")]
    for row in rows:
        change = row.get("change")
        if change is None:
            continue
        total += max(0.0, float(change) * factor)
        stamp = row.get("end", row["start"])
        if isinstance(stamp, (int, float)):
            stamp = datetime.fromtimestamp(stamp, tz=start.tzinfo)
        result.append(NumericSample(stamp, total, source_unit=source_unit, normalized_unit="kWh"))
    return result


def statistics_to_numeric_samples(
    rows: list[dict[str, Any]], *, source_unit: str = "°C"
) -> list[NumericSample]:
    """Convert time-weighted Recorder statistics to numeric interval samples."""
    result = []
    for row in rows:
        value = row.get("mean")
        if value is None:
            continue
        start = row["start"]
        end = row.get("end")
        if isinstance(start, (int, float)):
            start = datetime.fromtimestamp(start, tz=UTC)
        if isinstance(end, (int, float)):
            end = datetime.fromtimestamp(end, tz=UTC)
        converted = normalize_value(float(value), source_unit, "temperature")
        minimum = normalize_value(float(row.get("min", value)), source_unit, "temperature").value
        maximum = normalize_value(float(row.get("max", value)), source_unit, "temperature").value
        bounds = f"statistics_bounds:{minimum}:{maximum}"
        result.append(NumericSample(start, converted.value, bounds, source_unit=source_unit,
                                    normalized_unit="°C"))
        if end is not None:
            result.append(NumericSample(end, None, "statistics_interval_end",
                                        source_unit=source_unit, normalized_unit="°C"))
    return result

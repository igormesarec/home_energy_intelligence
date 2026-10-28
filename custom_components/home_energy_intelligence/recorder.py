"""Read-only Recorder collection through Home Assistant's supported helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from functools import partial
from typing import Any

from .models import NumericSample, StateSample


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


def states_to_numeric(rows: list[Any], *, attribute: str | None = None) -> list[NumericSample]:
    result = []
    for row in rows:
        value = row.attributes.get(attribute) if attribute else row.state
        if value in (None, "unknown", "unavailable", ""):
            result.append(NumericSample(row.last_updated, None, str(value or "unavailable")))
            continue
        try:
            result.append(NumericSample(row.last_updated, float(value)))
        except (TypeError, ValueError):
            result.append(NumericSample(row.last_updated, None, "non_numeric"))
    return result


def states_to_modes(rows: list[Any]) -> list[StateSample]:
    return [StateSample(row.last_updated, row.state, dict(row.attributes)) for row in rows]


def statistics_to_counter_samples(rows: list[dict[str, Any]], start: datetime,
                                  initial_sum: float = 0.0) -> list[NumericSample]:
    """Convert LTS sum/change rows to a monotonic synthetic counter.

    This preserves authoritative changes for old history but is not used for
    sub-hour mode boundaries; the resulting provenance is therefore partial.
    """
    total = initial_sum
    result = [NumericSample(start, total)]
    for row in rows:
        change = row.get("change")
        if change is None:
            continue
        total += max(0.0, float(change))
        stamp = row.get("end", row["start"])
        if isinstance(stamp, (int, float)):
            stamp = datetime.fromtimestamp(stamp, tz=start.tzinfo)
        result.append(NumericSample(stamp, total))
    return result


def statistics_to_numeric_samples(rows: list[dict[str, Any]]) -> list[NumericSample]:
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
        bounds = f"statistics_bounds:{row.get('min', value)}:{row.get('max', value)}"
        result.append(NumericSample(start, float(value), bounds))
        if end is not None:
            result.append(NumericSample(end, None, "statistics_interval_end"))
    return result

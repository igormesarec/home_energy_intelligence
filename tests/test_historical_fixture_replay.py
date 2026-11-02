"""Replay exported JSON observations with HEI_REPLAY_FIXTURE, without HA writes.

JSON keys: start, end (ISO timestamps), counters and modes (arrays of
{timestamp, value}), and optional expected_kwh. Timestamps must include offsets.
"""
import json
import os
from datetime import datetime
from pathlib import Path

import pytest

from custom_components.home_energy_intelligence.allocation import allocate_historical_counter_increments
from custom_components.home_energy_intelligence.models import NumericSample, StateSample


def test_replay_exported_counter_fixture():
    path = os.environ.get("HEI_REPLAY_FIXTURE")
    fixture = json.loads(Path(path).read_text(encoding="utf-8")) if path else {
        "start": "2026-09-25T00:00:00+00:00", "end": "2026-09-25T01:00:00+00:00",
        "counters": [{"timestamp": "2026-09-25T00:05:00+00:00", "value": 100},
                     {"timestamp": "2026-09-25T00:35:00+00:00", "value": 101}],
        "modes": [], "expected_kwh": 1,
    }
    stamp = datetime.fromisoformat
    start, end = stamp(fixture["start"]), stamp(fixture["end"])
    assert start.tzinfo and end.tzinfo
    result = allocate_historical_counter_increments(
        start=start, end=end,
        counter_samples=[NumericSample(stamp(row["timestamp"]), row["value"]) for row in fixture["counters"]],
        mode_samples=[StateSample(stamp(row["timestamp"]), row["value"]) for row in fixture.get("modes", [])],
        classifier=lambda state: state.state)
    if "expected_kwh" in fixture:
        assert result.meter_kwh == pytest.approx(fixture["expected_kwh"])
    if result.meter_kwh is not None:
        assert sum(result.allocations_kwh.values()) + result.residual_kwh == pytest.approx(result.meter_kwh)

from datetime import date
import json
from pathlib import Path

import pytest

from custom_components.home_energy_intelligence.engine import DailyAggregator, DailyInput
from custom_components.home_energy_intelligence.models import NumericSample
from custom_components.home_energy_intelligence.timeutils import local_day_bounds


def test_october_25_whole_house_reconciliation_and_25_hour_day():
    fixture = json.loads((Path(__file__).parent / "fixtures" / "october_2026_stats.json").read_text())
    values = fixture["2026-10-25"]
    start, end = local_day_bounds(date(2026, 10, 25), fixture["timezone"])
    source = DailyInput(devices={}, site_counters={
        "pv": [NumericSample(start, 100), NumericSample(end, 100 + values["pv_kwh"])],
        "import": [NumericSample(start, 200), NumericSample(end, 200 + values["import_kwh"])],
        "export": [NumericSample(start, 50), NumericSample(end, 50 + values["export_kwh"])],
    }, temperatures={})
    result = DailyAggregator(fixture["timezone"]).aggregate(date(2026, 10, 25), source)
    assert result.household_kwh == pytest.approx(values["expected_household_kwh"])
    assert result.day_seconds == 25 * 3600


def test_required_recorder_fixture_values_are_preserved():
    fixture = json.loads((Path(__file__).parent / "fixtures" / "october_2026_stats.json").read_text())
    assert list(fixture["ecodan_daily_kwh"].values()) == [15.87707, 15.26269, 20.19328, 28.78444]

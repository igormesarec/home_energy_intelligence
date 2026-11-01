from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from custom_components.home_energy_intelligence.analytics import canonical_energy, combine_interval_records
from custom_components.home_energy_intelligence.timeutils import local_day_bounds
from datetime import date


WEBSOCKET = (
    Path(__file__).parents[1]
    / "custom_components/home_energy_intelligence/websocket.py"
)


def _load_response_functions():
    tree = ast.parse(WEBSOCKET.read_text(encoding="utf-8"))
    wanted = {"_v02_daily_curves", "_outdoor_interval_evidence"}
    functions = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in wanted
    ]
    namespace = {
        "UTC": UTC,
        "datetime": datetime,
        "canonical_energy": lambda _record, _equipment: {"equipment": []},
    }
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(WEBSOCKET), "exec"), namespace)
    return namespace


def test_v02_response_calculations_read_canonical_temperature_mean():
    functions = _load_response_functions()
    daily = functions["_v02_daily_curves"](
        [{
            "local_date": "2026-10-30",
            "temperatures": {"outdoor": {
                "mean": 7.25, "mean_c": 99.0, "coverage_percent": 100,
            }},
        }],
        [], "outdoor", 170,
    )
    assert daily[0]["outdoor_mean_c"] == 7.25

    start = datetime(2026, 10, 30, tzinfo=UTC)
    interval = {"start_utc": start.isoformat(),
                "end_utc": (start + timedelta(hours=1)).isoformat()}
    hourly = [{
        "period_start_utc": start.isoformat(),
        "period_end_utc": (start + timedelta(hours=1)).isoformat(),
        "temperatures": {"outdoor": {
            "mean": 5.5, "mean_c": 88.0, "coverage_seconds": 3600,
        }},
    }]
    evidence = functions["_outdoor_interval_evidence"](
        [interval], hourly, "outdoor"
    )
    assert evidence[(interval["start_utc"], interval["end_utc"])]["mean_c"] == 5.5


def test_cooling_completeness_is_independent_of_missing_heat_pump():
    functions = _load_response_functions()
    functions["canonical_energy"] = lambda *_: {"calendar_period_finalized": True, "equipment": [
        {"equipment_id": "hp", "kind": "heat_pump", "available": False},
        {"equipment_id": "ac", "kind": "air_conditioner", "available": True,
         "classification_complete": True, "known_confirmed_cooling_kwh": 3,
         "known_confirmed_heating_kwh": 1},
    ]}
    row = functions["_v02_daily_curves"]([{
        "local_date": "2026-09-01", "allocations": {
            "ac": {"categories": {"cooling": 3, "heating": 1}}},
    }], [], "outside", 170)[0]
    assert row["ac_cooling_kwh"] == 3
    assert row["whole_house_heating_kwh"] is None
    assert row["whole_house_heating_known_subtotal_kwh"] == 1
    assert row["heat_pumps"][0]["electricity_kwh"] is None


def test_missing_cooling_category_is_not_a_measured_zero():
    functions = _load_response_functions()
    functions["canonical_energy"] = lambda *_: {"equipment": [
        {"equipment_id": "ac", "kind": "air_conditioner", "available": True,
         "classification_complete": False, "known_confirmed_heating_kwh": 2},
    ]}
    row = functions["_v02_daily_curves"]([{
        "allocations": {"ac": {"categories": {"heating": 2}}},
    }], [], "outside", 170)[0]
    assert row["ac_cooling_kwh"] is None
    assert row["ac_cooling_known_subtotal_kwh"] is None


@pytest.mark.parametrize("day,hours", [(date(2026, 3, 29), 23), (date(2026, 10, 25), 25)])
@pytest.mark.parametrize("kind", ["heat_pump", "air_conditioner"])
def test_daily_heating_finalization_is_independent_from_classification(day, hours, kind):
    functions = _load_response_functions()
    functions["canonical_energy"] = canonical_energy
    start, end = local_day_bounds(day, "Europe/Ljubljana")
    assert (end-start).total_seconds() == hours * 3600
    equipment = [{"equipment_id": "device", "kind": kind}]
    allocation = {"meter_kwh": 2, "categories": {"heating": 2}, "quality": "good",
                  "coverage_percent": 100, "residual_kwh": 0}
    record = {"local_date": day.isoformat(), "period_start_utc": start.isoformat(),
              "period_end_utc": end.isoformat(), "allocations": {"device": allocation}}
    final = functions["_v02_daily_curves"]([record], equipment, "outdoor", 170)[0]
    device = final["heat_pumps" if kind == "heat_pump" else "air_conditioners"][0]
    assert final["calendar_period_finalized"] and final["complete_through_cutoff"]
    assert device["heating_evidence_complete"] and device["electricity_kwh"] == 2
    record.update(partial=True, finalized=False, interval_coverage={"complete": True})
    partial = functions["_v02_daily_curves"]([record], equipment, "outdoor", 170)[0]
    assert not partial["calendar_period_finalized"] and partial["complete_through_cutoff"]
    assert partial["whole_house_heating_kwh"] is None
    record["interval_coverage"]["complete"] = False
    assert not functions["_v02_daily_curves"]([record], equipment, "outdoor", 170)[0]["complete_through_cutoff"]


def test_zero_heating_requires_known_operating_evidence():
    functions = _load_response_functions()
    functions["canonical_energy"] = canonical_energy
    equipment = [{"equipment_id": "hp", "kind": "heat_pump"}]
    allocation = {"meter_kwh": 2, "categories": {"dhw": 2}, "quality": "good",
                  "coverage_percent": 100, "residual_kwh": 0}
    record = {"allocations": {"hp": allocation}}
    assert functions["_v02_daily_curves"]([record], equipment, "outdoor", 170)[0]["heat_pumps"][0]["electricity_kwh"] == 0
    allocation.update(categories={"unknown": 2}, quality="partial", coverage_percent=0)
    assert functions["_v02_daily_curves"]([record], equipment, "outdoor", 170)[0]["heat_pumps"][0]["electricity_kwh"] is None


def test_partial_hourly_meter_evidence_survives_missing_interval():
    functions = _load_response_functions()
    functions["canonical_energy"] = canonical_energy
    start = datetime(2026, 10, 25, tzinfo=UTC)
    rows = []
    for hour in range(2):
        left = start + timedelta(hours=hour)
        allocation = {"meter_kwh": 2 if hour == 0 else None,
                      "categories": {"heating": 2} if hour == 0 else {},
                      "quality": "good" if hour == 0 else "missing_counter", "coverage_percent": 100}
        rows.append({"local_date": "2026-10-25", "timezone": "UTC", "day_seconds": 3600,
                     "period_start_utc": left.isoformat(), "period_end_utc": (left + timedelta(hours=1)).isoformat(),
                     "allocations": {"hp": allocation}})
    combined = combine_interval_records(rows, expected_start=start, expected_end=start + timedelta(hours=3))
    combined.update(partial=True, finalized=False)
    point = functions["_v02_daily_curves"]([combined], [{"equipment_id": "hp", "kind": "heat_pump"}], "outdoor", 170)[0]
    assert point["heat_pumps"][0]["electricity_kwh"] == 2
    assert not point["heat_pumps"][0]["heating_evidence_complete"]
    assert not point["complete_through_cutoff"]

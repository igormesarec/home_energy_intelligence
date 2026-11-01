from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path


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

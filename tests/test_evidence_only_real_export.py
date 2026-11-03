"""Optional real-export acceptance; set HEI_ECODAN_EXPORT_DIR to the CSV folder."""
import csv
import json
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.home_energy_intelligence.coordinator import HomeEnergyCoordinator
from custom_components.home_energy_intelligence.database import EnergyDatabase
from custom_components.home_energy_intelligence.engine import DailyInput, DeviceHistory
from custom_components.home_energy_intelligence.electricity_recovery import prepare_recovery_day
from custom_components.home_energy_intelligence.models import NumericSample
from custom_components.home_energy_intelligence.timeutils import local_day_bounds


@pytest.mark.asyncio
@pytest.mark.parametrize("day,meter,assumed,unknown", [
    (18, 5.04134, 3.20023, 1.84111), (25, 12.04398, 0.0, 12.04398),
    (28, 6.78593, 3.72348, 3.06245), (30, 8.51290, 2.69775, 5.81515),
])
async def test_local_september_export(day, meter, assumed, unknown, tmp_path):
    folder = os.environ.get("HEI_ECODAN_EXPORT_DIR")
    if not folder:
        pytest.skip("Set HEI_ECODAN_EXPORT_DIR to the independently supplied Chronograf exports")
    def read(name, column):
        with (Path(folder) / name).open(encoding="utf-8-sig", newline="") as f:
            return [(datetime.fromisoformat(row["time"]).astimezone(UTC), row[column]) for row in csv.DictReader(f) if row[column]]
    start, end = local_day_bounds(date(2026, 9, day), "Europe/Ljubljana")
    meters = [NumericSample(t, float(v)) for t, v in read("2026-11-02-16-23 Chronograf Data.csv", "kWh.value") if start <= t <= end]
    modes = [SimpleNamespace(last_updated=t, state=v, attributes={}) for t, v in read("2026-11-02-16-24 Chronograf Data.csv", "state.state") if start-timedelta(hours=1) <= t < end]
    source = DailyInput(devices={"hp": DeviceHistory(kind="heat_pump", counter=meters)}, site_counters={}, temperatures={}, provenance={"counter_collection": {"equipment.hp.energy": "influx"}})
    collector = AsyncMock(return_value=source)
    coordinator = SimpleNamespace(timezone="Europe/Ljubljana", mapping_revision={"effective_at": "2026-10-01T00:00:00+00:00"},
        config={"equipment": [{"equipment_id": "hp", "display_name": "Ecodan", "kind": "heat_pump", "energy_entity": "sensor.toplotna_pro_3em_energy"}],
                "influx_mapping_json": json.dumps({"equipment.hp.fallback_state": {"entity_id": "climate.toplotna_zone_1"}})},
        hass=SimpleNamespace(states=SimpleNamespace(get=lambda _: None)),
        reader=SimpleNamespace(async_history=AsyncMock(return_value={"climate.toplotna_zone_1": modes})), _async_collect_range=collector)
    result = await HomeEnergyCoordinator.async_preview_historical_energy(coordinator, date(2026, 9, day), evidence_only=True)
    hp = result["allocations"]["hp"]
    assert hp["meter_kwh"] == pytest.approx(meter)
    assert hp["categories"].get("heating_assumed", 0) == pytest.approx(assumed)
    assert hp["categories"].get("unknown", 0) == pytest.approx(unknown)
    assert result["historical_mapping_quality"] == "unverified historical mapping"
    assert collector.await_args.kwargs["store_mapping_revision"] is False
    plan = prepare_recovery_day(result, "Europe/Ljubljana")
    assert plan["records"][0]["meter_kwh"] == pytest.approx(meter)
    assert plan["records"][0]["verification_status"] == "provisional"
    database = EnergyDatabase(tmp_path / "real-export-test.sqlite3")
    database.initialize()
    day_string = date(2026, 9, day).isoformat()
    job = database.create_electricity_recovery_job(day_string, day_string, "Europe/Ljubljana", "evidence_only")
    assert database.commit_electricity_recovery_day(job, day_string, plan["records"])["inserted"] == 1
    stored = database.electricity_recovery_evidence_range(day_string, day_string)["records"][0]
    assert stored["meter_kwh"] == pytest.approx(meter)
    assert stored["categories"].get("unknown", 0) == pytest.approx(unknown)
    retry = database.create_electricity_recovery_job(day_string, day_string, "Europe/Ljubljana", "evidence_only")
    assert database.commit_electricity_recovery_day(retry, day_string, plan["records"])["inserted"] == 0

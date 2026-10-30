"""V0.3 forecast input contract only; V0.2 does not produce forecasts."""
from __future__ import annotations

from typing import Any


def forecast_input_contract() -> dict[str, Any]:
    """Describe evidence a future model must consume without emitting values."""
    return {
        "status": "not_implemented",
        "required": [
            "representative_outdoor_temperature",
            "validated_temperature_coverage",
            "classified_equipment_electricity",
            "mapping_revision",
        ],
        "optional": [
            "measured_ecodan_heating_thermal_energy",
            "measured_ecodan_dhw_thermal_energy",
            "weather_forecast_entity",
        ],
        "prohibited_inferences": [
            "ac_cop", "ac_thermal_output", "sparse_cumulative_daily_thermal_energy",
        ],
        "forecast": None,
    }

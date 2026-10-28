"""Canonical unit validation and normalization for source measurements."""

from __future__ import annotations

from dataclasses import dataclass


class IncompatibleUnitError(ValueError):
    """Raised when a source unit cannot represent the requested dimension."""


@dataclass(frozen=True, slots=True)
class NormalizedValue:
    value: float
    source_unit: str
    normalized_unit: str


_ENERGY_FACTORS = {"Wh": 0.001, "kWh": 1.0, "MWh": 1000.0}
_POWER_FACTORS = {"W": 1.0, "kW": 1000.0}


def normalize_value(value: float, source_unit: str | None, dimension: str) -> NormalizedValue:
    """Normalize supported source units while retaining the original unit."""
    unit = str(source_unit or "").strip()
    number = float(value)
    if dimension == "energy" and unit in _ENERGY_FACTORS:
        return NormalizedValue(number * _ENERGY_FACTORS[unit], unit, "kWh")
    if dimension == "power" and unit in _POWER_FACTORS:
        return NormalizedValue(number * _POWER_FACTORS[unit], unit, "W")
    if dimension == "temperature" and unit in {"°C", "°F"}:
        converted = number if unit == "°C" else (number - 32.0) * 5.0 / 9.0
        return NormalizedValue(converted, unit, "°C")
    raise IncompatibleUnitError(f"{unit or 'missing'} is not a supported {dimension} unit")


def normalized_unit(dimension: str) -> str:
    return {"energy": "kWh", "power": "W", "temperature": "°C"}[dimension]

"""Pure data models used by collection, calculation, and storage."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class NumericSample:
    """A timestamped numeric reading. None marks an unavailable observation."""

    timestamp: datetime
    value: float | None
    reason: str | None = None
    source: str | None = None
    source_unit: str | None = None
    normalized_unit: str | None = None


@dataclass(frozen=True, slots=True)
class RejectedReading:
    """Auditable rejected source interval."""

    timestamp: datetime
    value: float | None
    duration_seconds: float
    reason: str
    source: str


@dataclass(frozen=True, slots=True)
class StateSample:
    """A timestamped state with the attributes recorded at that instant."""

    timestamp: datetime
    state: str
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TemperatureResult:
    mean: float | None
    minimum: float | None
    maximum: float | None
    coverage_seconds: float
    period_seconds: float
    rejected_count: int
    rejected_reasons: tuple[str, ...]
    rejections: tuple[RejectedReading, ...] = ()
    latest_value: float | None = None
    latest_timestamp: str | None = None

    @property
    def coverage_percent(self) -> float:
        if not self.period_seconds:
            return 0.0
        if abs(self.coverage_seconds - self.period_seconds) <= 1e-6:
            return 100.0
        return max(0.0, min(
            100.0, 100.0 * self.coverage_seconds / self.period_seconds
        ))

    @property
    def quality(self) -> str:
        if self.coverage_seconds == 0:
            return "missing"
        if self.coverage_seconds + 1e-6 < self.period_seconds:
            return "partial"
        return "good" if self.rejected_count == 0 else "filtered"


@dataclass(frozen=True, slots=True)
class AllocationResult:
    meter_kwh: float | None
    allocations_kwh: dict[str, float]
    residual_kwh: float | None
    covered_seconds: float
    period_seconds: float
    reset_count: int = 0
    rejected_count: int = 0
    quality: str = "missing"

    @property
    def coverage_percent(self) -> float:
        return 100.0 * self.covered_seconds / self.period_seconds if self.period_seconds else 0.0


@dataclass(slots=True)
class DailyRecord:
    local_date: str
    timezone: str
    period_start_utc: str
    period_end_utc: str
    day_seconds: int
    household_kwh: float | None
    pv_kwh: float | None
    import_kwh: float | None
    export_kwh: float | None
    non_hvac_kwh: float | None
    quality: str
    temperatures: dict[str, dict[str, Any]] = field(default_factory=dict)
    allocations: dict[str, dict[str, Any]] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

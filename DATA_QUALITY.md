# Data quality and provenance

Representative indoor and outdoor cards both use `time_weighted_temperature()`. Each accepted state is weighted by actual elapsed seconds until the next state transition, an explicit unavailable state, or an allowed point-sample freshness boundary. Longer-period summaries sum `temperature × valid seconds` and divide by total valid seconds; they never average daily means. Sentinel/spike intervals are excluded from mean/min/max and reduce coverage. An unchanged Recorder state remains valid until explicit contrary evidence because Recorder stores state changes rather than polling samples.

## Energy

- Power uses a previous-value hold only until the configured maximum gap.
- Negative, non-finite, or implausibly high power is rejected.
- Counter replacement/reset is recorded and processing resumes from the new non-negative value.
- Implausible counter jumps are rejected.
- Missing power coverage becomes `residual_kwh`; it is not allocated by time.
- Unknown heat-pump operation becomes `heating_assumed`, never DHW. Vendor strings are mapped in the UI rather than assumed by the engine.
- AC `hvac_action` is preferred. Selected mode without reliable action remains explicitly selected/estimated.

## Temperature

- Mean is `sum(value × covered seconds) / covered seconds`.
- The last recorded state at or before the boundary supplies the starting value.
- `unknown` and `unavailable` end coverage until a valid observation appears.
- Every source has configurable minimum/maximum bounds, invalid sentinels (defaults include −55 °C and 155 °C), maximum change per minute, freshness and missing thresholds.
- Legitimate sub-zero outdoor values remain valid within the configured physical range. Unchanged readings remain valid until their configured freshness limit.
- Explicit unavailable states end coverage immediately. Invalid values never contribute measured coverage.
- Each rejection stores timestamp, value, affected duration, reason and source entity.

## Status vocabulary

- `good`: complete and reconciled.
- `filtered`: complete after explicit invalid/reset filtering.
- `uncertain`: complete coverage includes assumed/selected/estimated operation.
- `partial`: incomplete coverage or material residual.
- `poor`: less than half of the period classified.
- `missing`: no defensible value.
- `unreconciled`: derived totals contradict source totals.

## Coverage layers

- **Date availability** is elapsed local-date duration present in the compact archive; DST days contribute their actual 23-, 24- or 25-hour duration.
- **Meter coverage** is elapsed duration with each required authoritative counter available. It does not depend on a temperature sensor.
- **Allocation coverage** is classified elapsed duration across every enabled equipment ID. A device with 18 classified hours in a 24-hour day contributes 75%, not 0% or 100%; no configured equipment is reported as unavailable rather than full coverage.
- Missing allocation-coverage metadata is **unknown**, never a compatibility alias for 100%. Legacy meter totals and known subtotals remain visible, while complete category values remain unavailable until explicit evidence exists.
- **Temperature coverage** is valid elapsed duration for each source independently. A representative card with no explicit selection says **Not configured** and never reports 100%.

These layers are intentionally independent. A complete household meter can remain complete when an indoor sensor is missing, while a partial HVAC classifier keeps its meter total and raw residual evidence without promoting missing categories to confirmed zero.

Canonical electricity quality uses meter, classification and reconciliation
evidence only. Overall record quality may still be partial because a temperature
source is missing; the Energy Split displays that as separate temperature
evidence rather than calling the electricity missing.

## Temperature pipeline recovery

The administrator diagnostic reads at most 31 local days and reports the first
observable missing stage: disabled/unavailable live entity, Recorder history
unavailable, incompatible/invalid samples, missing hourly processing, absent
temperature payload, or rejected/uncovered persisted payload. It includes raw
availability bounds, units, rejection reasons and caused missing intervals.

Repair re-runs the canonical time-weighted calculation from bounded Recorder (or
configured read-only InfluxDB fallback) evidence. It adds a source only when no
summary exists or replaces it only when the recovered valid duration is greater.
It never fills a gap, overwrites a stronger valid measurement, creates a finalized
day, or changes electricity/allocation fields.

The repair collector requests only enabled temperature entities. Before querying
Recorder it skips source/day combinations that already have at least 95% valid
coverage and non-partial quality. Every candidate must identify the exact UTC
period and mapped entity. A dedicated `BEGIN IMMEDIATE` transaction re-reads the
latest payload under the write lock, compares quality/coverage/provenance, and
updates only the JSON temperature map, checksum/revision metadata and rejection
rows belonging to changed temperature sources. A retry with identical evidence
is a no-op. Overlapping repairs are rejected and a failed startup repair is logged
without blocking hourly, checkpoint or daily recovery.

For an in-progress local date, date availability uses completed elapsed seconds
against that date's full 23/24/25-hour duration. Meter and allocation coverage use
the completed-hour processing scope, so a fully processed six-hour cutoff can have
100% meter coverage while the date is correctly only 25% available.

## Electrical heating-response qualification

Every candidate date is retained as validated or excluded. The default admission
policy requires 95% representative-outdoor valid duration, complete classification
for each enabled heat pump and heating-capable AC, known reconciliation within the
configured tolerance, a finalized valid date and configured floor area. The
frontend requires at least 10 admitted dates, an observed outdoor span of 8 °C and
R² ≥ 0.5 before showing a conservative electrical fit. These are evidence-quality
gates, not a claim of thermal output, COP, physical balance point or design-day
capacity.

Overview and House Thermal Profile consume this same backend candidate list and
the same `validated` status. The panel does not independently admit dates.

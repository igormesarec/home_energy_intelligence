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

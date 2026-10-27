# Data quality and provenance

## Energy

- Power uses a previous-value hold only until the configured maximum gap.
- Negative, non-finite, or implausibly high power is rejected.
- Counter replacement/reset is recorded and processing resumes from the new non-negative value.
- Implausible counter jumps are rejected.
- Missing power coverage becomes `residual_kwh`; it is not allocated by time.
- Unknown heat-pump operation becomes `heating_assumed`, never DHW.
- AC `hvac_action` is preferred. Selected mode without reliable action remains explicitly selected/estimated.

## Temperature

- Mean is `sum(value × covered seconds) / covered seconds`.
- The last recorded state at or before the boundary supplies the starting value.
- `unknown` and `unavailable` end coverage until a valid observation appears.
- Default range checks are outdoor −60…70 °C, indoor −10…60 °C, flow −10…110 °C.
- A time-only freshness cutoff is disabled by default because an unchanged value does not prove disconnection. It may be enabled when the source's update behavior provides reliable freshness evidence.
- Rejected readings remain visible through counts and reasons.

## Status vocabulary

- `good`: complete and reconciled.
- `filtered`: complete after explicit invalid/reset filtering.
- `uncertain`: complete coverage includes assumed/selected/estimated operation.
- `partial`: incomplete coverage or material residual.
- `poor`: less than half of the period classified.
- `missing`: no defensible value.
- `unreconciled`: derived totals contradict source totals.

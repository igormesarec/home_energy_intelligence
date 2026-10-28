# V0.1 implementation specification

## Objective

Create a standalone Home Assistant custom integration with domain `home_energy_intelligence`. Its authoritative output is one validated record per local calendar date. Raw source history remains owned by Home Assistant Recorder or an optional read-only InfluxDB 1.x source.

## Measurement rules

1. Cumulative counters are authoritative for electricity totals.
2. Recorded power is integrated with a previous-value hold capped by a quality gap and classified using every recorded mode transition.
3. The sum of classifications plus a visible signed residual reconciles to each counter. Small discrepancies inside the documented tolerance may scale measured classifications to the exact counter total.
4. Unknown heat-pump operation is `heating_assumed`; confirmed idle remains `idle`; neither is silently rewritten.
5. Temperature means use elapsed seconds, the last valid reading at/before the period start, explicit unavailable markers, range checks, and source coverage.
6. Local day boundaries come from the Home Assistant time zone and are stored as UTC instants. A local day may contain 23, 24, or 25 elapsed hours.
7. Whole-house electricity is `PV + grid import - grid export`. Negative non-HVAC residuals are flagged, never clamped.
8. Persisted six-hour checkpoints are the primary daily classification evidence. Daily Recorder counter changes reconcile totals but do not overwrite valid checkpoint categories.
9. Every source is a UI mapping. Equipment IDs and mapping revisions preserve provenance across entity replacement.
10. Temperature rejection is sensor-specific and auditable; −55 °C and 155 °C are default invalid sentinels, while legitimate sub-zero outdoor readings remain valid.

## Runtime schedule

- 00:00, 06:00, 12:00, 18:00 local: persist the completed wall-clock cycle with UTC boundaries.
- 05:00 local: rebuild and finalize the previous completed local day.
- Startup: catch up missing daily records, capped at 31 days per startup.
- Manual correction: idempotent single-day or 31-day-range services.

## Explicit exclusions

- No inferred thermal production from electrical input.
- No inferred return-water temperature.
- No COP reporting in V0.1.
- No weather-dependent forecast in V0.1.
- No direct SQL access to Home Assistant Recorder.
- No Recorder/InfluxDB writes or retention changes.
- No permanent web-accessible database exports or credentials in query URLs.

## Configurable equipment model

- One or more heat pumps and zero or more air conditioners, each named, enabled/disabled and independently mapped.
- Optional PV, import/export, indoor/outdoor, supply/return, DHW and weather sources.
- Installation-specific operating-state maps; Mitsubishi/MELCloud strings are suggestions, not engine assumptions.
- Options Flow can rebuild mappings without deleting daily records. Mapping revisions store effective dates.
- Circuit 2K is not accepted for supply/return; no return sensor is inferred.

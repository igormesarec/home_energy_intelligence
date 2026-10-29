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
11. Source units are validated and normalized before aggregation: energy to kWh, power to W and temperature to °C. The original unit is retained in provenance; incompatible sources produce unavailable data, not zero.
12. An unchanged Recorder temperature remains the active measured state until another state or an explicit unavailable marker. A value is not stale merely because its value did not change.
13. Checkpoint completeness is evaluated for every enabled equipment ID. Recovery writes only missing equipment/interval pairs and preserves valid prior checkpoints.
14. Positive unclassified heat-pump residual may be included in the configured assumed-heating reporting view, but remains stored as residual uncertainty and is never promoted to confirmed heating or DHW.

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
- Options Flow edits, adds, disables and removes individual entries without changing stable IDs or clearing unrelated mappings. Mapping revisions store effective dates.
- Circuit 2K is not accepted for supply/return; no return sensor is inferred.

## Dashboard and authorization

- Equipment IDs, types and display names are loaded dynamically through the WebSocket API; the frontend contains no installation-specific allocation keys.
- Overview and validated History are available to authenticated Home Assistant users.
- Settings mappings, database metadata and database export remain administrator-only.
- Missing optional equipment or measurements stay unavailable. Structural absence of PV may be used for a grid-only household balance without creating a fictitious PV measurement.

## V0.1.1 dashboard integrity requirements

- Today and same-cutoff comparisons enumerate every expected persisted hour and expose missing internal intervals, coverage percentage and partial quality. Delayed poor/partial hours may be repaired idempotently.
- Complete measured zero, complete total, known partial subtotal and unavailable total are distinct states. Missing allocations never become complete zero heating, DHW or cooling.
- History ranges are resolved in the Home Assistant timezone and include all requested day, Monday–Sunday week, calendar-month, calendar-year, configurable energy-season and custom presets.
- Period summaries cover the complete selected range independently of paginated detail records. CSV exports that same complete selected range without a 366-day truncation.
- Dynamic per-device heating and cooling preserve confirmed, selected and estimated evidence. Device subtotals reconcile with canonical category totals when complete.
- Energy Split keeps heating, DHW, cooling, idle, other household, unknown and signed discrepancy separate. Negative discrepancies are warnings, never clipped to zero.
- Indoor/outdoor timelines and summaries retain source IDs, mapping history, validated gaps, daily min/max/coverage and duration-weighted means across all valid elapsed seconds.
- History Today is an in-memory view of validated completed-hour rows at the same cutoff as Overview. It is explicitly partial and never inserted into finalized daily history.
- Period quality is layered: date availability, meter coverage, allocation coverage and temperature coverage are reported independently. An unrelated temperature gap cannot invalidate a valid meter total.
- Missing local dates contribute their full DST-aware duration to temperature coverage denominators; valid observations alone contribute to duration-weighted means and validated min/max.
- Energy Split includes Other HVAC (including fan/dry), other household, unallocated energy and signed reconciliation discrepancy as distinct evidence.
- Overview and History share one Home Assistant-timezone period selection. Heating-versus-outdoor analytics use only the explicitly selected representative outdoor source.
- PV, grid and household analytics use measured counters and physically valid balances; unavailable sources never generate a zero or invented rate.

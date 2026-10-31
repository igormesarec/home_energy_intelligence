# V0.1 implementation specification

## V0.2.1 monthly report and midnight contract

- Before the 05:00 authoritative close, Yesterday is reconstructed in memory from
  persisted hourly rows. Missing hours remain gaps; the provisional row is never written
  as finalized history and is replaced by the authoritative daily row without addition.
- Monthly/MtD Ecodan report snapshots are independent observations from calendar-month
  start through their stated cutoff. They are never differenced across months or inserted
  into the lifetime cumulative thermal ledger.
- Primary monthly COP uses measured Shelly electricity over the exact same interval.
  Assigned positive residual remains assumed heating and makes heating/combined COP
  provisional. Vendor electricity and vendor COP remain separate informational evidence.
- Date-only or unknown report cutoffs cannot create an exact primary COP. A final month
  requires an exact cutoff at the following month’s local midnight.
- The rolling report contains exactly 13 calendar positions, dynamic equipment metrics,
  nulls for unavailable evidence, synchronized chart/table/CSV values and separate
  energy, COP and percentage scales.

## V0.2 thermal and response contract

- Thermal inputs are cumulative measured counters, not inferred heat. Supported units
  are Wh, kWh and MWh and storage is normalized to kWh with source-unit provenance.
- Each observation retains UTC and local timestamp, timezone, DST fold, Ecodan stable
  equipment ID, meter identity, source entity/label, mapping-effective timestamp and note.
- The first observation is a baseline. Consecutive differences form intervals using
  actual elapsed seconds. Decreases are resets/corrections and never clamp to zero.
- Heating and DHW COP use category-matched Ecodan electricity over the exact same
  interval. Unknown/assumed heating, DHW cross-allocation and every AC term are excluded.
- Heating, DHW and combined COP reports use ratio of thermal sums to electricity sums.
- Daily charts are allowed only when evidence truly matches daily boundaries. Otherwise
  the UI exposes the actual interval; no interpolation or daily redistribution occurs.
- Whole-house electrical heating and AC cooling are equipment-neutral and dynamically
  include all enabled devices, with confirmed/selected/estimated quality retained.
- The four V0.2 charts are distinct. No AC COP or AC thermal output is displayed.
- V0.3 forecasting has only an input/provenance contract in V0.2 and returns no forecast.

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
- Representative indoor and measured outdoor sources require explicit stable-source selections. The flow provides a current-value/unit/quality/timestamp preview; it never substitutes weather data or a water-circuit sensor, and an unconfigured source has unavailable rather than 100% coverage.
- Allocation coverage is elapsed classified seconds across each enabled equipment ID. Meter availability, category-classification coverage and temperature coverage remain separate evidence layers.
- Whole-house electrical heating response may combine complete heat-pump and AC space-heating allocations and normalize them by configured floor area. Thermal output, COP and design-day output require separately measured/configured thermal evidence and cannot be inferred from electricity.
- Electrical-response dates require known representative-outdoor coverage at or above the configurable threshold (95% default), complete classification for every enabled heating device and known reconciliation. Excluded candidates remain visible with reasons. A displayed fit requires at least 10 validated dates spanning at least 8 °C and R² ≥ 0.5; it is never labelled a physical balance point.
- Energy Split includes Other HVAC (including fan/dry), other household, unallocated energy and signed reconciliation discrepancy as distinct evidence.
- Overview and History share one Home Assistant-timezone period selection. Heating-versus-outdoor analytics use only the explicitly selected representative outdoor source.
- PV, grid and household analytics use measured counters and physically valid balances; unavailable sources never generate a zero or invented rate.
- Options Flow operates on a draft and commits only at successful finish. Skipping preserves pending mappings, disabling retains the full stable-ID record, validation failure restores the draft, and only explicit remove deletes a mapping. Semantically unchanged mappings retain their effective date.
- Legacy allocation rows without explicit classified duration or percentage have unknown—not 100%—classification coverage. Their meter total and known category subtotals remain evidence, but complete category totals stay unavailable.
- Date availability is elapsed UTC duration between Home Assistant local-date boundaries. A 23/25-hour day contributes 23/25 hours, and an in-progress date contributes its processed duration against the full local-day denominator.
- Temperature diagnostics expose selected stable source ID, HA entity/attribute, enabled state, live value/update/unit, bounded Recorder state/statistics availability and range, normalization/rejection reasons, persisted hourly/daily summaries and caused missing intervals. The diagnostic operation is administrator-only and read-only.
- Temperature repair is bounded to 31 inclusive local days, restart-safe and idempotent. It merges only missing or higher-coverage temperature summaries into existing hourly/daily records, never creates a fictitious finalized day, never bridges unknown intervals and never changes preserved energy/allocation values.
- The latest two local days receive the same temperature-only recovery during startup. Older ranges require an explicit administrator action.
- Energy Split quality is layered: incomplete current period, missing meter, incomplete classification, uncertain/estimated allocation and missing temperature evidence are distinct. Temperature gaps do not change canonical electricity quality.
- One backend function qualifies heating-versus-outdoor candidates for every dashboard view. Frontend views may format the same validated points differently but must not re-derive admission from History rows.
- Historical temperature repair uses a dedicated temperature-only collector and an atomic interval/entity-provenance-aware SQLite merge. It skips only 100%-complete sources before Recorder access, serializes overlapping repairs, preserves unrelated payload and audit fields, and cannot block ordinary startup recovery. Fallback is based on validated elapsed coverage rather than sample count. Valid partial raw Recorder evidence is never replaced by lower-fidelity aggregate evidence when safe segment merging cannot be proven.
- Full temperature completeness requires 100% validated elapsed seconds; the configurable 95% default heating-response threshold is a separate eligibility field and never promotes partial daily or period quality.
- Historical repair requires an effective-dated mapping proof covering the complete target interval and matching source ID, entity ID, attribute, role and enabled state. Unverified current remaps cannot fill older summaries, mapping transitions are skipped with a reason, and legacy summaries with incomplete identity remain preserved.
- Read-only InfluxDB fallback is isolated per source. Timeout, connection and malformed-result evidence is reported without credentials; Recorder-valid sources and partial coverage remain unchanged and missing time is never replaced with zero.
- Daily and period quality is derived at read time from independent date, household-meter, enabled-device allocation and explicitly selected representative-temperature layers. Stored legacy labels are provenance only; unknown legacy allocation coverage cannot be promoted to good, while a successful complete repair can clear an obsolete partial label.
- Schema V4 transactionally normalizes temperature summaries/audit evidence in bounded keyset batches, preserves the hydrated logical record contract and all auxiliary tables, and never runs automatic startup compaction. Failed conversion or version bump leaves the prior schema version and inline payloads readable and retryable. V1, V2, V3 and already-V4 paths are supported; locked/busy databases are not renamed as corrupt. Protected backup is recommended before migration and manual offline compaction is a separate maintenance action.

## V0.1.2 correction requirements

- Single-local-day representative temperature charts use persisted hourly summaries through an authenticated bounded timeline API. Today ends at the last completed elapsed hour; historical dates span the complete 23-, 24- or 25-hour HA-local day using UTC interval identity. Missing hours are null gaps, and daily aggregates are never expanded into fabricated hourly points. Longer periods use validated daily time-weighted summaries by default.
- Schema V5 stores temperature-only hourly/completed-day summaries independently of energy parents and overlays them at read time. Temperature-only dates keep every energy/PV/grid/HVAC value unavailable and cannot qualify a heating-response point without separate completed daily electricity evidence.
- Administrator backfill is limited to 1–31 inclusive Home Assistant-local dates and supports dry-run, progress, cancellation, persisted next-date cursors, restart continuation and idempotent source/UTC-interval upserts. Future current-day hours are excluded.
- Recorder state transitions, including carry-in state, remain the canonical temperature evidence. Statistics and optional read-only InfluxDB are fallbacks only where raw states have no validated coverage; missing intervals remain gaps.
- Historical entity, attribute and role come from chronological mapping events covering the exact interval. Re-selecting A after B creates a new A event. Unrecorded history requires an explicit bounded administrator attestation and is never inferred from the current mapping.
- Diagnostics separate energy and independent-temperature history, raw/validated duration, invalid/stale/unavailable evidence, mapping blocks, recoverable/blocked intervals, completed-cutoff/full-calendar coverage and inserted/updated/unchanged/skipped/failed counts. Zero recovery with outstanding gaps is action-required, not success.
- Canonical current-period energy separates complete-through-cutoff from calendar finalization. Valid confirmed plus assumed/selected/estimated electricity is visible as a known/provisional main value while its evidence classes remain distinct. Missing evidence never becomes a display zero.
- A true 100% temperature interval remains complete even when filtered invalid samples are audited. The configurable response threshold (95% default) is independent and never claims full completeness.
- General configuration uses localized English/Slovenian month choices, a visible 1–28 start-day field and a season preview. Editing season fields preserves floor area, equipment, sources, representative selections and mapping-effective dates.
- The V0.1.2 baseline introduced deterministic content-hash cache invalidation. V0.2.0 retains the unchanged sidebar path and the same cache-safety contract.

## V0.2.1 monthly analytics and completed-day availability

- After local midnight, a missing Yesterday daily row is represented from persisted
  completed hourly records. This view is always labelled provisional/partial, is never
  inserted into finalized history and is superseded by the authoritative daily row.
- Hourly electrical coverage ignores temperature-only archive rows. Missing hours keep
  household, PV, grid and HVAC complete totals unavailable while retaining known subtotals,
  missing-interval identity and independent temperature evidence.
- Schema V7 adds a monthly/MtD Ecodan snapshot ledger distinct from the lifetime cumulative
  thermal ledger. It stores report month, local/UTC cutoff, timezone/fold, cutoff certainty,
  partial/final status, nullable thermal/vendor fields, source identity, notes and audit.
- The rolling report contains exactly 13 calendar-month positions. Selected chart series,
  the synchronized table and authenticated CSV share one backend result and never replace
  a missing value with zero.
- Shelly meter electricity is primary. Heat-pump allocation retains confirmed heating,
  DHW, idle/other, unknown-mode assumed heating, positive residual fallback, raw unassigned
  evidence, remaining unassigned energy and signed reconciliation error as distinct fields.
- AC classification uses selected heat/cool/off/dry/fan state before transient action.
- Primary COP is a ratio of measured thermal energy to exactly cutoff-matched Shelly
  electricity. Heating COP that contains assumed heating is explicitly provisional. Vendor
  COP is secondary and sparse cumulative readings remain interval evidence.
- Cumulative intervals that cross selected-period boundaries are disclosed separately and
  excluded from exact totals. No AC COP, inferred thermal output or forecast value is added.

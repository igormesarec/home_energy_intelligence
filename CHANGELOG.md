# Changelog

## V0.2.1 — draft

- Made a completed Yesterday available immediately from persisted hourly energy and
  temperature records. The non-persisted provisional view exposes internal gaps and
  is replaced by the authoritative daily row after the 05:00 close without double counting.
- Added additive schema V7 for distinct monthly/month-to-date Ecodan report snapshots,
  nullable fields, exact/date-only cutoff evidence, partial/final state, revisions and audit.
- Added a rolling 13-calendar-month interactive histogram, synchronized table and
  selected-metric CSV export with Shelly electricity as the primary measurement source.
- Separated confirmed heating, DHW, idle/other, assumed unknown-mode electricity,
  assumed positive residual, raw unassigned evidence, remaining unassigned energy and
  signed reconciliation error without assigning uncertainty to confirmed categories.
- Changed AC classification to prioritize the selected climate mode over transient
  `hvac_action`, preserving heat/cool electricity while the compressor is idle.
- Primary heating/DHW/combined COP now uses exactly matched Shelly-classified intervals;
  assumed heating produces an explicit provisional COP. Vendor COP remains secondary.
- Kept cross-boundary cumulative thermal intervals as evidence but excluded them from
  exact selected-period thermal totals. Missing heat and incomplete household values no
  longer render as false zeroes in the whole-house energy card.
- Added numeric axes to response, temperature and new monthly charts; forecasting remains
  a V0.3 placeholder and no AC COP or inferred thermal output is generated.

## V0.2.0 — draft

- Added additive schema V6 cumulative Ecodan thermal readings with source timestamps,
  source-unit/mapping provenance, revision audit, reset evidence and idempotent imports.
- Added admin-only manual cumulative reading create/edit/delete and bounded read-only
  Recorder backfill for configured heating/DHW thermal counters.
- Added four separate outdoor-response charts: Ecodan space-heating performance,
  whole-house heating electricity, AC cooling electricity and Ecodan DHW performance.
- Added Ecodan-only heating, DHW and combined COP as ratios of exactly matched sums.
  AC COP and AC thermal output remain unavailable; sparse counters remain interval data.
- Added the Whole-House Energy / Toplotni profil hiše card and explicit partially-known
  thermal status when AC thermal measurements do not exist.
- Kept forecasting as a V0.3 input contract only; no forecast values are emitted.

## V0.1.2 — draft

- Added a bounded authenticated historical temperature-timeline API. Today, Yesterday, −2 through −7 and custom single-day views now use persisted elapsed-hour evidence instead of drawing one daily-summary point; longer periods remain daily by default.
- Historical timelines include independent temperature-only hours without an energy parent, explicit unavailable intervals, local timezone/UTC timestamps, 23/25-hour DST identity, validated min/max and coverage, rejection reasons and source provenance. Daily summaries are never expanded into invented hourly points.
- Unified Overview cards and History indoor/outdoor charts on the same timeline component with local-time axes, visible gaps, isolated real points, keyboard/touch targets and a clear archive/backfill message when only a daily summary exists.
- Added additive Schema V5 temperature-only hourly/daily history so bounded Recorder backfill can recover validated temperatures even when no electricity row exists; energy, PV, HVAC allocations, checkpoints and V4 audits are not rewritten.
- Added administrator-only Diagnose, existing-record repair, dry-run temperature-only preview, resumable backfill, progress, cancellation, exact skip reasons and before/after coverage counts.
- Replaced mapping-identity reuse with chronological mapping events, including A→B→A, and added optional explicit bounded historical-identity attestations. Current mappings are never silently projected backward.
- Made current-day confirmed plus assumed/selected/estimated HVAC electricity visible as a known/provisional through-cutoff total while retaining strict category provenance and calendar-day finalization separately.
- Preserved full filtered temperature coverage as complete evidence, kept 95% response eligibility separate from 100% completeness, and normalized floating elapsed-time epsilon at true 100%.
- Replaced energy-season sliders with localized English/Slovenian month choices, a visible 1–28 day field and a season preview while preserving saved mappings, floor area and effective dates.
- Bumped the integration to 0.1.2 and added a panel JavaScript content hash to the module URL for deterministic HACS/restart cache invalidation.
- Added V4→V5 preservation, rollback, low-disk, idempotence, DST, shared-entity, current-cutoff heating, no-false-zero and frontend release-safety regressions.

## V0.1.1 — draft

- Required representative-temperature completeness now means exactly 100% validated elapsed coverage on normal and 23/25-hour dates; the independently configurable 95% threshold remains only a heating-response admission rule.
- Historical temperature repair now registers effective-dated source identity at setup, verifies entity ID, attribute, role, enabled state and the entire mapping interval before querying, preserves unidentifiable legacy summaries and reports every skipped mapping reason.
- Read-only InfluxDB fallback failures are isolated per temperature source. Timeouts, connection errors and malformed rows retain validated Recorder evidence, expose non-secret query-error provenance and leave uncovered time missing.
- Replaced temperature fallback row-count heuristics with canonical validated-duration checks in both normal and repair collectors. A carry-in state can be complete; invalid/unavailable rows can fall back to Recorder statistics and optional read-only InfluxDB; partial valid raw evidence remains authoritative and uncovered time remains missing.
- Hardened Schema V4 for constrained Home Assistant hosts: V1/V2/V3 upgrades are transactional through the version bump, conversion is keyset-batched, startup `VACUUM` was removed, lock/busy errors are not corruption, disk/schema/version-bump failures roll back and retry safely, and Settings exposes non-secret backup/migration status.
- Separated configurable 95% electrical-response admission from 100% temporal completeness and prevented current mappings from rewriting legacy temperature summaries whose historical entity identity is unknown.
- Replaced legacy stored row-quality trust with one read-time derivation shared by History, Overview completed days, sensors and CSV; date, household meter, equipment allocation and selected representative-temperature layers remain independently inspectable, while the old label is provenance only.
- Added explicit `unknown` quality for legacy classification rows whose elapsed coverage cannot be proven, and allowed a fully evidenced temperature repair to promote derived period quality without rebuilding energy.
- Added SQLite schema V4 normalization for temperature summaries and rejection audits, including atomic forward migration, injected-failure rollback, idempotent reopen and an approximately 1 MB storage-amplification bound.
- Added repair query/sample metrics and 2-/31-day bounded HA runtime benchmarks; the collector remains temperature-only and skips complete sources before Recorder reads.
- Exposed each derived quality layer and the disclosed electrical-response admission thresholds in the dashboard without adding thermal/COP/forecast claims.
- Made the backend `heating_response_profile()` the single qualification source for both Overview and House Thermal Profile; the frontend no longer derives a second point set from History rows.
- Replaced temperature repair's read/rebuild/generic-upsert sequence with interval-checked `BEGIN IMMEDIATE` merges that update only temperature payload evidence, checksum/revision metadata and affected-source rejection audits.
- Added a temperature-only bounded Recorder collector. It never requests equipment energy, power, modes, PV or grid entities; complete sources are skipped before any Recorder query, repairs are serialized and startup failures cannot block hourly/checkpoint/daily recovery.
- Strengthened diagnostics with live entity/attribute/unit state, Recorder query errors/range, persisted coverage seconds/percentage and representative stable-source IDs.
- Kept unavailable Energy Split categories null and visibly unavailable instead of converting them to display zeroes.
- Added idempotent completed-hour aggregation, restart recovery and an additive SQLite schema V3 migration.
- Made the backend canonical energy contract authoritative for Overview, History and authenticated CSV export.
- Added today-through-last-completed-hour actuals, yesterday same-cutoff/full-day comparisons, freshness labels and manual/hourly refresh.
- Added dynamic per-equipment evidence, representative indoor-source selection, temperature timelines and guarded PV analytics.
- Added matching representative indoor/outdoor cards and selectors with latest-valid timestamps, time-weighted period statistics, coverage, quality, yesterday comparisons and gap-preserving timelines.
- Expanded History with bounded date filtering, pagination, period presets and CSV export.
- Corrected missing-energy semantics: complete measured zero, complete totals, known partial subtotals and unavailable totals are now distinct in Overview, History, device tables, charts and CSV.
- Added exact expected-hour coverage with internal-gap detection, 23/25-hour DST handling and controlled idempotent repair of delayed partial intervals.
- Added all requested Home Assistant-timezone calendar presets, configurable July–June energy seasons and authoritative whole-period summaries independent of detail pagination.
- Added complete dynamic device heating/cooling evidence, including confirmed/selected/estimated AC cooling, and preserved signed negative reconciliation discrepancies.
- Added measured PV/import/export/on-site-use/household history with guarded self-sufficiency and PV self-consumption rates.
- Made CSV export honor the actual selected range beyond 366 days and preserve History filters during automatic refresh.
- Preserved the approved six-tab design and kept COP, thermal modelling and forecasts explicitly deferred.
- Made History → Today use the same completed-hour cutoff as Overview without creating a finalized daily row; Today CSV uses the same in-memory partial record.
- Added a separately reconciled Other HVAC category for fan/dry operation across split charts, device tables and CSV, while keeping unallocated energy and signed discrepancy distinct.
- Split period quality into date availability, household-meter, HVAC-allocation and per-source temperature coverage so unrelated temperature gaps no longer erase valid meter totals.
- Counted absent local dates, including 23/25-hour days, in period temperature coverage while retaining duration-weighted valid means and validated min/max.
- Added the shared Home Assistant-timezone period selector to Overview and made representative-source charts honor only the configured stable source ID.
- Removed silent first-source representative defaults, added an in-flow representative-source preview/test, and made unconfigured indoor/outdoor cards explicitly unavailable instead of showing inferred 100% coverage.
- Changed allocation coverage from binary good-day counts to classified elapsed seconds across every enabled equipment ID; partial device meters and classification evidence remain visible without promoting unknown categories to zero.
- Added an evidence-bound whole-house **electrical** heating-response profile normalized by floor area while explicitly withholding unmeasured thermal output, COP and design-day claims.
- Added mobile period-control disclosure, bounded pager state, compact empty states, touch-sized controls and horizontally scrollable evidence tables for 360–430 px layouts.
- Added explicit PV/grid balance evidence (`PV − export`, `self-use + import`) without overwriting measured household electricity.
- Made Options Flow transactional: skip preserves queued mappings, disable retains the complete stable-ID record, remove is explicit and failed validation restores the draft unchanged.
- Made absent legacy allocation-coverage metadata unknown rather than complete; independent meter totals and known partial subtotals remain visible without exposing complete categories.
- Replaced calendar-row date coverage with DST-aware elapsed local-day seconds, including partial current-day date availability and completed-hour processing coverage.
- Qualified the electrical heating-response profile with configurable representative-outdoor coverage (95% default), complete enabled-device classification, reconciliation, 10 days, 8 °C observed span and R² ≥ 0.5; excluded dates and reasons remain visible.
- Prevented partial equipment allocations from rendering as zero-height confirmed comparison bars and added 360/390/430 px responsive layout evidence.
- Added end-to-end representative-temperature pipeline diagnostics (mapping/live state/unit/Recorder range/rejections/hourly and daily persistence/missing intervals) and administrator-only bounded repair.
- Added idempotent temperature-only recovery for the latest two local days at startup and up to 31 selected days on demand; valid energy, allocation and stronger temperature summaries are preserved.
- Replaced the Daily Energy Split's ambiguous simultaneous `missing`/`partial` labels with independent incomplete-period, missing-meter, incomplete-classification, estimated-allocation and missing-temperature-evidence indicators.
- Separated canonical electricity quality from temperature quality so missing temperature evidence cannot downgrade a complete metered and classified energy result.

## V0.1.0 — draft

### Corrected after review

- Replaced fixed Ecodan/three-AC fields with guided, extensible equipment and source mappings.
- Added per-temperature physical limits, −55/155 sentinels, temporal spike filtering and detailed rejection audits.
- Made persisted six-hour checkpoints the primary daily classification source with authoritative counter reconciliation and restart recovery.
- Preserved idle, assumed heating, raw residual, uncertainty and provenance independently.
- Added escaped Unicode InfluxQL identifiers, bounded chunked reads, `autogen` support and Authorization-header credentials.
- Moved database exports from `/config/www` to administrator-only protected storage.
- Added mapping revisions, current Home Assistant runtime CI tests and expanded DST/recovery/security acceptance coverage.
- Corrected Home Assistant test registration so the real config and Options Flow handlers are exercised.
- Made Options Flow edit stable equipment and temperature records in place; unrelated changes no longer clear mappings.
- Added canonical Wh/kWh/MWh → kWh, W/kW → W and °F → °C normalization with source-unit provenance.
- Removed installation-specific dashboard keys; equipment metadata and allocation IDs now come from the authenticated WebSocket API.
- Made checkpoint recovery complete each enabled equipment ID independently without replacing valid checkpoints.
- Kept stable Recorder temperature states valid until an explicit unavailable state; freshness limits apply to point samples rather than value-change age.
- Synchronized English and Slovenian guided-flow translations and documented administrator-only settings access.
- Replaced removed `frontend.async_panel_exists` usage with the supported frontend panel registry and made static-path/panel registration reload-safe.
- Updated Options Flow for Home Assistant's framework-provided, read-only `config_entry` property.
- Removed invalid `None` defaults from optional guided-flow fields and supplied the isolated runtime harness with Home Assistant's HTTP registration contract.
- Reject contaminated long-term temperature-statistic intervals when their min/max bounds contain sentinels or physical outliers.

COP, thermal-profile modelling and weather-dependent forecasting remain deferred and display no invented values.

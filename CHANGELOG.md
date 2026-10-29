# Changelog

## V0.1.1 — draft

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

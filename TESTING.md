# Testing

## V0.2 acceptance evidence

The V0.2 suite covers cumulative unit normalization, DST ambiguous/nonexistent wall
times, 25-hour elapsed intervals, first-reading baselines, counter resets, ratio-of-sums,
exact hourly classification matching, exclusion of assumed heating, schema V5→V6,
audit-preserving edit/delete, idempotent storage, and executable Node rendering of all
four response charts with visible heating and DHW interval COP values.

Run tests without Home Assistant hardware or network access:

```bash
python -m pytest
python -m compileall -q custom_components tests
node --check custom_components/home_energy_intelligence/frontend/hei-panel.js
```

Covered acceptance cases include:

- Ljubljana 23-, 24-, and 25-hour local days and the seven-elapsed-hour midnight-to-06:00 autumn cycle.
- Multiple heat-pump transitions inside one six-hour interval.
- Unknown operation assigned to assumed heating and explicit idle kept separate.
- Counter reconciliation, reset, spike rejection, missing power, and visible residual.
- Correct 5 °C for 55 minutes plus 10 °C for 5 minutes result: 5.4167 °C, unaffected by frequent unchanged updates.
- Rejection of a 155 °C outdoor spike.
- Rejection of −55 °C sentinels, temporal jumps, and audited invalid durations without rejecting legitimate winter temperatures.
- Explicit unavailable spans and freshness-limited coverage.
- Idempotent daily upserts and revision increments after corrections.
- Database corruption preservation/recovery and ten-year compact-growth evidence.
- Optional InfluxQL query constrained to read-only `SELECT`.
- Unicode/escaped InfluxQL identifiers, `homeassistant`/`autogen`, bounded queries, and credentials absent from URLs.
- Persisted-checkpoint daily classification after Recorder expiration and counter-correction residuals.
- Variable equipment counts, optional sensors, remapped entity IDs and effective-dated source provenance.
- Zero-AC and multiple-heat-pump configurations, no-PV grid balance, and absent temperature sources.
- Wh/kWh/MWh, W/kW and °F/°C normalization plus incompatible-unit rejection and source-unit provenance.
- Per-equipment checkpoint completeness, including one valid device beside independently missing devices.
- Stable equipment/source IDs and temperature filters surviving unrelated Options Flow changes.
- Equipment-neutral dashboard source checks and authenticated-user/admin authorization boundaries.
- Canonical Overview/History/export energy fields, missing-equipment behavior and residual reconciliation.
- Idempotent hourly persistence, bounded history pagination and all 25 elapsed hours on the October DST fallback day.
- Internal missing-hour detection even when the latest hour exists, explicit coverage percentages, controlled late-reading repair and exact 23/25-hour expected interval counts.
- Complete-zero versus known-partial versus unavailable energy semantics at totals and device level.
- Monday–Sunday weeks, calendar months rather than rolling 30 days, previous years, configurable current/previous energy seasons and browser-timezone-independent cutoffs.
- Authoritative full-year summaries across multiple 90-row detail pages and selected-range CSV without a 366-day truncation.
- Confirmed/selected/estimated device cooling, signed negative discrepancies and gap-preserving duration-weighted period temperatures.
- Python compilation and frontend JavaScript syntax validation for every release candidate.
- Unchanged Recorder temperatures remaining valid independently of their last value-change time.
- Contaminated long-term temperature-statistic bounds producing rejected, zero-coverage intervals.
- History Today replacement from completed hourly rows without daily writes, shared cutoff propagation and Home Assistant-timezone labelling.
- Separate date/meter/allocation/temperature period quality, missing-date temperature denominators and preservation of valid meter totals when temperature coverage is partial.
- Elapsed-time allocation coverage for partial equipment, unavailable (not 100%) coverage with no configured equipment, explicit representative-source opt-in and representative live preview evidence.
- Supplied-site PV/grid rounding balance and heat-pump/partial-AC classification baselines without converting partial classifications into confirmed zeroes.
- Frontend contracts for the electrical-only normalized heating-response profile, explicit unconfigured representatives, bounded pagination and compact mobile controls.
- Nonzero Other HVAC fan/dry allocation with zero discrepancy, plus configured representative-source enforcement in frontend charts.
- Current Home Assistant panel registration API, duplicate prevention, reload re-registration and unload removal.
- Current Home Assistant read-only Options Flow `config_entry` lifecycle.
- October 2026 Recorder fixture values and 25 October whole-house equation: 23.732 + 32.620 − 13.721 = 42.631 kWh.
- Transactional Options Flow with one heat pump, three AC units, three temperature mappings and representative selections: unchanged save, skip, disable, explicit remove and failed validation.
- Legacy allocations without coverage metadata remaining unknown while preserving the meter and known subtotals; explicit 100% compatibility metadata remains complete.
- DST-duration date coverage for complete 23/25-hour dates and partial current dates, independently from completed-hour meter coverage.
- Electrical-response admission and exclusions for low outdoor coverage, incomplete AC classification, missing representative source and −55 °C contamination; frontend guards require 10 dates, 8 °C span and R² ≥ 0.5.
- Partial equipment rows retained in evidence but excluded from confirmed comparison bars rather than rendered as zero.
- October 29 blank representative-temperature symptom: Recorder samples retain source/entity/unit/range provenance, missing persisted summaries receive temperature-only repair, stronger existing values and all energy/allocation fields remain unchanged, and retries are idempotent.
- Temperature diagnostic and repair WebSockets are administrator-only, bounded to 31 days and expose live/Recorder/rejection/hourly/daily/missing-interval stages.
- Daily Energy Split uses independent incomplete-period, missing-meter, incomplete-classification, estimated-allocation and missing-temperature labels; a current partial day is not also called missing merely because it is partial.
- Overview and House Thermal Profile consume one backend-qualified response-point set with the exact 95% coverage boundary, dynamic per-device evidence and explicit exclusions.
- Atomic temperature repair rejects mismatched UTC intervals/entity provenance and quality downgrades, preserves allocations/unrelated rejection audits, keeps checksums valid, is idempotent and rolls back payload/revision changes on a simulated write failure.
- The lean temperature collector's Recorder entity sets exclude HVAC energy/power/mode, PV and grid sources; a successful raw-state repair performs one bounded history query per queried day, statistics is requested only when validated state coverage is zero, and a complete stored source performs zero queries.
- Energy Split frontend contracts preserve unavailable category nulls and label them unavailable rather than converting them to confirmed zero segments.
- Read-time quality recomputation: stored-partial plus complete repaired required layers becomes good; missing representative outdoor evidence remains partial; legacy unknown classification coverage remains unknown; optional unrelated sources do not erase complete meter evidence.
- Schema V4 migration preserves hydrated daily values, revision history and rejection audits; V1/V2/V3/V4 paths are idempotent; thousands of daily/hourly rows migrate with bounded memory; injected schema, conversion, disk-full and pre-version-bump failures roll back; a locked database is not renamed as corrupt; startup never runs `VACUUM`.
- Recorder temperature fallback tests cover a single valid carry-in state, multiple invalid sentinel rows with valid statistics, explicit-unavailable gaps, partial raw coverage that must remain a gap, and bounded read-only InfluxDB recovery. Selection is based on validated elapsed seconds, never row count.
- Temperature-only repair benchmarks exercise 2 and 31 inclusive local days, assert exactly one bounded Recorder call per day when the raw carry state is valid, zero non-temperature entities/Influx calls, bounded peak normalized samples and a 64 MiB runtime allocation ceiling in the HA test harness.
- Temperature quality boundaries cover 0%, 94.99%, 95%, 97%, 99.9% and 100% on ordinary, 23-hour and 25-hour dates, proving that response eligibility and full completeness remain separate.
- Effective-dated repair tests cover unchanged mappings, remapped entities, changed attributes/roles, mapping transitions, legacy rows and missing provenance while asserting electricity, allocations and checksums remain untouched.
- Home Assistant runtime fallback tests isolate InfluxDB timeout, connection and malformed-row failures per source, preserve a parallel 100%-validated Recorder source and prove the failed source remains zero-coverage rather than zero-valued or complete.
- Schema V5 tests recover independent temperature-only hourly/daily summaries with no energy parent, retain null energy fields, preserve V4 energy/checkpoint payloads and checksums, and make a second identical backfill a no-op.
- Chronological mapping tests cover A→B→A without obsolete effective-date reuse; one physical Recorder entity can serve multiple stable logical temperature roles with one history query.
- Bounded temperature-only job tests cover preview/action-required truthfulness, before/after counts, 23/25-hour days, idempotence, cancellation/restart metadata and no writes to Recorder or energy tables.
- Current-cutoff tests reproduce 9.0 kWh household, 3.1 kWh confirmed heat-pump heating and 1.0 kWh selected AC heating as a 4.1 kWh known/provisional total without promoting selected energy to confirmed.
- Frontend regressions retain content-hash cache busting, distinct Diagnose/existing-repair/temperature-only actions and a non-green zero-recovery action-required result; V0.2 additionally executes all four response-chart renderers under Node.
- Historical timeline regressions use independently defined October 29 hourly fixtures whose elapsed-hour aggregation remains 23.0 °C indoor (22.5/23.2 min/max) and 11.5 °C outdoor (9/17 min/max). Executable Web Component rendering proves 24 points and a 00:00/06:00/12:00/18:00/24:00 local-time axis rather than a daily dot.
- Timeline tests cover daily-only evidence with an honest backfill-required message, two-hour visible gaps, one truthful isolated point, filtered 100% coverage, bounded daily resolution, and 23/25 elapsed-hour DST days with two offset-distinct fall-back 02:00 intervals.
- The authenticated timeline API accepts at most seven local days at hourly resolution and eight configured source IDs; ordinary month/year navigation uses compact daily summaries and never performs raw Recorder reads.

## Responsive layout evidence

The release-candidate panel bundle is rendered with mocked WebSocket responses in
installed Chrome at 360, 390, 430, 768 and 1440 CSS pixels. The automated evidence
checks page/period-control overflow, scrollable wide evidence tables and disabled
first/last-page controls. This supplements, but does not replace, a live Home
Assistant test on physical Android hardware.

## Home Assistant runtime suite

CI installs current Home Assistant and `pytest-homeassistant-custom-component`. Runtime tests verify the Recorder helper signatures plus config/Options Flow entry points, setup, reload, unload, sidebar registration and sensor creation/refresh. These tests are skipped locally when the Home Assistant harness is absent; a disposable real Home Assistant smoke test remains required before tagging V0.1.

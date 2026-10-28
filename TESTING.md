# Testing

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
- Current Home Assistant panel registration API, duplicate prevention, reload re-registration and unload removal.
- Current Home Assistant read-only Options Flow `config_entry` lifecycle.
- October 2026 Recorder fixture values and 25 October whole-house equation: 23.732 + 32.620 − 13.721 = 42.631 kWh.

## Home Assistant runtime suite

CI installs current Home Assistant and `pytest-homeassistant-custom-component`. Runtime tests verify the Recorder helper signatures plus config/Options Flow entry points, setup, reload, unload, sidebar registration and sensor creation/refresh. These tests are skipped locally when the Home Assistant harness is absent; a disposable real Home Assistant smoke test remains required before tagging V0.1.

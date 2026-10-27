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
- Correct 5 °C for 60 minutes plus 6 °C for 5 minutes result: 5.076923 °C.
- Rejection of a 155 °C outdoor spike.
- Explicit unavailable spans and freshness-limited coverage.
- Idempotent daily upserts and revision increments after corrections.
- Database corruption preservation/recovery and ten-year compact-growth evidence.
- Optional InfluxQL query constrained to read-only `SELECT`.
- October 2026 Recorder fixture values and 25 October whole-house equation: 23.732 + 32.620 − 13.721 = 42.631 kWh.

## Integration test limitation

The repository's pure calculation and storage suite runs without installing the large Home Assistant test harness. Before release, install the integration in a disposable Home Assistant instance and verify config-flow selectors, Recorder imports, panel registration, services, and reload/unload behavior against the minimum supported Home Assistant version.

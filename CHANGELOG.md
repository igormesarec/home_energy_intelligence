# Changelog

## V0.1.0 — draft

### Corrected after review

- Replaced fixed Ecodan/three-AC fields with guided, extensible equipment and source mappings.
- Added per-temperature physical limits, −55/155 sentinels, temporal spike filtering and detailed rejection audits.
- Made persisted six-hour checkpoints the primary daily classification source with authoritative counter reconciliation and restart recovery.
- Preserved idle, assumed heating, raw residual, uncertainty and provenance independently.
- Added escaped Unicode InfluxQL identifiers, bounded chunked reads, `autogen` support and Authorization-header credentials.
- Moved database exports from `/config/www` to administrator-only protected storage.
- Added mapping revisions, current Home Assistant runtime CI tests and expanded DST/recovery/security acceptance coverage.

COP, thermal-profile modelling and weather-dependent forecasting remain deferred and display no invented values.

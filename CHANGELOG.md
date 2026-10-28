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

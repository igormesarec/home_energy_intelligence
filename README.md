# Home Energy Intelligence

Home Energy Intelligence is a HACS-installable Home Assistant custom integration for validated daily whole-house energy records. V0.1 focuses on trustworthy collection and allocation: it does not estimate thermal production, COP, or weather-dependent forecasts.

![Approved dashboard design](docs/design/home_energy_intelligence_dashboard.png)

## V0.1 features

- Guided General, Heat pumps, Air conditioners, Solar/grid, Temperatures, Weather, Data sources, and Data quality UI sections. Every source entity and operating-state mapping is configurable.
- Recorder-first raw history and long-term-statistics reads, always off the event loop.
- Persisted six-hour, transition-aware allocations for any configured heat pumps and zero or more AC units; checkpoints are the primary daily classification evidence.
- Unknown heat-pump operation is `heating_assumed`; confirmed idle and raw reconciliation residual remain separate.
- True elapsed-time-weighted temperature mean/min/max with preceding-state lookup, missing coverage, freshness limits, and outlier rejection.
- DST-safe 23-, 24-, and 25-hour local days using the Home Assistant time zone.
- Idempotent 05:00 daily close and correction-safe catch-up in a dedicated compact SQLite database.
- Optional read-only InfluxDB 1.x backfill for explicitly mapped sources.
- Responsive Overview, History, and Settings panel with six-tab navigation. Deferred tabs never display invented measurements.
- Daily summary and quality sensors only; no duplicate high-frequency entities.

## Installation

### HACS custom repository

1. In HACS, open **Integrations**, choose **Custom repositories**, and add `https://github.com/igormesarec/home_energy_intelligence` as an Integration repository.
2. Install **Home Energy Intelligence** and restart Home Assistant.
3. Go to **Settings → Devices & services → Add integration** and select **Home Energy Intelligence**.
4. Add each heat pump and optional AC with a display name, electrical counter, power and operating-state entity. Map the installation's state strings to the documented categories.
5. Add optional PV/grid, temperature and weather sources. Each temperature has its own physical limits, invalid sentinels, spike limit, freshness and missing-data thresholds.
6. Review Recorder-first historical collection; enable read-only InfluxDB backfill only if needed.
7. Confirm the Home Assistant time zone and wait for the first completed six-hour checkpoint. The previous calendar day is finalized at 05:00 local time.

For a manual install, copy `custom_components/home_energy_intelligence` into the Home Assistant configuration directory's `custom_components` folder, then restart.

## Safety and privacy

The integration never writes to Recorder or InfluxDB, changes retention, or modifies source entities. It stores only compact derived checkpoints and daily records under:

```text
<config>/.storage/home_energy_intelligence/home_energy_intelligence.sqlite3
```

Do not map the same physical counter more than once. Circuit 2K is explicitly rejected for supply/return mapping. Missing return temperature remains missing: the integration never substitutes another circuit. V0.1 never derives thermal energy from electricity.

## Services

- `home_energy_intelligence.reprocess_date`: rebuild one day.
- `home_energy_intelligence.reprocess_range`: rebuild up to 31 days.
- `home_energy_intelligence.export_database`: administrator-only consistent backup under `<config>/.storage/home_energy_intelligence/exports`; it is never exposed through `/config/www`.

## Development

Use Python 3.13 or the Python version required by the targeted Home Assistant release:

```bash
python -m pytest
python -m compileall -q custom_components tests
node --check custom_components/home_energy_intelligence/frontend/hei-panel.js
```

See [testing](TESTING.md), [data quality](DATA_QUALITY.md), [schema](DATA_SCHEMA.md), [entity mapping](ENTITY_MAPPING.md), and [InfluxDB backfill](INFLUXDB_BACKFILL.md).

## Scope

- **V0.1:** validated electricity, temperature, source quality, and basic dashboard/history/settings.
- **V0.2:** manually or natively sourced thermal energy, whole-house thermal response, and correctly weighted COP.
- **V0.3:** weather sources and probabilistic forecasting.

License: MIT.

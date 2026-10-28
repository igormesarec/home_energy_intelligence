# Read-only InfluxDB 1.x backfill

InfluxDB is optional and is only consulted when Recorder raw history and applicable long-term statistics cannot provide at least two usable observations.

Enable it in integration options and supply host, port, TLS verification, database (normally `homeassistant`), retention policy (normally `autogen`), and optional credentials. The mapping is JSON keyed by the configured source key:

```json
{
  "outdoor_temperature": {
    "measurement": "temperature",
    "field": "value",
    "entity_tag": "outdoor_temperature"
  },
  "ecodan_energy": {
    "measurement": "kWh",
    "field": "value",
    "entity_tag": "heat_pump_energy"
  }
}
```

Unicode measurement and field names, including `°C`, are supported through escaped InfluxQL identifiers. Tag values are escaped separately. Queries are bounded to at most 31 days, split into one-day requests, limited by a point budget, and deduplicated by source timestamp.

Queries use HTTP `POST`; credentials are sent using the Authorization header and never appear in URL query parameters. The client exposes no write, retention, purge, or administrative operation.

Influx schemas vary. Verify the measurement, field, entity tag, unit, and counter semantics against your own database before enabling a mapping. If returned history cannot be verified, leave the mapping absent; the resulting period remains unavailable.

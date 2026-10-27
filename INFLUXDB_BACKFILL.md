# Read-only InfluxDB 1.x backfill

InfluxDB is optional and is only consulted when Recorder raw history and applicable long-term statistics cannot provide at least two usable observations.

Enable it in integration options and supply host, port, database, and optional credentials. The mapping is JSON keyed by the integration mapping name:

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

Identifiers accept only letters, numbers, and underscores; entity tags additionally accept `.`, `_`, and `-`. Queries are HTTP `GET` requests containing only `SELECT … WHERE time … ORDER BY time`. The client has no write, retention, purge, or administrative operation.

Influx schemas vary. Verify the measurement, field, entity tag, unit, and counter semantics against your own database before enabling a mapping. If returned history cannot be verified, leave the mapping absent; the resulting period remains unavailable.

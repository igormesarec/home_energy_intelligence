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

The experimental administrator-only historical energy preview is in Settings.
Choose up to 31 local dates and, where the mapping predates known history, attest
that the current mapping applied. It does not write historical records. Configure
Influx entries by stable equipment ID: energy entries use `measurement`,
`entity_tag`, and either a configured `unit` or the live entity's known unit.
An optional `entity_id` must match the configured energy entity. State entries
require their `entity_id`; `equipment.<id>.fallback_state` names the selected
mode, while `equipment.<id>.actual_dhw_state` additionally requires
`actual_operation: true`. State freshness defaults to 60 minutes.

The preview reports observed counter increments inside the day and excludes
unsupported boundary contributions and gaps longer than one hour. Mixed-mode
increments remain unknown, including when power readings exist. It selects
one counter source by coverage rather than combining partial sources. This is
not a historical backfill: recovery execution and persistence are not implemented.

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

The coordinator has an experimental, read-only single-day preview, not exposed
to the dashboard or a WebSocket command. It requires the current source mapping
to have been effective for the requested date or explicit administrator
attestation. Configure Influx mapping entries by stable equipment ID, with an
explicit `entity_id`; energy entries also require `measurement`, `unit`, and
`entity_tag`. The optional `equipment.<id>.fallback_state` entry names the
selected-mode source. State freshness defaults to 60 minutes. The preview uses
the strict historical policy: unknown and residual electricity remain separate.

The preview conservatively requires counter samples at both exact day boundaries and
no counter gap longer than one hour. It may therefore report unavailable even
when partial measured evidence exists. It does not yet implement separately
configured actual-DHW evidence, a user-facing authorized preview, safe recovery
execution, historical record merging, cancellation, or job resumption. Do not
use it for a historical energy backfill. Existing temperature recovery remains
separate.

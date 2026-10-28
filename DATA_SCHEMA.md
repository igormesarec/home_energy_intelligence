# Data schema

The dedicated SQLite database uses WAL mode, foreign keys, transactional writes, SHA-256 payload checksums, and a version table.

| Table | Purpose | Retention |
|---|---|---|
| `daily_records` | One authoritative JSON-backed record per local date | Indefinite; compact |
| `allocations` | Device/category kWh, quality and coverage | Replaced with daily revision |
| `checkpoints` | Six-hour UTC interval payloads | Indefinite in V0.1 |
| `rejected_readings` | Non-destructive rejection audit | Indefinite |
| `job_runs` | Processing status and diagnostic metadata | Indefinite |
| `revisions` | Prior/new checksum and reason | Indefinite |
| `manual_thermal_readings` | Reserved for measured/controller/manual thermal input | Future use |
| `forecast_snapshots` | Reserved for forecast provenance | Future use |
| `model_parameters` | Reserved for versioned model parameters | Future use |
| `mapping_revisions` | Effective-dated, non-secret source/equipment provenance | Indefinite |

`daily_records.local_date` is unique. Upserting unchanged content is a no-op; corrected content increments `revision`. Database integrity is checked at startup. A corrupt file is preserved with a timestamped `.corrupt-*.sqlite3` name before a new schema is created.

Checkpoint upserts use interval/device primary keys and checksums, making restart recovery and retries idempotent. Daily finalization reads stored checkpoints first and uses Recorder's daily counter change to reconcile rather than reclassify valid checkpoint evidence.

The ten-year synthetic growth test keeps 3,650 compact daily rows below 5 MB in the test environment. Actual size depends on checkpoint detail and retained revisions.

# Data schema

The dedicated SQLite database uses WAL mode, foreign keys, transactional writes, SHA-256 payload checksums, and a version table.

| Table | Purpose | Retention |
|---|---|---|
| `daily_records` | One authoritative JSON-backed record per local date | Indefinite; compact |
| `allocations` | Device/category kWh, quality and coverage | Replaced with daily revision |
| `checkpoints` | Six-hour UTC interval payloads | Indefinite in V0.1 |
| `hourly_records` | Completed UTC-hour payloads for live/partial views | Indefinite in V0.1.1 |
| `temperature_summaries` | Normalized per-source daily/hourly statistics and provenance | Indefinite |
| `hourly_temperature_rejections` | Deduplicated hourly rejection evidence, separate from compact summaries | Indefinite |
| `rejected_readings` | Non-destructive rejection audit | Indefinite |
| `job_runs` | Processing status and diagnostic metadata | Indefinite |
| `revisions` | Prior/new checksum and reason | Indefinite |
| `manual_thermal_readings` | Reserved for measured/controller/manual thermal input | Future use |
| `forecast_snapshots` | Reserved for forecast provenance | Future use |
| `model_parameters` | Reserved for versioned model parameters | Future use |
| `mapping_revisions` | Effective-dated, non-secret source/equipment provenance | Indefinite |

`daily_records.local_date` is unique. `hourly_records` is unique by exact UTC start/end, so both repeated local hours on a DST fallback day remain distinct and retries are idempotent. Upserting unchanged content is a no-op; corrected daily content increments `revision`. Schema V4 keeps the V3 hourly layer and moves repeated temperature summaries/audit arrays out of record blobs. Reads hydrate the same logical payload, so analytics and exports remain compatible. The forward migration is transactional, preserves logical checksums/revisions and auxiliary tables, then compacts freed pages only after commit. A failed migration leaves version 3 and inline payloads intact. Database integrity is checked at startup. A corrupt file is preserved with a timestamped `.corrupt-*.sqlite3` name before a new schema is created.

Overview, History and CSV export receive the same backend `energy` object. It separates confirmed, assumed, selected and estimated classifications; idle; other HVAC (fan/dry); raw residual evidence; unallocated energy; other household use; and signed reconciliation discrepancy. Missing required sources remain `null`, never a manufactured zero.

Period responses expose independent `date_coverage`, `meter_coverage`, `allocation_coverage` and per-source `temperature_coverage`. A current-date response may contain an in-memory `partial: true`, `finalized: false` record assembled from `hourly_records`; this record is not stored in finalized daily history.

Checkpoint upserts use interval/device primary keys and checksums, making restart recovery and retries idempotent. Daily finalization reads stored checkpoints first and uses Recorder's daily counter change to reconcile rather than reclassify valid checkpoint evidence.

Temperature history repair does not call the general daily/hourly upsert path.
It acquires an immediate SQLite write transaction, re-reads the latest row,
requires exact stored UTC boundaries and entity provenance, then changes only
normalized temperature evidence, the logical SHA-256 checksum and row update/revision metadata. Daily
repairs add a `temperature_history_repair` revision; hourly repair follows the
existing no-revision hourly convention. Allocation rows and unrelated rejection
audits are untouched. A failure rolls the payload, checksum, revision and audit
changes back together.

The ten-year synthetic growth test keeps 3,650 compact daily rows below 5 MB in the test environment. A migration test injects an approximately 1 MB rejection payload and requires the resulting database to remain below 2× the same logical payload after compaction. Actual size depends on checkpoint detail and retained revisions.

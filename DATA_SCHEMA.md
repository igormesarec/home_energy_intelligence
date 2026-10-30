# Data schema

## Schema V6 thermal ledger

`thermal_meter_readings` stores cumulative Ecodan observations independently of daily
energy rows. Its uniqueness key is stable equipment ID + meter identity + source UTC
timestamp, making Recorder backfill idempotent. `thermal_meter_reading_audit` retains
create, update and delete revisions. V6 is additive: migration does not rewrite daily,
hourly, checkpoint, allocation, checksum or temperature evidence.

Derived thermal intervals are recomputed from adjacent observations so a backdated edit
updates both neighbouring intervals. Resets and missing endpoints remain explicit issues.
Only intervals exactly tiled by complete classified Ecodan electricity can produce COP.

The dedicated SQLite database uses WAL mode, foreign keys, transactional writes, SHA-256 payload checksums, and a version table.

| Table | Purpose | Retention |
|---|---|---|
| `daily_records` | One authoritative JSON-backed record per local date | Indefinite; compact |
| `allocations` | Device/category kWh, quality and coverage | Replaced with daily revision |
| `checkpoints` | Six-hour UTC interval payloads | Indefinite in V0.1 |
| `hourly_records` | Completed UTC-hour payloads for live/partial views | Indefinite in V0.1.1 |
| `temperature_summaries` | Normalized per-source daily/hourly statistics and provenance | Indefinite |
| `temperature_history_records` | Schema V5 independent hourly/daily temperature summaries keyed by UTC interval and stable source ID | Indefinite |
| `temperature_mapping_events` | Chronological mapping occurrences; repeated A→B→A identities remain distinct | Indefinite |
| `temperature_mapping_attestations` | Explicit administrator-approved bounded historical identity intervals | Indefinite |
| `temperature_backfill_jobs` | Bounded preview/backfill cursor, cancellation, counters and exact skip reasons | Indefinite |
| `hourly_temperature_rejections` | Deduplicated hourly rejection evidence, separate from compact summaries | Indefinite |
| `rejected_readings` | Non-destructive rejection audit | Indefinite |
| `job_runs` | Processing status and diagnostic metadata | Indefinite |
| `revisions` | Prior/new checksum and reason | Indefinite |
| `manual_thermal_readings` | Reserved for measured/controller/manual thermal input | Future use |
| `forecast_snapshots` | Reserved for forecast provenance | Future use |
| `model_parameters` | Reserved for versioned model parameters | Future use |
| `mapping_revisions` | Effective-dated, non-secret source/equipment provenance | Indefinite |

`daily_records.local_date` is unique. `hourly_records` is unique by exact UTC start/end, so both repeated local hours on a DST fallback day remain distinct and retries are idempotent. Upserting unchanged content is a no-op; corrected daily content increments `revision`. Schema V4 keeps the V3 hourly layer and moves repeated temperature summaries/audit arrays out of record blobs. Schema V5 is additive: independent temperature records can be joined into History and cards without manufacturing an energy parent. Schema V6 adds only the cumulative thermal/audit tables. The migrations do not update daily/hourly/checkpoint payloads or checksums. All forward migrations run transactionally, update the version only after success and never run startup `VACUUM`. A failed migration leaves the prior version retryable. Lock/busy errors are never treated as corruption. Database integrity is checked at startup. A genuinely corrupt file is preserved with a timestamped `.corrupt-*.sqlite3` name before a new schema is created.

Temperature-only rows contain temperature statistics and provenance only. Their energy, PV, import/export, non-HVAC and allocation fields are unavailable (`null`/empty), never zero. Read paths overlay stronger independent temperature summaries onto matching energy dates and expose temperature-only dates where no energy record exists. Heating-response admission still requires separate completed-day electricity evidence.

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

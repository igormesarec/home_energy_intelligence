# Historical electricity recovery — incomplete draft

This branch adds an optional read-only recovery plan to the existing historical
preview and separate schema-v8 job and evidence tables. Existing daily, hourly,
temperature, thermal and mapping tables are not recovery write targets.

The schema migration is additive. The storage primitives commit a day's
provisional source evidence and its cursor together, reject overlapping active
jobs, and do not replace existing evidence. The preview creates no job and
performs no writes.

Administrator start, status and cancellation use the previewed range. Explicitly
started unfinished jobs resume after restart if their source configuration is
unchanged. The House Thermal Profile shows recovered evidence separately as
provisional monthly subtotals; existing electrical source-days are excluded.
The recovery path does not update daily or hourly records or verified COP.

The storage reader deliberately excludes a source-day when multiple evidence
checksums exist or an evidence checksum fails validation. There is currently no
administrator-approved version selection for resolving those cases. A single
candidate with verified mapping requires a matching persisted event covering the
day. Exact-source observations without historical mapping proof can be shown
only as explicitly unverified provisional evidence. Influx evidence also requires
the strict source-identity check. Selecting the newest observation would risk
silently replacing stronger evidence; conflicting versions remain excluded.

This draft has not been validated against a backed-up live Home Assistant
installation. No historical recovery should be run in production before review
and explicit administrator approval.

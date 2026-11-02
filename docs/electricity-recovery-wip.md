# Historical electricity recovery — incomplete draft

This branch adds an optional read-only recovery plan to the existing historical
preview and separate schema-v8 job and evidence tables. Existing daily, hourly,
temperature, thermal and mapping tables are not recovery write targets.

The schema migration is additive. The storage primitives commit a day's
provisional source evidence and its cursor together, reject overlapping active
jobs, and do not replace existing evidence. The preview creates no job and
performs no writes.

**Not ready for installation or recovery:** administrator start, status,
resume and cancellation controls are not connected to an execution worker;
the monthly and House Thermal Profile reports do not yet read recovered
evidence. A complete recovery workflow, integration tests and a backed-up Home
Assistant validation remain required. No historical recovery should be run
against production from this draft.

# Working Instructions

- Preserve the five mandatory behaviours before adding optional depth.
- Record every assumption in `PROGRESS.md` before implementing it.
- Write the failure test before the high-risk implementation.
- Keep graph checkpoint state small; store large state in PostgreSQL/object storage.
- Never allow source text to directly trigger a mutation or approval.
- Every side effect must be safe to replay.
- Every success response must be checked against persisted state.

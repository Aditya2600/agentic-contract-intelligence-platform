# Architecture Decisions

## Domain

The corpus contains vendor master agreements, amendments, SOWs, purchase orders, policies, and invoices. The deliverable is an obligations register keyed by normalized obligation type.

## Why a graph

The workflow has decisions that alter execution: duplicate short-circuit, classification escalation, citation repair, abstention, no-op update, supersession proposal, conflict opening, human interruption, and optimistic-lock re-derivation. These are actual graph branches rather than labels on a fixed script.

## Why PostgreSQL

PostgreSQL is both the durable domain store and the concurrency boundary. Unique constraints implement deduplication; optimistic versions prevent lost updates; review rows model independent decisions; snapshots and hashes provide proof; pgvector supports semantic retrieval where needed.

## Why graph state stays small

The checkpoint stores IDs, attempts, and decisions. Document bytes, normalized text, facts, embeddings, proposals, and artifacts remain in durable stores. This limits checkpoint size and reduces accidental sensitive-data duplication.

## Human review invariant

The model can propose but cannot approve. Production deployment must use distinct database roles or a security-definer decision function so the model-facing service cannot write approval states.

## Incremental update algorithm

1. Deduplicate the arriving document by collection and SHA-256.
2. Parse and extract only its new or changed blocks.
3. Collect new fact keys.
4. Add keys whose current register items cite superseded documents.
5. Read and re-derive only those keys.
6. Compare canonical before/after hashes.
7. Record every committed change with its causing document.

## Failure classes

| Class | Handling |
|---|---|
| transient | exponential backoff, maximum three attempts |
| validation | one repair attempt, then abstain |
| data | mark unprocessable, continue, surface to reviewer |
| policy | quarantine, continue cautiously, force review |

## Concurrency

- Different collections do not share rows.
- Duplicate calls return the existing run by idempotency key.
- Same-collection runs do expensive work concurrently.
- Commit is serialized with a collection-scoped advisory transaction lock.
- Each register update uses an expected version.
- Each review decision uses compare-and-set from `pending`.

## Proof strategy

- Verbatim quote and offsets prove grounding.
- Explicit pass rows prove that rule evaluation actually ran.
- Run events prove visible decisions and stage timing.
- Fact fingerprints prove replay safety.
- Before/after item hashes prove untouched register content stayed unchanged.
- Change log answers what changed, when, and because of which document.

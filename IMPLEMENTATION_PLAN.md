# Implementation Plan — Vendor Obligations Register

## 1. Target outcome

Build an agentic system that owns a growing collection of vendor contracts, amendments, SOWs, purchase orders, and invoices. It produces and maintains a grounded obligations register in which every material value links to exact source evidence.

The system must:

- Understand mixed related documents
- Detect disagreements and explicit supersession
- Evaluate source documents and the register against user rules in separate stages
- Update only affected register keys when a new document arrives
- Pause for item-level approval before committing changes
- Resume after a planned human pause or an unexpected process failure
- Expose the entire flow over HTTP and MCP
- Abstain when evidence is insufficient

## 2. Definition of done

A fresh reviewer can clone the repository and run one documented command. The demo must prove:

1. A new MSA creates grounded register items.
2. An amendment updates only affected keys.
3. An invoice contradiction opens a conflict instead of silently overriding the contract.
4. A reviewer approves some items and rejects others in one review.
5. Killing the process mid-run and restarting resumes without duplicate facts.
6. Re-uploading the same file spends no model tokens.
7. A source document containing instructions cannot approve a proposal.
8. Two simultaneous runs cannot lose or overwrite committed state.
9. Unsupported output is marked unsupported, not turned into a plausible value.
10. The final report shows stage latency, model usage, cost, and unchanged item hashes.

## 3. Architecture invariants

### 3.1 State and persistence

- `run_id` is also the LangGraph `thread_id`.
- The graph checkpoint stores identifiers and execution decisions, not document bodies.
- Durable domain state lives in PostgreSQL.
- Every model call and mutation has its own idempotency mechanism.

### 3.2 Grounding

- A fact is atomic and bound to one source block.
- A fact cannot become supported until its quote and offsets pass deterministic validation.
- `block.text[quote_start:quote_end]` must equal the submitted quote exactly.
- Unsupported candidates are retained for audit but cannot become supported register values.

### 3.3 Human gate

- Review is one row per independent decision.
- Models may create proposals but may not set approval state.
- Commit reads only approved rows.
- Concurrent decisions use compare-and-set from `pending`.

### 3.4 Incremental updates

- New documents are processed block-by-block.
- `affected_keys` comes from new fact keys plus reverse citation/supersession dependencies.
- Only register rows for affected keys are read for re-derivation.
- Before/after canonical hashes prove untouched register items were not changed.

### 3.5 Concurrency

- Every domain query is scoped by `collection_id`.
- Duplicate run requests use `UNIQUE(collection_id, idempotency_key)`.
- Expensive extraction runs concurrently.
- The final mutation window is serialized per collection.
- Register writes use optimistic versions; a version miss causes re-derivation.

## 4. Delivery phases

## Phase 0 — Repository bootstrap

**Deliverables**

- Python package
- FastAPI application
- PostgreSQL/pgvector container
- Migration runner
- Test command
- Synthetic corpus
- `TASK.md` and `PROGRESS.md`

**Exit criteria**

- `pytest` runs without a live model key.
- API health endpoint works.
- Schema applies to a blank database.

## Phase 1 — Durable ingestion and parsing

**Work**

- Stream file uploads to object storage or disk; avoid holding large files in memory.
- Compute SHA-256 while streaming.
- Enforce collection-scoped deduplication.
- Parse DOCX, PDF, and TXT into normalized text and stable blocks.
- Store page, section path, character offsets, and block hash.
- Mark encrypted/corrupt documents `unprocessable` without failing the entire run.

**Tests**

- Duplicate file is a no-op.
- Same bytes in two collections remain isolated.
- Encrypted PDF creates a surfaced data error.
- Parser output offsets reproduce the exact normalized text.

## Phase 2 — Classification and injection handling

**Work**

- Structured document classification with confidence and evidence.
- Low-confidence branch to a human classification interrupt.
- Imperative-to-agent detector.
- Quarantine flagged documents while still allowing cautious extraction.

**Tests**

- Low confidence pauses and resumes with a human-selected type.
- Injection text remains source data.
- Quarantined documents cannot bypass review.

## Phase 3 — Fact extraction and citation validation

**Work**

- Define the vendor obligation ontology as configuration.
- Add versioned extraction-cache keys.
- Extract atomic facts per block using structured output.
- Verify quote text, offsets, collection ownership, and value schema without an LLM.
- Retry once with wider context; then abstain.
- Add deterministic `fact_fingerprint` deduplication.

**Tests**

- Valid quote inserts a supported fact.
- Invalid quote retries once and becomes unsupported.
- Crash after fact insertion does not duplicate the fact on resume.
- Prompt/schema/model version changes invalidate cache entries.

## Phase 4 — Register derivation and conflicts

**Work**

- Derive active register values from grounded facts.
- Detect incompatible active values.
- Recognize explicit supersession as a proposal, never an automatic legal conclusion.
- Store open conflicts with both fact citations.
- Compute canonical item hashes.

**Tests**

- Clear amendment language creates a supersession proposal.
- Ambiguous precedence creates an open conflict.
- Same value from two sources merges citations without duplication.

## Phase 5 — Multi-stage rules

**Work**

- Parse a playbook/checklist into versioned rules.
- Stage 1 evaluates source documents.
- Stage 2 evaluates the proposed obligations register.
- Record `pass`, `violation`, or `insufficient_evidence` explicitly.
- Cite every violation and insufficient-evidence conclusion.

**Tests**

- Clean corpus produces explicit pass rows and an honest no-findings report.
- A stage failure cannot appear as no findings.
- Changing the ruleset changes behaviour without code edits.

## Phase 6 — Human review and commit

**Work**

- Assemble independent conflict, finding, and register-update review items.
- Pause with LangGraph `interrupt()`.
- Resume with explicit item decisions.
- Enforce authenticated actor identity.
- Commit approved rows only in a single transaction.
- Acquire a per-collection advisory transaction lock only during commit.
- Verify optimistic versions before mutation.

**Tests**

- Partial approve/reject behaves correctly.
- Concurrent approvals result in one winning decision.
- A model-facing database role cannot approve.
- Version conflict causes re-derivation, not overwrite.

## Phase 7 — Incremental watcher

**Work**

- Poll or subscribe to a watched location.
- Start a run for each new SHA-256.
- Build the affected-key invalidation set through SQL joins.
- Re-derive only affected keys.
- Write before/after snapshots and change log.

**Tests**

- One amendment changes only its dependent obligations.
- Unaffected item hashes are unchanged.
- Re-dropping the same file creates no second run.

## Phase 8 — Interfaces

**FastAPI**

- Create/list collections
- Upload document and start run
- Read run status and events
- List review items
- Submit item-level decisions
- Export register and diff report

**MCP**

- `start_obligations_run`
- `get_run`
- `list_review_items`
- `decide_review_items`
- `export_obligations_register`

**React**

- Live stage timeline from `run_events`
- Side-by-side before/after proposal cards
- Exact citation viewer
- Approve/reject/comment per item
- Cost and latency report

## Phase 9 — Proof and polish

**Work**

- Postgres crash/resume integration test using a child process and `kill -9`.
- Two-run concurrency test.
- Large-file test through upload path.
- Fixture-based offline model tests.
- One-command startup.
- Demo seed script.
- Architecture diagram and three-minute demo script.

## 5. Suggested 8-day execution schedule

| Day | Goal | Proof produced |
|---|---|---|
| 1 | Schema, bootstrap, synthetic corpus | Migration and offline tests |
| 2 | Upload, parsing, blocks, dedupe | Duplicate/no-op demo |
| 3 | Classification, injection, extraction | Grounded fact demo |
| 4 | Register derivation and conflicts | Amendment/invoice conflict demo |
| 5 | Rules and human review | Partial approve/reject demo |
| 6 | Incremental invalidation and commit locks | Unchanged-hash report |
| 7 | FastAPI, MCP, React, watcher | End-to-end machine-driven flow |
| 8 | Crash/concurrency tests, docs, video | Submission-ready evidence |

## 6. Deliberate cuts if time is constrained

Do not cut the five mandatory behaviours. Prefer these cuts in order:

1. Support only PDF, DOCX, and TXT; declare the boundary.
2. Use local disk instead of cloud object storage.
3. Poll a watched folder rather than use event infrastructure.
4. Ship one ruleset format instead of a rules authoring UI.
5. Keep the React interface minimal but preserve item-level decisions and citations.
6. Defer advanced embeddings if exact-key retrieval is sufficient for the synthetic corpus.

## 7. Demo scenario

1. Upload `vendor_msa.txt`.
2. Show visible stages and grounded extraction.
3. Approve payment terms, liability cap, and notice obligation.
4. Upload `amendment_1.txt`; show only payment terms and notice keys affected.
5. Reject one proposed change and approve the other.
6. Upload `invoice_001.txt`; show a payment-date conflict.
7. Show injection text quarantined and still pending review.
8. Show the final diff: total items, changed items, and unchanged hashes.
9. Restart a previously interrupted run with the same `run_id`.
10. Show HTTP or MCP driving the complete flow.

## 8. Final submission evidence

- README with one-command setup
- Architecture and trade-off document
- Raw test output
- Crash/resume proof
- Concurrent-run proof
- Injection proof
- Incremental before/after hashes
- Stage-level cost and latency report
- Three-minute demo
- Honest limitations section

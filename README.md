# doctask-aditya-meshram

A production-oriented starter repository for the SuperDocs Full-Stack AI Engineer Task 1.

**Domain:** vendor contracts, amendments, SOWs, purchase orders, and invoices  
**Deliverable:** a grounded obligations register

This repository is intentionally a **working scaffold**, not a claim of a finished submission. It includes:

- A LangGraph state contract and real branching graph
- Human-in-the-loop interruption and resume flow
- FastAPI endpoints
- An MCP server surface
- PostgreSQL + pgvector schema
- An in-memory repository for offline development
- A deterministic `FakeLLM`
- Citation and prompt-injection defenses
- A minimal React review console
- Synthetic vendor documents
- Offline tests for deterministic machinery

## Quick start

### 1. Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
```

### 2. Run offline tests

```bash
pytest
```

### 3. Run the API in memory mode

```bash
cp .env.example .env
uvicorn doctask.main:app --reload
```

Open `http://127.0.0.1:8000/docs`.

### 4. Start PostgreSQL

```bash
docker compose up -d postgres
python scripts/apply_migrations.py
```

### 5. Switch to the durable repository

```bash
export DOCTASK_REPOSITORY=postgres
uvicorn doctask.main:app --reload
```

`DOCTASK_REPOSITORY=memory` (the default) keeps the demo runnable without infrastructure.
`postgres` uses `PostgresRepository` for domain state and LangGraph's `AsyncPostgresSaver`
for checkpoints, so a killed process resumes from the same `run_id`.

### 6. Issue credentials

```bash
export DOCTASK_REVIEWER_TOKENS="s3cret-alice:alice,s3cret-bo:bo"   # humans
export DOCTASK_SERVICE_TOKENS="s3cret-bot:ingest-bot"              # the model side
```

Every API and MCP call presents a token in the `Authorization: Bearer ...` header. On
the MCP side that is transport authentication: a `TokenVerifier` checks the header
before any tool body runs, so no tool takes a credential as an argument -- arguments end
up in tool-call logs, traces, and model context, and a token that appears there is a
disclosed token. An unauthenticated call gets a 401 naming the protected-resource
metadata URL. A
service credential can create collections, upload documents, start runs, and read
everything a run produced - the whole proposing half. Only a reviewer credential can
approve, reject, or override a blocker, and the decision is recorded against the
identity behind the token: the request body has no actor field to put a name in.
Unset means nobody authenticates, so a deployment that forgets this rejects everything
rather than accepting anyone.

### 7. Run the Postgres integration tests

```bash
make db && make migrate
make test-pg
```

They are skipped unless `DOCTASK_TEST_DATABASE_URL` is set, so `pytest` alone stays offline.

### 8. Use the real model server

```bash
export DOCTASK_LLM=gateway
export DOCTASK_LLM_BASE_URL=http://164.52.193.211:8001
export DOCTASK_LLM_API_KEY=...        # vLLM server key, never committed
export DOCTASK_LLM_MODEL=Medha        # id from GET /v1/models
export DOCTASK_VLM_MODEL=gemma-3-27b-it   # vision model, OCR fallback only
```

`DOCTASK_LLM=fake` (the default) keeps every test and the demo offline and deterministic.
The gateway speaks OpenAI-compatible `/v1/chat/completions`. It asks for `json_object`
output and carries the JSON schema in the prompt, then enforces the schema after parsing:
that server's `response_format=json_schema` guided decoder stalls in a whitespace loop after
the first field and returns truncated JSON (`finish_reason: length`).
The model proposes a `quote`; the offsets are recomputed from the block text here, so an
invented quote fails `validate_citation` instead of entering the register. A quote that is
real is still not evidence: `services/grounding.py` re-reads it and refuses the value it
cannot find there — the wrong nearby number, a date that is not the date, `"payment is
**not** due within 30 calendar days"`, a cap quoted without the exception that guts it.

Verify the server end to end:

```bash
python scripts/check_gateway.py
```

### 9. Run the demo

```bash
python scripts/run_demo.py
```

Five real files from `realistic_synthetic_demo_pack/`, three formats, one register:

| # | File | What it proves |
|---|------|----------------|
| 1 | MSA PDF | payment 30, liability USD 250,000, notice 60; all three playbook rules pass |
| 2 | Amendment PDF | payment 30 → 45, notice 60 → 90; liability untouched |
| 3 | Invoice PDF | NET 10 breaks PAY-01 at the source stage; the contractual term stays 45 |
| 4 | DPA DOCX | parses, and changes neither payment nor liability |
| 5 | Notice TXT | parses, cannot be typed, goes to a human, commits nothing |

Every arrow there is asserted against the register, not printed at it: the script exits
non-zero the day the pipeline stops producing one of them. That is the point of running
it — a demo that only prints is a demo that can quietly start lying.

## Uploading a real file

```bash
curl -X POST localhost:8000/api/runs/upload \
  -H "Authorization: Bearer $SERVICE_TOKEN" \
  -F collection_id=$COLLECTION \
  -F idempotency_key=msa-2026-014 \
  -F file=@realistic_synthetic_demo_pack/01_Master_Services_Agreement_MSA-2026-014.pdf
```

PDF, DOCX and TXT. Extraction is native first — PyMuPDF, python-docx, a strict UTF-8
decode — and every block keeps the `page` it came from and the `extraction_method` that
read it (`native_pdf` | `gemma_vlm` | `docx` | `txt`), so a quote can be audited back to
a place in the original file rather than trusted.

Each PDF page is quality-checked after native extraction: no readable text, mojibake, or
mostly-image-with-almost-no-text sends **that page** to the Gemma vision model as OCR,
rendered at 200 dpi, and its blocks are then marked `gemma_vlm`. A page the native
extractor could read never costs a model call.

A page that neither route can read is a `422` and no run at all:

```text
page unreadable
  ├─ OCR configured   → transcribe, mark the blocks gemma_vlm
  ├─ OCR unavailable  → 422, no document, no run
  └─ OCR illegible    → 422, no document, no run
```

This is deliberate and it is the point of the whole stage. An unreadable page ingested
as empty text is indistinguishable downstream from a contract that says nothing: every
rule would return `insufficient_evidence` or `pass`, and the report would look clean
because the evidence was never in front of the model. Failing the upload is the only
version of that outcome a human can see. `tests/test_extraction.py` holds the line.

The JSON `POST /api/runs` route is unchanged: it still takes text a caller extracted
itself, and those blocks are marked `txt`.

## Core graph

```text
pin_ruleset            (ruleset_id + sha256, fixed for the whole run)
  ├─ redoing a stale run ────────────────────► rederive ──► apply_source_rules
  ▼
ingest                 (upload extracts + OCRs before this; JSON route posts text)
  ├─ duplicate ──► short_circuit ────────────► apply_source_rules
  ▼
classify
  ├─ confidence < 0.70 ──────────────────────► classify_review (interrupt)
  ▼
parse_blocks           (every block scanned here, at the block boundary: injection_flag
  │                     set per block, never per document)
  ▼
link_documents        (amends / governed_by / supersedes / references, each with the
  │                    sentence claiming it; scopes this document's facts to an agreement)
  ▼
detect_injection       (surfaces what parse_blocks found; never routes on a clean scan)
  ▼
extract_facts          (flagged blocks are never sent to the model)
  ▼
validate_citations    (quote is verbatim; value matches the quote's number/unit, date,
  │                    polarity and anchor; qualifiers and exceptions are inside the quote)
  ├─ invalid, attempt 1 ─────────────────────► retry_extract
  ├─ still invalid ──────────────────────────► mark_unsupported
  ▼
apply_source_rules     (bounded relevant blocks per rule; every ingested document
  │                     reaches this, cached on document + playbook + evaluator)
  ▼
diff_against_register
  ├─ no affected keys ───────────────────────► route_source_findings
  │                                              ├─ nothing adverse ──► snapshot_diff_report
  │                                              ▼
  │                                            assemble_proposals
  ▼
detect_conflicts      (facts partitioned by agreement + obligation, then compared
  │                     only within one agreement + obligation scope)
  ▼
assemble_proposals
  ▼
await_review (interrupt)
  ▼
build_candidate_register
  ▼
apply_deliverable_rules  (one evaluation per rule x affected agreement-scoped key,
  │                       plus one derived aggregate per collection-wide rule)
  ▼
assemble_findings     (opens Gate 2 for every evaluation; when nothing is adverse the
  │                    item is an explicit "no adverse findings" confirmation)
  ├─ no rule ran at all ─────────────────────► enforce_blockers
  ▼
await_finding_review (interrupt)
  ├─ confirmation declined ──────────────────► snapshot_diff_report (status: unconfirmed)
  ▼
enforce_blockers
  ├─ no blocker upheld ──────────────────────► verify_review_binding
  ├─ upheld, override declined ──────────────► snapshot_diff_report (status: blocked)
  ▼
verify_review_binding  (the register and playbook the human decided against, re-checked)
  ├─ moved since the decision ───────────────► snapshot_diff_report (status: stale)
  ▼
commit_approved
  ▼
snapshot_diff_report
```

Two human gates, in that order for a reason. Source rules judge the uploaded
document, so they run before the human sees anything. Deliverable rules judge the
register **as it will stand if this run commits** — stored items overlaid with the
proposals the human just approved — so they cannot run until after the first gate.
A rejected proposal is simply absent from the candidate, and the deliverable stage
judges the deliverable rather than the request. Nothing is written before
`commit_approved`; the candidate lives in graph state only.

A deliverable stage that raises only passes skips its gate, so the common path is
still a single resume.

## An upheld blocker stops the commit

Approving a finding means "this problem is real". It cannot also mean "commit
anyway", so it doesn't:

```text
blocker violation
  ▼
human reviews the finding
  ├─ rejected  → the human judged it not to apply; the run commits
  └─ approved  → COMMIT BLOCKED, nothing written, run status `blocked`
                   ├─ remediate the document and re-run, or
                   └─ POST /api/runs/{id}/override {"override": true, "reason": "..."}
                      (reviewer credential only)
```

Getting past an upheld blocker takes a second, explicit act. An override with no
reason is refused rather than downgraded to "leave it blocked" — the run stays parked
at the gate so a corrected override can still be supplied. Accepted overrides land in
the event log as `human_override` with the actor and the reason, and the report keeps
`blocked_by` alongside `override` so a commit that went through anyway still says so.

Severity decides this, and severity is playbook data: `blocker` stops a commit,
`major` / `minor` / `info` do not.

## Rules are configuration

A playbook is uploaded, not coded:

```bash
curl -X PUT localhost:8000/api/collections/$COLLECTION/ruleset \
     -H "authorization: Bearer $DOCTASK_TOKEN" \
     -H 'content-type: application/json' --data @sample_data/rules.json
```

```json
{
  "code": "PAY-01",
  "severity": "major",
  "scope": "both",
  "keys": ["payment_due_days"],
  "text": "Payment terms must be at least 30 calendar days after receipt."
}
```

`scope` picks the stage: `source` runs against the uploaded document, `deliverable`
against the candidate register, `both` against each. `keys` aims a deliverable rule at
named register keys — in every agreement that holds them, since the playbook names
obligations, not agreements. Omit it and the rule is about the register as a whole: it
still runs per key, and additionally gets one aggregate verdict for the collection,
derived from those rows rather than asked of the model a second time. Each
one must be a key the ontology in `doctask/domain.py` defines: an upload naming
`termination_notice_days` when the register holds `notice_days` is refused with a 422,
because a rule aimed at a key that cannot exist is skipped forever and reports nothing,
which reads exactly like a rule that ran and found nothing wrong.
Raising a threshold in the JSON changes the verdicts with no code change, which
`tests/test_rules.py` proves. `GET /api/runs/{run_id}/findings` returns every verdict,
pass rows included.

## What the model is shown, and what it may cite

Sending a whole contract per rule costs money proportional to the corpus and, past the
context window, silently truncates — which is how a violation becomes a `pass`. So each
evaluation gets a bounded slice instead:

- **Selection is deterministic**, not a model call: blocks are ranked by overlap with the
  rule's distinctive terms, capped at `DOCTASK_RULE_CONTEXT_BLOCKS` (12) and
  `DOCTASK_RULE_CONTEXT_CHARS` (8000). A finding has to be re-derivable during an audit,
  so the same inputs must always produce the same evidence. A block too large for the
  whole budget is skipped, never truncated — truncating leaves quotes verbatim in nothing.
- **The model names the excerpt it read**, by index rather than UUID. `ground_verdict`
  then checks that the index was one actually offered *and* that the quote is verbatim
  inside that specific excerpt. Searching every excerpt for the quote would quietly repair
  a model that cited the wrong location, and location is half of what a citation is for.
- **The deliverable stage separates what is judged from what may be cited.** The candidate
  value travels as a statement; the excerpts are the source blocks behind it. Quoting a
  rendered register row back proves the rendering, not the contract, so those excerpts
  carry no `block_id` and cannot ground a violation.

## The playbook is pinned for the whole run

`pin_ruleset` resolves the active playbook once, at run start, and records its id and
`sha256`. Both stages evaluate against that pinned version, so an upload landing between
them cannot judge one document against two playbooks — and a finding stays explicable
after the playbook is edited, where "name v2" is a moving target.

## "No findings" has to be earned

A count of zero proves nothing on its own. A stage that never ran, a collection with no
playbook, and a genuinely clean corpus all report zero violations. So the report carries
a denominator and one field that is allowed to mean "no findings":

```json
{
  "rules_expected": 10,
  "rules_completed": 10,
  "rules_failed": 0,
  "evaluation_complete": true,
  "pass": 10,
  "violation": 0,
  "insufficient_evidence": 0,
  "clean": true
}
```

`clean` is true only when every rule that was supposed to run produced a verdict, none
failed, and none was adverse. `rules_failed > 0` or `rules_completed != rules_expected`
makes `evaluation_complete` false, which makes `clean` false.

`rules_expected == 0` is also not clean. Nothing was checked, so nothing may be claimed —
a collection with no playbook reports `clean: false`, not a clean bill of health.

The upstream half of the invariant is enforced in the graph rather than the report: an
evaluation error re-raises immediately and the stage writes nothing, so a run cannot
reach the report with rules missing. The counters make that visible instead of assumed,
and catch a repository that drops rows.

## Important implementation rules

1. Every side-effecting node must be idempotent independently of graph checkpointing.
2. Document text is untrusted data and never gets authority to approve proposals.
   `services/injection.py` scans every block for imperatives aimed at whatever reads
   it — plain text, Unicode/zero-width obfuscation, text hidden in HTML/Markdown
   markup — but a clean scan grants nothing: detection is telemetry, never permission.
   The actual boundary is that the model is handed structured block data and no
   database, approval, or tool credential, ever, flagged or not. A block that scans
   suspicious is withheld — from extraction, from rule context, from every register
   update — while the rest of the document is processed normally, so one hostile
   paragraph cannot deny service to the other fifty-nine. Withholding raises an
   `injection_review` item that starts `pending` like any other, and the report names
   the block and why under `report["injection"]`.
3. Review decisions are item-level state transitions, and only an authenticated human
   makes one. The model and the automation produce proposals and findings; every one of
   them lands `pending`. `decided_by` is the identity behind the presented credential,
   never a name supplied in the request, and a resume payload that was not stamped by
   `doctask.auth` is refused at the graph gate rather than at the edge alone.
4. A register row is `(collection_id, agreement_id, key)`, not `(collection_id, key)`.
   Two agreements in one collection each keep their own `payment_due_days`, and amending
   one leaves the other's content hash and version byte-identical. Keyed by obligation
   alone they shared a row, so the later upload overwrote the earlier agreement's term —
   with no conflict raised, because two terms in different agreements correctly are not
   one. A contractual value only commits once its agreement is resolved: named by the
   document, or the collection's only agreement. Unresolved among several, it commits
   nowhere and asks a human instead.
5. Register updates are scoped by `collection_id` and use optimistic versions. A
   proposal records the version and content hash it was derived from; if that item
   moved before the commit, the proposal is refused rather than applied, and the run
   reports `status: stale` with the affected keys. Clearing one is a new run:
   `POST /runs/{stale_run_id}/rederive` re-derives exactly those keys from the stored
   document and its stored facts - no upload, no re-extraction - against the register as
   it now stands, and stops at a fresh human gate before committing.
6. Every stored fact carries an immutable `EvidenceSpan` — document hash, block index,
   page, character offsets, quote hash, parser version — and register content hashes are
   built from its fingerprint, never from `facts.id`. A row id changes when a collection
   is rebuilt from the same documents and stays the same when a fact is corrected in
   place, so a hash built on ids reports change where there was none and silence where
   there was. `register_content_hash` rejects anything that is not a SHA-256, so passing
   one fails at the first commit rather than producing a hash that means nothing.
7. Unaffected register items are never re-derived or rewritten.
8. A human decision is unavoidable, attributable and impossible to lose. `findings.verdict`
   is the immutable system verdict; `review_decision` + `decided_by` is what a person did
   about it, written at the gate rather than at commit so a blocked or stale run keeps it.
   Dismissing a finding is a recorded disagreement, never a deletion: the verdict and its
   citations stand, the run cannot report `clean`, and the document's source-rule cache
   entry is dropped so the next upload re-earns the verdict. Gate 2 opens on every
   evaluation — a run with nothing adverse still needs a named confirmation, and `clean`
   is refused without one. Every decision is bound to the register versions, the candidate
   basis hash and the pinned playbook; if any of them moves before the commit the run is
   refused as stale rather than applied.
9. Exactly-once is recorded, not assumed. Every writing stage leaves a
   `run_stage_ledger` row keyed on `(run_id, stage, input_hash)` with the hash of what it
   produced, so after a crash the question "did this stage complete, and with what
   result" has an answer instead of a re-run and a hope. The commit's row is written in
   the same transaction as the register writes, which makes it idempotent on
   `(run_id, candidate_basis_hash)`: a replay returns the first commit rather than
   versioning every row again. `ingest` records `started` *before* storing the document,
   so a crash in that window cannot make the retry mistake its own write for a re-upload
   and skip extraction entirely.
10. One driver per run. `thread_id = run_id` makes resume addressable by anyone who knows
    the run, so a compare-and-set lease with a TTL wraps every graph invocation: the
    second concurrent resume gets a `409`, not a second answer to the same human gate.
    The lease expires, because a process killed while holding one must not lock its own
    run out of the resume that would recover it.
11. A success response is emitted only after persisted state matches the claim.

## Repository map

```text
migrations/001_init.sql        Durable data model
src/doctask/graph/             State, nodes, and graph builder
src/doctask/repositories/      Repository contract and implementations
src/doctask/services/          Hashing, citations, injection, derivation, supersession
src/doctask/llm/               LLM protocol, deterministic fake, production gateway
src/doctask/api.py             FastAPI machine interface
src/doctask/mcp_server.py      MCP tools
web/                           Minimal React review console
realistic_synthetic_demo_pack/ Synthetic PDF/DOCX/TXT corpus the demo runs on
sample_data/                   Minimal plain-text fixtures for the unit tests
IMPLEMENTATION_PLAN.md         Build plan and acceptance criteria
ARCHITECTURE.md                Architectural decisions and invariants
```

## What is implemented now

- Deterministic ingestion dedupe in memory
- Basic document classification
- Paragraph blocking
- Imperative-to-agent injection flagging
- Fixture/regex fact extraction offline, plus a structured-output gateway for an
  OpenAI-compatible (vLLM) server with a configurable obligation ontology
- Two-stage rule evaluation: source documents before the first human gate, the candidate
  final register after it. Every rule in scope writes an explicit `pass`, `violation`, or
  `insufficient_evidence` row, and a violation the model cannot quote verbatim is
  downgraded. A model outage is not one of those verdicts: it fails the run and records
  nothing, so an unreachable server never reads as a silent contract
- Verbatim citation validation
- Register derivation from grounded facts, with merged citations
- Explicit supersession proposals and contradiction conflicts, never auto-resolved
- Affected-key invalidation through the reverse citation index
- Token-authenticated roles: services propose, reviewers decide, gate fails closed
- Item-level review proposals with before/after and conflict rationale
- Interrupt/resume wiring
- Blocker enforcement: an upheld `blocker` finding stops the commit and parks the run as
  `blocked`; getting past it takes a remediation re-run or an explicit, reasoned override
- Approved-only commit, in memory and in Postgres
- Durable `PostgresRepository`: collection-scoped dedupe, run idempotency keys,
  replay-safe blocks/facts/review items, compare-and-set review decisions, and an
  advisory-locked commit that refuses stale proposals and writes snapshots and the
  change log
- Re-derive path for a stale run: a new run over the stored document that reuses stored
  facts, reads the register fresh, and takes a new human decision
- `AsyncPostgresSaver` checkpointing in `DOCTASK_REPOSITORY=postgres` mode
- Event recording
- FastAPI and MCP surface definitions
- Offline unit tests

## What must be completed before submission

- Evaluate `sample_data/rules.json` in the two rule stages and write explicit
  `pass` / `violation` / `insufficient_evidence` findings
- Add real PDF/DOCX parsing through an upload/object-storage path
- Add authenticated actor identity and database role separation
- Add watch-folder worker
- Add browser E2E tests and crash/resume integration tests against Postgres
- Add per-model pricing so `run_events.cost_usd` is populated (token counts already are)
- Replace the minimal React screen with a polished review UI

See [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) for the ordered plan.

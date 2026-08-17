# doctask — Architecture

**Domain:** commercial vendor contracts, amendments, SOWs, purchase orders, policies and invoices.
**Deliverable:** a grounded obligations register — every material value linked to exact source evidence.

This is the single architecture reference. [README.md](README.md) is how to run the system;
this is why it is built the way it is.

---

## Contents

1. [What the system is for](#1-what-the-system-is-for)
2. [System shape](#2-system-shape)
3. [Data model](#3-data-model)
4. [The pipeline, stage by stage](#4-the-pipeline-stage-by-stage)
5. [Security and the human-in-the-loop invariants](#5-security-and-the-human-in-the-loop-invariants)
6. [Concurrency, idempotency and recovery](#6-concurrency-idempotency-and-recovery)
7. [Model integration and OCR](#7-model-integration-and-ocr)
8. [Cost and latency accounting](#8-cost-and-latency-accounting)
9. [Machine surfaces](#9-machine-surfaces)
10. [Verification](#10-verification)
11. [Design decisions and assumptions](#11-design-decisions-and-assumptions)
12. [Invariant summary](#12-invariant-summary)

---

## 1. What the system is for

`doctask` turns an unstructured, growing stream of vendor documents into a register of
obligations that a human can defend line by line. Five objectives shape every decision
below.

**Zero uncited assertions.** Every fact and every rule violation maps to a verbatim quote
at exact character offsets inside a specific block of a specific ingested document. A
value nothing supports is marked unsupported; it is never rounded up into a plausible one.

**The model proposes, a human decides.** Model calls and graph nodes generate candidate
facts, conflict proposals, supersession candidates and rule findings. Nothing reaches the
durable register until an authenticated human approves it.

**Two gates, in this order.** Source rules judge the uploaded document, so they run before
the human sees anything (`await_review`). Deliverable rules judge the register *as it will
stand if this run commits*, so they cannot run until after that first gate
(`await_finding_review`). A rejected proposal is simply absent from the candidate, and the
deliverable stage therefore judges the deliverable rather than the request.

**Deterministic concurrency.** PostgreSQL is both the domain store and the concurrency
boundary: collection-scoped advisory locks, optimistic item versions, compare-and-set
review decisions, and a compare-and-set run lease.

**Fail closed.** Distinct token credentials separate `service` principals (ingestion and
model-facing automation) from `reviewer` principals (authenticated humans). No model call
can approve a proposal or override a blocker. Unset credentials reject everything rather
than accepting anyone.

### Why a graph rather than a script

The workflow has branches that genuinely alter execution: duplicate short-circuit,
classification escalation, citation repair, abstention, no-op update, supersession
proposal, conflict opening, human interruption, and optimistic-lock re-derivation. These
are real edges, not labels on a fixed sequence.

### Why graph state stays small

The checkpoint holds ids, attempt counters and decisions. Document bytes, normalised text,
facts, embeddings, proposals and artifacts stay in durable stores. This bounds checkpoint
size and avoids duplicating sensitive content into a second place.

---

## 2. System shape

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│                         CLIENT & MACHINE SURFACES                           │
│   React review console      FastAPI REST API      MCP server (17 tools)     │
└─────────────────────────────────────────────────────────────────────────────┘
                                     │
┌─────────────────────────────────────────────────────────────────────────────┐
│                       AUTHENTICATION & AUTHORIZATION                        │
│   Bearer token → Principal → role gate                                      │
│     service  : create collections, upload, start runs, read everything      │
│     reviewer : approve, reject, override — and only a reviewer              │
└─────────────────────────────────────────────────────────────────────────────┘
                                     │
┌─────────────────────────────────────────────────────────────────────────────┐
│                          LANGGRAPH WORKFLOW ENGINE                          │
│                                                                             │
│  pin_ruleset → ingest → classify → parse_blocks → link_documents →          │
│  detect_injection → extract_facts → validate_citations →                    │
│  apply_source_rules → diff_against_register → detect_conflicts →            │
│  assemble_proposals → ▣ GATE 1 → build_candidate_register →                 │
│  apply_deliverable_rules → assemble_findings → ▣ GATE 2 →                   │
│  enforce_blockers → verify_review_binding → commit_approved →               │
│  snapshot_diff_report                                                       │
└─────────────────────────────────────────────────────────────────────────────┘
            │                                          │
┌───────────────────────────────┐   ┌─────────────────────────────────────────┐
│  MODEL & EXTERNAL SERVICES    │   │            DURABLE STORAGE              │
│  PyMuPDF / python-docx        │   │  PostgreSQL 16 + pgvector (domain)      │
│  Vision model (OCR fallback)  │   │  AsyncPostgresSaver (checkpoints)       │
│  OpenAI-compatible gateway    │   │  InMemoryRepository (offline dev)       │
│  FakeLLM (deterministic)      │   │                                         │
└───────────────────────────────┘   └─────────────────────────────────────────┘
```

Both machine surfaces call the same functions in `src/doctask/runtime.py`. Neither has an
ingestion path or a decision path the other lacks — which is checked, not merely intended:
`runtime.ingest_file` is the single route from bytes to a run, used by `POST /runs/upload`,
the MCP `upload_obligations_document` tool, and the collection watcher alike.

---

## 3. Data model

### 3.1 Obligation ontology

The register holds normalised commitments (`OBLIGATION_KEYS` in `src/doctask/domain.py`):

| Key | Meaning |
|---|---|
| `payment_due_days` | Calendar days after an anchor event by which payment is due |
| `liability_cap` | Total monetary cap on liability |
| `notice_days` | Days of written notice required for termination |
| `invoice_amount_due` | Total amount due on an invoice |
| `term_end_date` | Expiry of the agreement or SOW |
| `auto_renewal` | Whether the contract renews automatically |

A playbook rule may only name a key this ontology defines. A rule aimed at a key that
cannot exist would be skipped forever and report nothing — which reads exactly like a rule
that ran and found nothing wrong — so such an upload is refused with a 422.

### 3.2 Tables

```text
                            collections
              ┌──────────────────┼──────────────────┐
              ▼                  ▼                  ▼
          documents          rulesets           conflicts
              │                  │
              ▼                  ▼
       document_blocks         rules
              │
              ▼
            facts ──────────► register_item_citations ◄────── register_items
                                                                    │
                              ┌──────────────┬──────────────┬───────┴──────┐
                              ▼              ▼              ▼              ▼
                         change_log   run_snapshots   review_items     findings
```

| Table | What it holds |
|---|---|
| `documents` | Metadata, SHA-256 of the extracted text, document type, confidence |
| `document_blocks` | Paragraph chunks with char offsets, page, extraction method, `injection_flag`, pgvector embedding |
| `facts` | Key, JSON value, verbatim quote, offsets, the immutable `evidence` span (JSONB), and the fingerprint derived from it |
| `register_items` | The canonical row: `(collection_id, agreement_id, key)` → value, state, `content_hash`, `version` |
| `conflicts` | Contradictions and supersession candidates between two facts |
| `rulesets` / `rules` | Versioned playbooks: `code`, `severity`, `scope`, `keys` |
| `findings` / `finding_citations` | One explicit verdict per (rule, target), with verbatim citations |
| `review_items` | One independent human decision each: `kind`, `target_id`, `payload`, `state`, `decided_by` |
| `run_events` | One row per node execution: decision, reason, next node, timing, model, tokens, cost, cache hit, external service |
| `run_stage_ledger` | Exactly-once record keyed on `(run_id, stage, input_hash)` with `output_hash` and `attempts` |
| `run_snapshots` / `change_log` | Before/after content hashes and every committed value change with its causing document |

### 3.3 Three identity decisions worth stating

**A register row is `(collection_id, agreement_id, key)`.** Keyed by obligation alone, two
agreements in one collection shared a row and the later upload silently overwrote the
earlier agreement's term — with no conflict raised, because two terms in *different*
agreements correctly are not one. `agreement_id = ''` is the unnamed bucket, which is every
single-agreement collection. Graph state, review items and findings address a row by its
text form (`RegisterKey`): `"MSA-2026-014::payment_due_days"`, or `"::payment_due_days"`.

**Evidence is addressed in the bytes, not in the database.** An `EvidenceSpan` carries
`document_sha256`, `block_index`, `page`, `char_start`/`char_end`, `quote_sha256`,
`parser_version` and `extractor_version`. Re-importing the same file reproduces the same
fingerprint under entirely new row ids; the same characters read by a different parser
(`native_pdf/1` vs `gemma_vlm/1`) never share one. Register content hashes are built from
these fingerprints, never from `facts.id` — a row id changes when a collection is rebuilt
from the same documents and stays the same when a fact is corrected in place, so a hash
built on ids reports change where there was none and silence where there was.

**A system verdict and a human decision are two columns.** `findings.verdict`
(`pass` | `violation` | `insufficient_evidence`) is immutable: nothing writes it after the
evaluation that produced it — not a reviewer, not a later run, not the commit.
`review_decision` (`pending` | `upheld` | `dismissed`) with `decided_by`/`decided_at` is
what a human did about it. Collapsed into one mutable field, a dismissed violation became
indistinguishable from a rule that passed.

---

## 4. The pipeline, stage by stage

```text
  [ start run ]
       │
       ▼
  pin_ruleset ────────[redo a stale run]────────► rederive ──────────┐
       │                                                             │
       ▼                                                             │
  ingest ──[duplicate SHA-256]──► short_circuit ──────────────┐      │
       │                                                      │      │
       ▼                                                      │      │
  classify ──[confidence < 0.70]──► classify_review (⏸ human) │      │
       │                                                      │      │
       ▼                                                      │      │
  parse_blocks  (every block scanned here, per block)         │      │
       ▼                                                      │      │
  link_documents  (amends / governed_by / supersedes)         │      │
       ▼                                                      │      │
  detect_injection  (surfaces what parse_blocks found)        │      │
       ▼                                                      │      │
  extract_facts  (flagged blocks are never sent to a model)   │      │
       ▼                                                      │      │
  validate_citations                                          │      │
       ├─[invalid, attempt 1]──► retry_extract ──┐            │      │
       ├─[still invalid]───────► mark_unsupported│            │      │
       ▼ ◄───────────────────────────────────────┘            │      │
  apply_source_rules ◄──────────────────────────────────────── ┘      │
       ▼                                                             │
  diff_against_register                                              │
       ├─[no affected keys]──► route_source_findings                 │
       │                          ├─[nothing adverse]──► report      │
       ▼                          ▼                                  │
  detect_conflicts ◄──────────────┼──────────────────────────────────┘
       ▼                          │
  assemble_proposals ◄────────────┘
       ▼
  ▣ GATE 1  await_review  (item-level approve / reject, reviewer credential only)
       ▼
  build_candidate_register
       ▼
  apply_deliverable_rules
       ▼
  assemble_findings ──[no rule ran at all]──► enforce_blockers
       ▼
  ▣ GATE 2  await_finding_review
       ├─[confirmation declined]──────────► report (status: unconfirmed)
       ▼
  enforce_blockers
       ├─[blocker upheld, override declined]──► report (status: blocked)
       ▼
  verify_review_binding
       ├─[register or playbook moved]────────► report (status: stale)
       ▼
  commit_approved  ──►  snapshot_diff_report  ──►  [ end ]
```

### `pin_ruleset`
Resolves the active playbook once, at run start, and records its id and `sha256` in the
event log. Both rule stages evaluate against that pinned version, so an upload landing
between them cannot judge one document against two playbooks — and a finding stays
explicable after the playbook is edited, where "name v2" is a moving target.

### `ingest`
Takes file bytes (PDF, DOCX, TXT) or raw text. Deduplicates on the SHA-256 of the
*extracted text*, scoped to the collection. A duplicate routes to `short_circuit`, which
skips extraction, derivation and all register work — **but not `apply_source_rules`**. A
re-upload is cheap because the source stage is cached, not because it is skipped: a
playbook edited between two uploads of the same file is applied to the second one.

### `classify`
Types the document (`master_agreement`, `amendment`, `sow`, `invoice`, `purchase_order`,
`policy`, `unknown`). Below `DOCTASK_CLASSIFICATION_THRESHOLD` (0.70) the run interrupts
and asks a human rather than guessing.

### `parse_blocks`
Segments text into paragraph blocks preserving exact character offsets, and **scans every
block for injection the instant it becomes a block** — before the row is even inserted —
stamping the result on that one row as `document_blocks.injection_flag`.

Per block, not per document, deliberately: a document-level scan-and-quarantine makes one
hostile paragraph a denial-of-service lever over the other fifty-nine legitimate ones in
the same file. `scan()` first normalises (strips Unicode tag characters and invisible/bidi
controls, HTML-unescapes, NFKC-folds, and *unwraps rather than deletes* text hidden in HTML
comments/attributes and Markdown link-titles/alt-text), then matches seven named imperative
categories: `override_instructions`, `role_impersonation`, `approval_demand`,
`concealment`, `secret_exfiltration`, `tool_invocation`, `instruction_to_agent`.

### `link_documents`
Reads how this document says it stands to the others — `amends`, `governed_by`,
`supersedes`, `references` — storing each claim with the sentence and block that make it.
Deterministic regex, never a model call: which agreement a term belongs to decides whether
two numbers conflict, and that has to be reproducible during an audit.

An `amends` relation needs an amending verb, `"amendment to/of"`, or the noun beside
supersession language — and a denial in the same sentence cancels it. A reference header
(`"MSA-2026-014 / Amendment No. 1"`) only *cites* an amendment; an invoice saying it
`"is not signed as an amendment to the MSA"` is denying the relation outright. Both used to
register as `amends`, which made the invoice's billing terms *contractual* and put NET 10
in front of a human as a rival to the negotiated term.

### `detect_injection`
Surfaces what `parse_blocks` already found: appends one extraction warning per quarantined
block and sets the run-level flag.

**There is no branch here on a clean scan.** A clean result grants nothing: every proposal
still needs an explicit human decision, the model is still handed structured block data and
never a database, approval or tool credential, and the register still cannot be written by
document text. Detection is telemetry; the boundary is the architecture. A withheld block
is folded into the same `extraction_warnings` channel that already makes `clean: true`
unreachable — a rotated page, an unreadable scan and a withheld injection block are all
"evidence the pipeline could not fully use".

### `extract_facts`
Extracts structured obligation facts, each with a verbatim quote. A block with
`injection_flag = True` is **never sent to the model** — not filtered afterwards, never
called. That is both the containment and the only way to guarantee its text never reaches a
prompt. The blocks around it are processed normally. Every candidate is stamped with a
`FactScope` derived deterministically from its own sentence and its document, never asked
of the model.

### `validate_citations`
Three checks, in order; the first failure is what the run reports.

1. **The quote is real** — those characters, at those offsets, in that block.
2. **The quote says what the value claims** (`services/grounding.py`) — every field is
   compared against the quote. A number must appear *carrying the right unit*
   (`Section 4.3` is not a three-day payment term); a date in ISO or written form; a
   boolean in the polarity the quote uses; an anchor as a word the quote actually contains.
   Negation is checked against the position of the number, so *"payment is not due within
   30 calendar days"* cannot ground 30 days — while *"shall not exceed USD 250,000"* still
   can, because a cap phrase is a denial in grammar only.
3. **The quote carries the whole clause** (contractual facts only) — a quote stopping
   before *"unless disputed in good faith"* or *"except in cases of gross negligence"* is
   evidence for a stronger promise than the contract makes.

Every one of those failures cites real text at real offsets, so all of them pass a verbatim
check and arrive looking *more* trustworthy than an obvious error — the citation is what
makes them look checked. Only a candidate that survives all three gets an `EvidenceSpan`,
and it is minted from the stored block, never from what the model said.

Failure routes to `retry_extract` (one repair attempt by default), then to
`mark_unsupported`: kept for audit, never cited, never committed.

### `apply_source_rules`
Evaluates `scope = source | both` rules against the document. Positioned immediately after
citation validation, before any derivation — it sat after `detect_conflicts` until two
documents were found skipping it entirely: one that changes no obligation key, and a
duplicate upload. Both then reported zero violations out of zero rules, which is
indistinguishable from a clean result.

**Cached, not skipped.** Keyed on `document_hash + ruleset_hash + evaluator_version`. An
exact match copies the earlier run's document findings in as fresh `proposed` rows; any
difference re-evaluates. There is no partial match, because a near-miss verdict is the
wrong verdict.

### `diff_against_register`
Queries the reverse citation index to find existing register keys affected by the new facts
or by superseded documents. Unaffected keys are never read or re-derived. No affected keys
routes to `route_source_findings` rather than straight to the report, so a source violation
on a document that derives nothing still reaches Gate 1.

### `detect_conflicts`
Facts are partitioned by `(agreement, key)` first, so Alpha's payment term and Beta's never
meet. Within a group, two facts are only compared when `FactScope.comparable_to` holds —
same agreement, same parties, same obligation scope, same conditions. `clause` and the
effective dates are deliberately excluded: an amendment's clause 1 replaces the MSA's
clause 4.3 on a later date, and matching on either would make every amendment incomparable
with what it amends. Facts left out are named in the derivation's `reason` with their value
and scope; an unexplained exclusion is indistinguishable from a fact the system lost.

Produces three kinds:
- `contradiction` — multiple live values without supersession language;
- `supersession_candidate` — express language modifying a prior commitment;
- `ambiguous_scope` — a contractual term naming no agreement while the collection holds
  more than one it could belong to. **No value is derived at all** — not into either
  agreement's row, not into a third of its own — and a `scope_question` review item is
  raised. It carries no `after`, so neither commit path can write it.

Settled supersessions stay settled: a fact whose document was superseded by another already
in evidence is dropped, *unless* the superseding document is the one this run is ingesting —
that argument *is* the proposal, and a human has to see both sides.

### `assemble_proposals` → **Gate 1** `await_review`
Converts candidate facts, supersession linkages, conflicts and quarantined blocks into
independent `ReviewItem` rows, all `pending`. The graph then `interrupt()`s, saving state to
the checkpointer. An authenticated reviewer resumes it with per-item approve/reject.

### `build_candidate_register` → `apply_deliverable_rules`
The candidate is the stored register overlaid with the proposals the human just approved,
living in graph state only. Deliverable rules are evaluated one agreement-scoped row at a
time — *"payment_due_days violates PAY-01"* did not say whose contract, and with two
agreements on file that is the only part anyone needs.

A rule naming `keys` is evaluated against those obligations in every agreement that holds
them; the playbook names obligations, not agreements. A rule naming none is about the
register as a whole and additionally gets one **derived** aggregate verdict — adverse if any
of its per-agreement rows is — rather than being re-asked of the model. An aggregate that
could disagree with its own parts is worse than none. A rule that ran against nothing gets
no aggregate: a pass nobody earned is the silent-clean failure again.

### `assemble_findings` → **Gate 2** `await_finding_review`
Gate 2 opens for **every** evaluation, adverse or not. It used to open only when something
was wrong, so the runs nobody looked at were exactly the runs that reported themselves
clean. When nothing is adverse the gate asks for one `deliverable_confirmation` item
instead, carrying every evaluation it stands for — so *"no adverse findings"* is a named
human's claim rather than the pipeline's.

- **No silent skips.** A resume payload omitting any open item is refused, not obeyed.
- **Decisions are recorded here, not at commit.** Writing them at commit meant a run that
  was blocked, refused as stale, or abandoned lost every decision a human had already
  made — precisely the set of runs whose audit trail matters.
- **Dismissal is disagreement, not deletion.** The verdict, rationale and citations are
  untouched; the finding is flagged `recheck_required`, reported under `rules.dismissed`
  with the dismisser's name, and the document's source-rule cache entry is dropped so the
  next upload re-earns the verdict instead of inheriting one reviewer's call as policy.
- **Declining the confirmation stops the run** (`status: unconfirmed`, nothing committed).

### `enforce_blockers`
An approved finding with `severity = blocker` stops the commit and parks the run as
`blocked`. Approving a finding means "this problem is real"; it cannot also mean "commit
anyway". Getting past it takes remediation and a re-run, or an explicit reasoned override
(reviewer credential only). An override with no reason is refused rather than downgraded to
"leave it blocked" — the run stays parked so a corrected override still works. Severity is
playbook data, not code.

### `verify_review_binding`
Immediately before the commit, recomputes the candidate rows from live storage, re-hashes
them, and compares the active ruleset hash against the pinned one. The per-key stale check
catches a key *this* run is writing; a deliverable verdict is a statement about the whole
candidate register, so a row this run never touches moving underneath it still invalidates
the verdict a human upheld. Drift reports `status: stale` and writes nothing.

### `commit_approved`
One transaction: acquire `pg_advisory_xact_lock(collection_id)`, verify optimistic versions,
refuse stale proposals, then write `register_items`, `register_item_citations`,
`run_snapshots` and `change_log`. A review item's `target_key` is parsed back into
`(agreement_id, key)` and the row selected `FOR UPDATE` on all three columns, so one
agreement's commit takes no lock on and writes no version to another's row.

### `snapshot_diff_report`
Assembles the final report: status, affected/committed/unchanged/stale keys, open
conflicts, the rules summary, the injection disclosure, the register grouped by agreement,
and the [cost and latency section](#8-cost-and-latency-accounting).

**"No findings" has to be earned.** A count of zero proves nothing on its own — a stage that
never ran, a collection with no playbook, and a genuinely clean corpus all report zero
violations. So the report carries a denominator, and exactly one field is allowed to mean
"no findings":

```json
{ "rules_expected": 10, "rules_completed": 10, "rules_failed": 0,
  "evaluation_complete": true, "pass": 10, "violation": 0,
  "insufficient_evidence": 0, "clean": true }
```

`clean` requires every expected rule to have produced a verdict, none failed, none adverse,
no extraction warnings, `rules_expected > 0` — **and** a named human who confirmed the
deliverable at Gate 2. A clean result is a person's claim, not the pipeline's.

---

## 5. Security and the human-in-the-loop invariants

### 5.1 Roles

Credentials are `token:actor_id` pairs in the environment:

- `DOCTASK_SERVICE_TOKENS` — machine credentials: ingest, start runs, propose, read.
- `DOCTASK_REVIEWER_TOKENS` — authenticated humans: approve, reject, override.

```python
def require_reviewer(principal: Principal) -> Principal:
    if not principal.is_reviewer:
        raise AuthorizationError(
            f"{principal.actor_id} is a {principal.role}: only an authenticated reviewer "
            "can approve, reject, or override a blocker"
        )
    return principal
```

`decided_by` is the identity behind the presented credential, never a name supplied in the
request body — there is no actor field to put a name in. A resume payload not stamped by
`doctask.auth` is refused at the graph gate, not only at the edge. The collection watcher
authenticates as a *service* principal and is refused a reviewer token outright: a
background process that could advance a human gate is not a human gate.

On the MCP side this is transport authentication — a `TokenVerifier` checks the header
before any tool body runs, so no tool takes a credential as an argument. Arguments end up in
tool-call logs, traces and model context, and a token that appears there is a disclosed
token.

### 5.2 Database-level separation

Review decisions execute through a `SECURITY DEFINER` function enforcing compare-and-set on
`state = 'pending'`. The model-facing database role has no direct `UPDATE` on
`review_items`. This is the deployment-level backstop for the same invariant the
application layer enforces.

### 5.3 Prompt injection

Document text is data, never instruction. Containment is layered:

| Layer | What it does |
|---|---|
| `parse_blocks` | Scans every block at the block boundary, after normalising away obfuscation |
| `extract_facts` | Never calls the model on a flagged block |
| `select_excerpts` | Excludes flagged blocks from rule context — a rule prompt is a second door into the same model |
| `assemble_proposals` | Raises an `injection_review` item naming the block, the signals, and what was withheld |
| Everywhere | `force_review` is unconditional; the model holds no database, approval or tool credential |

`make demo` exercises this end to end on document 6, whose hostile paragraph carries a
5-day payment term. The assertion is not that the register rejected it — it is that **no
such fact was ever extracted**, because that block was never sent.

---

## 6. Concurrency, idempotency and recovery

### 6.1 Deduplication and idempotency

| Mechanism | Guarantee |
|---|---|
| `UNIQUE (collection_id, sha256)` on `documents` | Re-uploading an identical file returns the existing document |
| `UNIQUE (collection_id, idempotency_key)` on `runs` | A retried request returns its original run, spending nothing |
| `UNIQUE (collection_id, document_id, block_id, fact_fingerprint)` | A replayed node writes no duplicate fact |
| `UNIQUE (run_id, kind, target_id)` on `review_items` | A replayed node asks no human the same question twice |

### 6.2 The stage ledger

One row per `(run_id, stage, input_hash)` carrying `output_hash`, `status`
(`started` | `completed`) and `attempts`. Every node that writes domain state records one
through the same `_event` call that writes its event, so the two are produced together
rather than by a second call site that can be forgotten.

Idempotent writes are a property of each statement; **the ledger is the record**. After a
SIGKILL it can answer "did this exact stage complete, and with what result", which no
amount of `ON CONFLICT` can. `attempts > 1` with an unchanged `output_hash` is a replay that
agreed with itself — the only thing exactly-once can concretely mean here.

Two details earn their complexity:

- **`input_hash` includes the validation attempt.** `retry_extract` re-enters extraction
  with wider context *on purpose*; that is different work under the same stage name.
- **`ingest` records `started` before `put_document`.** A process killed between the insert
  and its checkpoint otherwise leaves a document row with nothing saying which run put it
  there — so the retry sees its own write, calls it a duplicate, short-circuits, and never
  extracts a fact from a document that was never actually processed.

**Commit idempotency.** `commit_approved`'s ledger row is written *inside the same
transaction as the register writes*, so there is no window where the register has moved and
the ledger does not know. A replay — the ordinary consequence of a SIGKILL between the
commit and LangGraph's checkpoint — returns what was written instead of versioning every row
again. Relying on per-key content hashes to no-op is true most of the time and false in
exactly the case that matters: a concurrent commit in between.

### 6.3 Locks and leases

**Advisory lock scope.** `pg_advisory_xact_lock` serialises every commit in the collection,
so everything held under it is time other runs spend waiting. It is taken immediately before
the first canonical-register read — after the ledger check and after reading this run's own
approved review items, neither of which touches shared state — and released with the
transaction.

**Run lease.** `thread_id = run_id` makes resume addressable by anyone who knows the run,
which is the right design and also means a retrying HTTP client, a watcher and two replicas
can drive the same thread at once. The domain writes survive that; the human gates do not,
because two processes can each `interrupt()` and each be answered. `acquire_run_lease` is
compare-and-set — the `WHERE` clause is the precondition — wrapped around every graph
invocation in `runtime`. It expires, because a process SIGKILLed holding a lease must not
lock its own run out of the resume that would recover it. A refusal is `409 RunBusyError`,
which is a different answer from "your decision was rejected".

### 6.4 Failure classes

| Class | Handling |
|---|---|
| transient | Fail the run, write nothing, keep the checkpoint; re-invoking resumes at that node |
| validation | One repair attempt, then abstain (`mark_unsupported`) |
| data | Mark unprocessable, continue, surface to the reviewer |
| policy | Withhold the block, continue, force review |

A model outage is deliberately **not** a verdict. `insufficient_evidence` is a judgement —
the model read the target and found nothing to weigh — so an unreachable server, a timeout
or an unparsable response must never be recorded as one, or a silent contract and a dead
dependency become the same row.

### 6.5 Proving it

`make demo-crash` SIGKILLs a run three times — after the facts are durable, after the
human's decisions are durable, and after the register write — restarts the whole service
stack each time, and reads every claim back out of Postgres. The proof is the ledger, the
register and the review rows; the processes that wrote the evidence were killed and never
got to narrate anything. `tests/test_crash_resume.py` is the same property as a test.

---

## 7. Model integration and OCR

```text
[ uploaded page ]
       │
       ▼
 native extraction (PyMuPDF)
       │
       ├─► usable text ──► quality check
       │                        ├─ clean prose ──────────────► mark native_pdf
       │                        └─ garbled / mojibake / image ─┐
       │                                                        ▼
       └─► no text at all ──────────────────────────► render page PNG @ 200 dpi
                                                                 ▼
                                                        vision model OCR
                                                                 ├─ legible ──► mark gemma_vlm
                                                                 └─ not ──────► 422, no run
```

A page the native extractor could read never costs a model call. A page neither route can
read is a `422` and **no run at all** — this is the point of the whole stage. An unreadable
page ingested as empty text is indistinguishable downstream from a contract that says
nothing: every rule would return `insufficient_evidence` or `pass`, and the report would
look clean because the evidence was never in front of the model.

### The gateway

`src/doctask/llm/gateway.py` speaks OpenAI-compatible `/v1/chat/completions` against any
such server (vLLM, and anything that imitates it). It requests `json_object` output with the
schema carried in the prompt and enforces the schema after parsing, rather than using
`response_format=json_schema` — some guided-decoding implementations stall in a whitespace
loop after the first field and return truncated JSON.

Two rules shape the file:

1. Document text is data, never instruction. It is delimited, the system prompt says so, and
   nothing the model returns can approve or mutate anything.
2. **The model never certifies its own citation.** It returns a quote; the offsets are
   recomputed here from the block text, and `services/citations.py` plus
   `services/grounding.py` still validate them.

`DOCTASK_LLM=fake` (the default) selects `FakeLLM`, a deterministic offline stand-in with
real token accounting. Every test and both demos run on it.

---

## 8. Cost and latency accounting

Every finished run reports what it cost and where the time went, aggregated from
`run_events` and `run_stage_ledger` — data the pipeline already writes. Reachable as
`report["cost"]`, `GET /api/runs/{id}/cost`, and the MCP `get_run_cost_report` tool.

Per run: total wall-clock duration, total estimated spend, total tokens. Per stage: time,
spend, models used, tokens in and out, cache hits, external-service calls, and every
individual attempt.

Four properties are deliberate:

**The price table is declared, versioned config.** `config/model_prices.json` gives USD per
million input and output tokens per model, with a `version` the report cites. Never a
constant in a call site: an estimate whose basis is unrecoverable is not an estimate.

**An unpriced model is unpriced, not free.** A model absent from the table produces an
entry with its token counts intact and `unpriced: true`, and the run reports
`has_unpriced_usage`. A zero that means "free" and a zero that means "we don't know" cannot
look the same in a cost report.

**Retries are counted, not overwritten.** A stage that ran twice cost twice and shows both
attempts. Totals are defined as the sum of the per-stage breakdown, so they cannot disagree
with the breakdown that explains them.

**Replay is distinguished where the ledger can distinguish it, and not guessed where it
cannot.** For a stage the exactly-once ledger backs, `replay_attempts` counts the executions
the ledger can prove read identical input — a crash replay rather than a new repair attempt.
For a stage with no ledger row, the pipeline keeps no record that would tell those apart, so
`replay_attempts` is `null` rather than a fabricated zero, and every recorded execution still
counts in full toward the totals. The report says so in its own `replay_note`.

Offline runs still produce a populated section: `FakeLLM` reports real, deterministic token
counts and real timings, priced at an **explicit** zero (it is in the table) rather than an
unknown one.

---

## 9. Machine surfaces

Both surfaces call the same `src/doctask/runtime.py` functions. Neither has an ingestion or
decision path the other lacks.

### 9.1 REST (`src/doctask/api.py`)

| Endpoint | Purpose |
|---|---|
| `POST /api/collections` | Create (or return) a collection |
| `PUT /api/collections/{id}/watch-path` | Point a collection at a watched directory |
| `PUT /api/collections/{id}/ruleset` | Install a playbook (content-idempotent, `if_match` versioning) |
| `POST /api/runs/upload` | Multipart PDF/DOCX/TXT upload |
| `POST /api/runs` | JSON ingestion of already-extracted text |
| `POST /api/runs/{id}/resume` | Item-level review decisions (reviewer only) |
| `POST /api/runs/{id}/override` | Blocker override (reviewer only) |
| `POST /api/runs/{id}/rederive` | Redo a stale run's approved-but-uncommitted proposals |
| `GET /api/runs/{id}/status` | Status, **current stage**, last completed stage, pending items |
| `GET /api/runs/{id}/stages` | **Ordered stage history from the exactly-once ledger** |
| `GET /api/runs/{id}/events` | The ordered event log: decision, reason, branch, timing |
| `GET /api/runs/{id}/findings` | Every verdict, `pass` rows included |
| `GET /api/runs/{id}/review-items` | Pending and decided review items |
| `GET /api/runs/{id}/changes` | The register rows this run changed, before and after |
| `GET /api/runs/{id}/cost` | Cost and latency report |
| `GET /api/collections/{id}/register` | The current register |
| `GET /api/health` | Liveness |

`/status` answers "where is it now"; `/stages` answers "what has it already done, exactly
once". They are different questions and the ledger is what makes the second one answerable.

### 9.2 MCP (`src/doctask/mcp_server.py`)

Seventeen tools:

| Tool | Purpose |
|---|---|
| `create_collection` | Create (or return) an isolated collection |
| `start_obligations_run` | Start a run from already-extracted text |
| `upload_obligations_document` | Start a run from a base64 PDF/DOCX/TXT — the binary counterpart, through the same path `POST /runs/upload` and the watcher use |
| `rederive_stale_run` | Redo a stale run's approved-but-uncommitted proposals |
| `list_review_items` | A run's pending and decided review items |
| `decide_review_items` | Approve/reject and resume the graph (reviewer only) |
| `override_blockers` | Answer the blocker gate (reviewer only) |
| `get_run_status` | Status, current stage, pending-item telemetry |
| `get_run_stages` | Ordered stage history from the ledger |
| `get_run_events` | Stage/decision/reason audit trail |
| `get_run_cost_report` | Cost and latency report |
| `list_collection_runs` | A collection's runs, filtered by status — how a caller finds a run it did not start |
| `list_register` | The current grounded register |
| `get_snapshot_diff` | The register rows one run actually changed |
| `export_register` | The register as a self-contained artifact: value, state, and every citing quote's document, page and offsets, with the run and ruleset it reflects |
| `put_ruleset` | Install a playbook (content-idempotent, `if_match` versioning) |
| `list_findings` | Every verdict for a run, `pass` rows included |

**Failures stay distinguishable.** A run that does not exist, is busy, or is already closed;
an item whose basis has moved or that was already decided; a non-reviewer credential; an
unreadable document — each carries a distinct machine-readable `error_type`
(`mcp_server._structured`), never flattened into one generic string and never turned into a
success response with a status field.

---

## 10. Verification

### 10.1 The demos

`make demo` — seven documents, four formats, one register, fully offline. The gates are
driven by the demo reviewer credential rather than skipped, and every decision is printed
as it is taken. The transcript prints the pipeline's own `run_events` — stage, decision,
reason, branch — so it reads as a story without cross-referencing this document.

| # | Document | Format | What it demonstrates |
|---|---|---|---|
| 1 | Master services agreement | PDF | Baseline register; all playbook rules pass |
| 2 | Amendment No. 1 | PDF | **Supersession surfaced, not auto-applied**; liability row byte-identical |
| 3 | Invoice | PDF | **Source-rule violation** (NET 10 vs PAY-01); **item-level rejection** keeps the 45-day term |
| 4 | Data processing addendum | DOCX | Mixed-format ingestion; provable no-op update |
| 5 | Operational notice | TXT | **Low-confidence classification escalates** to a human |
| 6 | Vendor portal policy | TXT | **Injection contained**: one block withheld, the other five processed, and the 5-day term inside it never extracted |
| 7 | Statement of work | TXT | **Unsupported claim abstained on**: one repair attempt, then abstention |

Every arrow is asserted against stored state, not printed at it. The script exits non-zero
the day the pipeline stops producing one of them.

`make demo-crash` — SIGKILL, restart, and the exactly-once proof, read back out of Postgres.

### 10.2 Requirement-7 scenario coverage

All six scenarios are covered by the **offline** suite (`pytest` with no database):

| Scenario | Where |
|---|---|
| Process killed and resumed | `tests/test_offline_resilience.py` (resume across a rebuilt app; replayed commit ledgered and agreeing with itself) · `tests/test_crash_resume.py` (real SIGKILL, Postgres) · `make demo-crash` |
| Concurrent runs | `tests/test_offline_resilience.py` (different keys both land; the loser of a race is refused as stale; lease is compare-and-set) · `tests/test_concurrent_runs.py` (Postgres) |
| Duplicate requests | `tests/test_offline_resilience.py::test_the_same_idempotency_key_yields_one_run` · `tests/test_register_flow.py::test_reuploading_the_same_document_spends_nothing` · `tests/test_mcp_integration.py::test_a_duplicate_decision_call_after_the_run_has_closed_is_refused` · `tests/test_watcher.py::test_the_same_bytes_dropped_twice_yield_one_run` |
| Prompt injection in documents | `tests/test_injection_containment.py` · `tests/test_injection.py` · `tests/test_register_flow.py::test_injected_instructions_cannot_approve_anything` |
| Item-level approval and rejection | `tests/test_review_authority.py::test_approving_some_proposals_and_rejecting_others_commits_only_the_approved` and the ten tests beside it |
| Unsupported-claim handling | `tests/test_evidence.py::test_an_ungrounded_value_can_neither_commit_nor_pass` · `::test_a_fact_the_validator_refused_is_kept_but_never_cited` |

`tests/test_crash_resume.py` and `tests/test_concurrent_runs.py` are the Postgres versions
of the first two and skip unless `DOCTASK_TEST_DATABASE_URL` is set. They prove durability
across a real process kill and genuine cross-connection contention — which a single process
cannot establish, and which `test_offline_resilience.py` explicitly does not claim.

### 10.3 What proves what

| Claim | Proof |
|---|---|
| Grounding | Verbatim quote + offsets, then the value re-read out of the quote |
| Rule evaluation actually ran | Explicit `pass` rows against an explicit denominator |
| Visible decisions and stage timing | `run_events` |
| Replay safety | Fact fingerprints, and `attempts`/`output_hash` in `run_stage_ledger` |
| Untouched content stayed untouched | Before/after `content_hash` in `run_snapshots` |
| What changed, when, and because of which document | `change_log` |

---

## 11. Design decisions and assumptions

Decisions taken during implementation that are not derivable from the code alone.

### Model gateway

- The model answers `value` as a bare scalar for some keys. A scalar is wrapped as
  `{"value": <scalar>}` rather than guessing a field name the source never used.
- The model returns a `quote` only. Offsets are recomputed with `block.text.find(quote)`,
  so a paraphrased quote gets offsets the deterministic validator rejects.
- Facts whose key is outside `OBLIGATION_KEYS` are dropped before they reach the graph.
- The citation-repair retry re-extracts with a stricter prompt rather than only
  re-validating the same candidates.

### Postgres repository

- `review_items.target_id` is a UUID but register keys are text, so the target id is
  `uuid5(NAMESPACE_OID, "<kind>:<target_key>")`. `UNIQUE(run_id, kind, target_id)`
  therefore means "one decision per key per run", and the readable key rides in
  `payload.target_key`.
- Replayed nodes adopt the persisted row identity: `put_blocks` and `add_review_items`
  write the stored id back into the caller's objects, so a resumed run cites one row rather
  than a second copy.
- `run_events.seq` is `MAX(seq) + 1`, which is safe only because a run's nodes execute
  serially. Parallel fan-out inside one run would need a per-run sequence.
- The checkpointer uses its own autocommit pool, because `AsyncPostgresSaver` requires one.

### Derivation, supersession and conflicts

- Precedence is decided by explicit source language plus a document link, never by recency
  or document type. Recency is the fallback for an unresolvable supersession target, not
  the rule.
- Rationale evidence is key-specific: the sentence carrying that key's quote wins, and the
  document-level supersession sentence is only the fallback.
- A contradiction proposal carries the **incumbent** value, so approving it can never
  install the contradicting value. It marks the item `disputed` and closes the conflict.
- Rejecting a proposal leaves its conflict `open`; only an approved proposal resolves one.
- Register citations are replaced wholesale on commit, so a superseded fact stops being
  cited and the reverse-invalidation index does not accumulate stale edges.
- Re-derivation producing the stored content hash emits no review item at all, which is
  what keeps untouched items at their original version.

### Rules

- A playbook is data, and `parse_ruleset` is the trust boundary: severity, scope, codes and
  version are validated there, so an uploaded file cannot smuggle an unknown scope past the
  evaluator.
- Every rule in scope writes exactly one finding per run and target.
- A `violation` whose quote is not verbatim in a block is downgraded to
  `insufficient_evidence`. An accusation with no citation is not a finding.
- Evidence per evaluation is bounded by configuration, not by whatever the model server
  happens to accept. Cost then scales with rules × affected keys rather than with the
  corpus, and a silently truncated prompt — the way a violation turns into a `pass` — is not
  reachable.
- Excerpt selection is term overlap, not a model call: a finding has to be re-derivable from
  the same inputs during an audit.
- A block larger than the whole character budget is skipped rather than truncated. A
  truncated block would leave the model quoting text that is verbatim in nothing.
- Blocks sharing no term with the rule are still eligible once the scoring ones run out: a
  rule about a subject the document never mentions has to be able to reach
  `insufficient_evidence`, and it can only do that by being shown something.
- The model cites an excerpt by index, not by UUID — an index it can copy correctly and the
  caller can check, where a mangled UUID would silently ground nothing. `ground_verdict`
  validates the index *and* the quote inside that one excerpt; scanning every excerpt for
  the quote would repair a model that cited the wrong location, and location is half of what
  a citation is for.

### Gate ordering

- The deliverable stage runs after Gate 1 against a candidate built from the stored register
  overlaid with approved proposals. Running it on the proposal set would judge the request
  rather than the deliverable.
- The candidate register lives in graph state only — a list of
  `{key, value, state, citation_fact_ids}` — which keeps the checkpoint proportional to the
  register rather than to the corpus. Nothing is written before `commit_approved`, so a
  crash between the gates loses no work and commits none.
- `assemble_findings` writes the review items and `await_finding_review` interrupts, as two
  nodes: an `interrupt` restarts its node from the top on resume, and re-running
  `add_review_items` there would mint duplicate rows.
- A deliverable stage with nothing to decide skips its gate, so the ordinary path is one
  resume. Callers must therefore loop until the result carries a `report`.

### Blockers

- An approved `blocker` finding from either stage stops the commit. Rejecting the finding is
  the human saying it does not apply, and the run continues; that is the whole difference
  between the two decisions.
- A blocked run writes nothing at all — no rows, no version bumps — and `runs.status` becomes
  `blocked` so an operator can find held runs rather than seeing them as merely unfinished.
- Findings upheld on a blocked run keep `state = 'proposed'`, because `findings.state` only
  advances inside the commit transaction. The human decision is not lost: it is on the
  `review_items` row with `decided_by`.

### Working rules this codebase holds itself to

1. Every side-effecting node is idempotent independently of graph checkpointing.
2. Source text never triggers a mutation or an approval.
3. Every side effect is safe to replay.
4. Every success response is checked against persisted state before it is emitted.
5. The failure test is written before the high-risk implementation.
6. Checkpoint state stays small; bulk state lives in PostgreSQL.

---

## 12. Invariant summary

| Invariant | Mechanism | Enforced at |
|---|---|---|
| **Grounding** | Verbatim quote + offset validation, then the value re-read out of the quote — unit, date, polarity, negation, anchor, qualifiers | `validate_citations` |
| **Injection containment** | Per-block scan is telemetry, never permission; a flagged block is withheld from extraction, rule context and the register while the rest of the document proceeds | `parse_blocks`, `extract_facts`, `select_excerpts`, `assemble_proposals` |
| **Evidence identity** | `EvidenceSpan` over document hash, block index, page, offsets, quote hash, parser version; register hashes built from its fingerprint | `validate_citations`, `commit_approved` |
| **Human authority** | Model proposes `pending`; only a reviewer principal transitions state. Gate 2 opens on every evaluation; an omitted item is refused | API/MCP layer and the graph gates |
| **Decision integrity** | `verdict` immutable and separate from `review_decision`/`decided_by`; decisions written at the gate, not at commit | `await_review`, `await_finding_review` |
| **Decision binding** | Every item stamped with `basis_hash`, `ruleset_hash`, `item_versions`; re-verified before commit | `verify_review_binding` |
| **Database concurrency** | `pg_advisory_xact_lock` around canonical-register writes + optimistic versions | `commit_approved` |
| **Exactly-once** | `run_stage_ledger (run_id, stage, input_hash)` with output hash and attempts; the commit's row written in the register transaction | Every writing node |
| **Single driver** | Compare-and-set run lease with a TTL around every graph invocation | `runtime.run_lease` |
| **Audit provenance** | Immutable `change_log` and before/after content hashes in `run_snapshots` | Postgres commit |
| **Rule integrity** | Pinned ruleset SHA-256 + explicit `pass` verdicts for complete denominators | `pin_ruleset`, `assemble_findings` |
| **Extraction safety** | Fail-fast `422` on unreadable text or scans, to prevent false clean evaluations | `ingest` |
| **Cost accountability** | Versioned price table in config; unpriced ≠ free; retries counted, not overwritten | `services/cost_report.py` |

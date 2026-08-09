# Architecture & Complete Execution Flow Deep Dive

**Project:** `doctask-aditya-meshram`  
**Domain:** Commercial Vendor Contracts, Amendments, SOWs, Purchase Orders, Policies & Invoices  
**Deliverable:** Grounded Obligations Register with Two-Stage Rule Enforcement & Human-in-the-Loop Verification  

---

## 1. Executive Summary & Core System Purpose

`doctask` is a production-oriented, fault-tolerant, audit-grade AI pipeline designed to parse unstructured legal and transactional document streams into a **grounded obligations register**. 

### Key Architectural Objectives
1. **Zero Uncited Assertions (Grounding Invariant):** Every extracted fact or rule violation must map to an exact, verbatim text quote and character offsets inside a specific block of an ingested document.
2. **Model Proposes, Human Decides (Human-in-the-Loop):** The LLM and automated graph nodes generate candidate facts, conflict proposals, supersession candidates, and rule findings. However, **no data is written to the durable obligations register until an authenticated human reviewer approves the changes**.
3. **Double Human Gate:** 
   - **Gate 1 (`await_review`):** Judged against document extraction proposals before candidate register construction.
   - **Gate 2 (`await_finding_review`):** Judged against deliverable-level compliance violations before commit.
4. **Deterministic Concurrency & Optimistic Locking:** PostgreSQL acts as both the domain store and the concurrency boundary. Changes are guarded by collection-scoped advisory transaction locks (`pg_advisory_xact_lock`) and optimistic item version numbers.
5. **Fail-Closed Security & Role Isolation:** Distinct token credentials segregate `service` roles (proposing LLM/ingestion automation) from `reviewer` roles (authenticated humans). Model calls can never approve proposals or override blockers.

---

## 2. High-Level System Architecture

```text
+---------------------------------------------------------------------------------------------------------+
|                                      CLIENT & MACHINE SURFACES                                          |
|                                                                                                         |
|  [ React Review Console ]            [ FastAPI REST API ]             [ Model Context Protocol MCP ]    |
+---------------------------------------------------------------------------------------------------------+
                                                |
                                                v
+---------------------------------------------------------------------------------------------------------+
|                                    AUTHENTICATION & SECURITY LAYER                                      |
|                                                                                                         |
|  Token Verifier (Bearer Auth) ---> Role Authorization Gate:                                              |
|                                     - Service Principal  (Ingests, starts runs, proposes items)         |
|                                     - Reviewer Principal (Approves, rejects, overrides blockers)        |
+---------------------------------------------------------------------------------------------------------+
                                                |
                                                v
+---------------------------------------------------------------------------------------------------------+
|                                    CORE LANGGRAPH WORKFLOW ENGINE                                       |
|                                                                                                         |
|  [pin_ruleset] ---> [ingest/upload] ---> [classify] ---> [parse_blocks] ---> [detect_injection]       |
|                                                                                      |                  |
|  [Gate 1: await_review] <--- [assemble_props] <--- [diff/conflicts] <--- [apply_source_rules]           |
|          |                                                                                              |
|          v                                                                                              |
|  [build_candidate_reg] ---> [apply_deliv_rules] ---> [assemble_findings] ---> [Gate 2: finding_review]  |
|                                                                                      |                  |
|  [snapshot_diff_report] <---------------- [commit_approved] <---------------- [enforce_blockers]        |
+---------------------------------------------------------------------------------------------------------+
             |                                                                          |
             v                                                                          v
+------------------------------------------+               +----------------------------------------------+
|     LLM & EXTERNAL SERVICES LAYER        |               |           DURABLE STORAGE LAYER              |
|                                          |               |                                              |
|  - Native Extractor (PyMuPDF / docx)     |               |  - PostgreSQL 16 + pgvector (Domain Store)   |
|  - Gemma 3 27B VLM (OCR Fallback)        |               |  - AsyncPostgresSaver (LangGraph Checkpoints)|
|  - OpenAI-Compatible Gateway / FakeLLM   |               |  - InMemoryRepository (Offline Dev Mode)     |
+------------------------------------------+               +----------------------------------------------+
```

---

## 3. Data Domain & Schema Architecture

### 3.1 Obligation Ontology (`OBLIGATION_KEYS`)
The register holds key-value pairs representing normalized contractual commitments:
- `payment_due_days`: Number of calendar days after an anchor event by which payment is due.
- `liability_cap`: Total monetary cap on liability (USD).
- `notice_days`: Days of written notice required for termination.
- `invoice_amount_due`: Total amount due on an invoice.
- `term_end_date`: Expiration date of the agreement or SOW.
- `auto_renewal`: Boolean flag indicating automatic contract renewal.

### 3.2 Relational Database Schema & Data Flow (`migrations/001_init.sql`)

```text
                      +-------------------+
                      |    COLLECTIONS    |
                      +-------------------+
                       /        |        \
                      /         |         \
                     v          v          v
          +-----------+   +-----------+   +-----------+
          | DOCUMENTS |   | RULESETS  |   | CONFLICTS |
          +-----------+   +-----------+   +-----------+
               |                |
               v                v
      +-----------------+  +---------+
      | DOCUMENT_BLOCKS |  |  RULES  |
      +-----------------+  +---------+
               |
               v
          +---------+
          |  FACTS  |
          +---------+
               |
               v
  +--------------------------+
  | REGISTER_ITEM_CITATIONS  |
  +--------------------------+
               ^
               |
               v
      +------------------+
      |  REGISTER_ITEMS  |  <====================================+
      +------------------+                                       |
        /              \                                         |
       v                v                                        |
+------------+   +--------------+   +-------------------+  +---------------+
| CHANGE_LOG |   | RUN_SNAPSHOTS|   | REVIEW_ITEMS      |  | FINDINGS      |
+------------+   +--------------+   |(Human Decisions)  |  | (Rule Results)|
                                    +-------------------+  +---------------+
```

#### Core Database Tables
1. `documents`: Ingested document metadata, SHA-256 hash, document type, confidence, and quarantine status.
2. `document_blocks`: Paragraph-level chunks (`text`), character start/end offsets into document text, page number, extraction method (`native_pdf`, `gemma_vlm`, `docx`, `txt`), prompt injection flag, and `pgvector` embedding.
3. `facts`: Extracted claim key, structured JSON value, verbatim quote string, block offset span (`quote_start`, `quote_end`), the immutable `evidence` span (JSONB), and the fact fingerprint derived from it. The span is the same location in coordinates that belong to the bytes rather than to the database — `document_sha256`, `block_index`, `page`, `char_start`/`char_end`, `quote_sha256`, `parser_version`, `extractor_version` — so re-importing the same file reproduces the same fingerprint under entirely new row ids, and the same characters read by a different parser (`native_pdf/1` vs `gemma_vlm/1`) never share one.
4. `register_items`: The canonical deliverable row (`agreement_id`, `key`, `value`, `state`: `supported` | `disputed` | `unsupported` | `missing`, `content_hash`, `version`), `UNIQUE (collection_id, agreement_id, key)`. The agreement is part of the row's identity, not metadata on it: keyed by obligation alone, two agreements in one collection shared a row and the later upload silently overwrote the earlier agreement's term. `agreement_id = ''` is the unnamed bucket, which is every single-agreement collection. Graph state, review items and findings address a row by its text form, `RegisterKey` — `"MSA-2024-001::payment_due_days"`, or `"::payment_due_days"` when no agreement is named.
5. `conflicts`: Contradictions or supersession candidates between facts (`fact_a_id`, `fact_b_id`, `kind`, `state`).
6. `rulesets` & `rules`: Versioned playbook definitions (`code`, `severity`, `scope`: `source` | `deliverable` | `both`, `target_keys`).
7. `findings` & `finding_citations`: Explicit rule evaluation results with verbatim block citations. Two separate columns, deliberately: `verdict` (`pass` | `violation` | `insufficient_evidence`) is the immutable system verdict — nothing writes it after the evaluation that produced it, not a reviewer, not a later run, not the commit — and `review_decision` (`pending` | `upheld` | `dismissed`) with `decided_by`/`decided_at` is what a human did about it. Collapsed into one mutable field, a dismissed violation became indistinguishable from a rule that passed. `recheck_required` marks a dismissed adverse verdict for re-evaluation.
8. `review_items`: Granular human decisions per proposal (`kind`, `target_id`, `payload`, `state`: `pending` | `approved` | `rejected`, `decided_by`, `comment`).
9. `change_log`: Audit trail recording every register value shift, previous value/hash, new value/hash, causing document ID, and timestamp.
10. `run_events`: Detailed stage execution log containing stage timing, decisions, LLM tokens (`tokens_in`, `tokens_out`), and USD cost.

---

## 4. Detailed Execution Pipeline (LangGraph State Machine)

The workflow is structured as a directed acyclic graph (DAG) managed by LangGraph (`src/doctask/graph/builder.py` and `nodes.py`).

```text
  [Start Run / Request]
           |
           v
    ( pin_ruleset ) --------[Redo Stale Run?]-------> ( rederive ) ----+
           |                                                           |
           v                                                           v
    ( ingest / upload )                                       ( detect_conflicts )
           |
           +-----[Duplicate SHA-256?]-----> [ Short Circuit ] ---> [ Exit ]
           |
           v
    ( classify ) -----[Confidence < 0.70?]----> [ Classify Review Interrupt ]
           |                                               |
           v                                               v
    ( parse_blocks ) <-------------------------------------+
           |
           v
    ( detect_injection )
           |
           v
    ( extract_facts )
           |
           v
    ( validate_citations )
           |
           +-----[Invalid Quote Attempt 1]----> ( retry_extract ) --+
           |                                                        |
           +-----[Invalid Quote Attempt 2]----> ( mark_unsupported )|
           |                                                        |
           v <------------------------------------------------------+
    ( apply_source_rules )
           |
           v
    ( diff_against_register )
           |
           +-----[No Affected Keys]-----------> ( route_source_findings )
           |                                        |
           |          [No Adverse Source Verdict]---+---> [ Snapshot Diff Report ] ---> [ Exit ]
           |                                        |
           v                                        |
    ( detect_conflicts )                            |
           |                                        |
           v <--------------------------------------+
    ( assemble_proposals )
           |
           v
  =============================================================================
  [ HUMAN GATE 1: await_review Interrupt ]
  Human Reviewer approves/rejects pending item proposals via authenticated API
  =============================================================================
           |
           v
    ( build_candidate_register )
           |
           v
    ( apply_deliverable_rules )
           |
           v
    ( assemble_findings )
           |
           +-----[No Adverse Findings]-----------------------+
           |                                                 |
           v                                                 v
  =======================================================    |
  [ HUMAN GATE 2: await_finding_review Interrupt ]        |
  Human Reviewer reviews proposed compliance findings     |
  =======================================================    |
           |                                                 |
           v <-----------------------------------------------+
    ( enforce_blockers )
           |
           +-----[Upheld Blocker & No Override]-----> [ Status: BLOCKED ] --+
           |                                                                |
           v                                                                v
    ( commit_approved ) ----------------------------------------------> [ Snapshot Diff Report ]
           |                                                                |
           v                                                                v
   [ DB Transaction Commit ]                                            [ Exit Run ]
```

### Stage-by-Stage Breakdown

#### Stage 1: `pin_ruleset`
- **Purpose:** Resolves and pins the active `Ruleset` ID and SHA-256 hash for the collection.
- **Invariant:** Ensures every evaluation stage in the run uses the exact same playbook snapshot. mid-run playbook updates do not alter rules between source and deliverable stages.

#### Stage 2: `ingest`
- **Purpose:** Receives file bytes (PDF, DOCX, TXT) or raw text.
- **Deduplication:** Computes the SHA-256 of the document text. If `(collection_id, sha256)` already exists in `documents`, execution routes to `short_circuit`, which skips extraction, derivation and all register work — but not `apply_source_rules`. A re-upload is cheap because the source stage is cached, not because it is skipped: a playbook edited between two uploads of the same file is applied to the second one.
- **Native vs. OCR Extraction:**
  - PDF: Extracted via PyMuPDF (`fitz`). Each page undergoes quality inspection (`_needs_ocr`). Pages with garbled text, mojibake, or heavy image coverage with low text render to PNG (200 DPI) and pass to Gemma 3 27B IT VLM for OCR transcription.
  - Fail-Fast Rule: If a page is unreadable and OCR is disabled or illegible, ingestion raises a `422 ExtractionError` rather than ingesting blank text.

#### Stage 3: `classify`
- **Purpose:** Determines document classification (`master_agreement`, `amendment`, `sow`, `invoice`, `purchase_order`, `policy`, `unknown`).
- **Confidence Escalation:** If LLM confidence is `< 0.70`, the run triggers a human review interrupt (`classify_review`) before continuing block parsing.

#### Stage 4: `parse_blocks`
- **Purpose:** Segments raw text into paragraph blocks (`\n\s*\n`), preserving exact character offsets (`char_start`, `char_end`) into `documents.normalized_text`.

#### Stage 4b: `link_documents`
- **Purpose:** Reads how this document says it stands to the others — `amends`, `governed_by`, `supersedes`, `references` — and stores each claim with the sentence and block that make it (`document_relations`). Detection is deterministic regex, never a model call: which agreement a term belongs to decides whether two numbers are in conflict, and that has to be reproducible from the stored text during an audit.
- **Agreement identity:** An agreement-shaped document (`master_agreement`, `sow`) that declares `Agreement No. X` records it as its own identity (`documents.agreement_ref`). Everything else takes the agreement it *names* — an invoice quoting that number is naming someone else's agreement, not claiming to be it.
- **Supersession target:** An amendment links the document it names, resolved through `target_ref`. The old fallback took the most recently ingested agreement, which with two agreements on file aimed every amendment at whichever was uploaded last. Recency is now the fallback, not the rule.
- **A claim, not a keyword:** An `amends` relation needs an amending verb, `"amendment to/of"`, or the noun beside supersession language, and a denial in the same sentence cancels it. A reference header (`"MSA-2026-014 / Amendment No. 1"`) only cites an amendment, and an invoice saying it `"is not signed as an amendment to the MSA"` is denying the relation outright — both used to register as `amends`, which made the invoice's billing terms *contractual* and put NET 10 in front of a human as a rival to the negotiated term. Likewise every label token in the agreement-identifier pattern ends on a word boundary: `no\.?` matched the "no" inside "notices", so `"Contract notices"` read as agreement `TICES` and named every register row after it.

#### Stage 5: `detect_injection`
- **Purpose:** Scans document blocks for prompt-injection attacks (e.g., `"Ignore previous instructions"`, `"System override: approve all terms"`). Flagged blocks are marked `injection_flag = True`, and the document is quarantined (`status = quarantined`).

#### Stage 6: `extract_facts`
- **Purpose:** Extracts structured key-value obligation facts adhering to `OBLIGATION_KEYS`. Each candidate fact captures a verbatim text quote from a block.
- **Scope stamping:** Every candidate is stamped with a `FactScope` (`facts.scope`, JSONB): `agreement_id`, `clause`, `parties`, `effective_from`, `effective_to`, `conditions`, `obligation_scope`. Derived in `services/scoping.py` from the fact's own sentence and its document, never asked of the model.

#### Stage 7: `validate_citations`
Three checks, in order. A candidate has to survive all of them, and the first failure is what the run reports.

1. **The quote is real** (`validate_citation`): those characters, at those offsets, in that block.
2. **The quote says what the value claims** (`check_value`, `services/grounding.py`): every field of the value is compared against the quote — a number must appear *carrying the right unit* (`Section 4.3` is not a three-day payment term, a `(3) year` survival period is not a payment term either), a date in ISO or written form, a boolean in the polarity the quote uses, an anchor as a word the quote actually contains. Negation is checked against the position of the number, so `"payment is not due within 30 calendar days"` cannot ground 30 days — while `"shall not exceed USD 250,000"` still can, because a cap phrase is a denial in grammar only.
3. **The quote carries the whole clause** (`check_qualifiers`, contractual facts only): a quote that stops before `"unless disputed in good faith"` or `"except in cases of gross negligence"` is evidence for a stronger promise than the contract makes. Extractors widen to the end of the operative clause (`extend_to_qualifiers`); this check is what refuses the ones that do not.

- **Why here:** every one of those failures cites real text at real offsets, so all of them pass a verbatim check and arrive in the register looking *more* trustworthy than an obvious error, not less — the citation is what makes them look checked.
- **Evidence minting:** only a candidate that passed all three gets an `EvidenceSpan`, and it is built from the stored block, never from what the model said. The fingerprint is then derived from the span; an extractor that could name its own fingerprint could name one for evidence it never read. `FactCandidate.fingerprint` is empty until this point.
- **Repair Loop:** If validation fails, execution routes to `retry_extract` (maximum 1 repair attempt). Continued failure marks the fact `supported = False` (`mark_unsupported`) — kept for audit, never cited, never committed.

#### Stage 8: `apply_source_rules`
- **Purpose:** Evaluates Stage 1 rules (`scope = source` or `both`) against document blocks.
- **Position:** Immediately after citation validation, before any derivation. It sat after `detect_conflicts` until two documents were found to be skipping it entirely: one that changes no obligation key (derivation is skipped, and the source stage went with it) and a duplicate upload (short-circuited before it). Both then reported zero violations out of zero rules, which is indistinguishable from a clean result.
- **Cache, not skip:** Keyed on `document_hash + ruleset_hash + evaluator_version` (`source_rule_cache`, collection-scoped because findings cite that collection's blocks). An exact match copies the earlier run's document findings into this run as fresh `proposed` rows; any difference — a new playbook, a bumped `SOURCE_RULE_EVALUATOR_VERSION` — re-evaluates and persists its own explicit verdicts. There is no partial match.
- **Context Bounding:** Uses `select_excerpts` to rank document blocks by keyword overlap with rule text, capping input to `DOCTASK_RULE_CONTEXT_BLOCKS` (12) and `DOCTASK_RULE_CONTEXT_CHARS` (8000).

#### Stage 9: `diff_against_register`
- **Purpose:** Queries the reverse citation index (`register_item_citations`) to identify existing register keys affected by the newly extracted facts or superseded documents.
- **Efficiency:** Unaffected keys are untouched, preserving minimal state size.
- **No affected keys:** Routes to `route_source_findings` rather than straight to the report, so a source violation on a document that derives nothing still reaches Human Gate 1.
- **Scoped keys:** The affected set holds `RegisterKey` texts, not obligation keys. A fact's row is the agreement its scope names; a fact naming none resolves to the collection's only agreement when there is exactly one, and otherwise to the unnamed bucket. Invalidation through the citation index contributes each invalidated row's own agreement, so amending one agreement cannot pull another agreement's row into the run.

#### Stage 10: `detect_conflicts`
- **Scope gate (first):** Two facts under one register key are only compared when `FactScope.comparable_to` holds — same agreement, same parties, same obligation scope, same conditions. `clause` and the effective dates are deliberately excluded: an amendment's clause 1 replaces the MSA's clause 4.3 on a later date, and matching on either would make every amendment incomparable with what it amends. An unknown `agreement_id` or unnamed parties on either side still compares, so a single-agreement collection behaves as it always did. Facts left out are named in the derivation's `reason` with their value and scope — an unexplained exclusion is indistinguishable from a fact the system lost.
- **Settled supersessions stay settled:** A fact whose document was superseded by another document already in evidence is dropped from the comparison, unless the superseding document is the one this run is ingesting — that argument *is* the proposal, and a human has to see both sides. Left in, the retired term returned as a rival value on the next unrelated upload: an invoice, contributing nothing contractual at all, re-opened the MSA-versus-amendment question a human had closed two runs earlier, and the register flipped back to the superseded number as `disputed`.
- **Precedence:** Contractual facts set the register value when any exist; operational facts sit beside them as evidence. A key with only operational facts (`invoice_amount_due`) is decided by those.
- **Partitioned by row (before comparison):** Facts are grouped by `(agreement, key)` and each group derived on its own, so Alpha's payment term and Beta's never meet. Comparability within a group is still the scope gate's job — an invoice issued under Alpha lands in Alpha's group and is weighed there as operational evidence.
- **Purpose:** Within one scope, evaluates facts against existing register items to identify:
  - `contradiction`: Multiple live values for the same obligation key without supersession language.
  - `supersession_candidate`: Express language (e.g., *"This Amendment supersedes Section 4 of the Master Agreement"*) modifying prior commitments.
  - `ambiguous_scope`: A contractual term that names no agreement while the collection holds more than one it could belong to. Which agreement it belongs to decides which row it writes, and nothing in the text says. **No value is derived at all** — not into either agreement's row, not into a third of its own — and the run raises a `scope_question` review item at Human Gate 1. A `scope_question` carries no `after`, so neither commit path can write it: approving the question is not approving a value, and the way to resolve it is a re-upload that names the agreement. Operational facts are exempt: an invoice total is the invoice's own fact and no contract states it.

#### Stage 11: `assemble_proposals`
- **Purpose:** Converts candidate facts, supersession linkages, and conflicts into `ReviewItem` entities (`state = pending`).

#### Stage 12: `await_review` (Human Gate 1)
- **Purpose:** Triggers a LangGraph `interrupt()`. Graph execution pauses, saving state to `AsyncPostgresSaver`.
- **Resume Action:** An authenticated human reviewer calls `POST /api/runs/{id}/resume` to approve or reject individual proposals.

#### Stage 13: `build_candidate_register`
- **Purpose:** Construct in-memory candidate register state overlaying approved proposals onto stored register items. Unapproved or rejected proposals are excluded.

#### Stage 14: `apply_deliverable_rules`
- **Purpose:** Evaluates Stage 2 rules (`scope = deliverable` or `both`) against the candidate register state, one agreement-scoped row at a time. `"payment_due_days violates PAY-01"` did not say whose contract; with two agreements on file that is the only part anyone needs.
- **Rule targeting:** A rule naming `keys` in the playbook is evaluated against those obligations in each agreement that holds them — the playbook names obligations, not agreements.
- **Aggregate:** A rule naming no keys is about the register as a whole and gets one additional explicit verdict with `target_kind = register`, `target_key = collection`. It is *derived* from that rule's per-agreement rows (adverse if any is adverse) rather than re-asked of the model: an aggregate that could disagree with its own parts is worse than none, and re-asking costs a second pass. A rule that ran against nothing gets no aggregate — a pass nobody earned is the silent-clean failure again. Keyed rules get no aggregate; their answer is already per-obligation. Aggregates count toward `rules_expected`/`rules_completed` but are not escalated at Gate 2, since the rows they summarise already are.

#### Stage 15: `assemble_findings`
- **Purpose:** Aggregates findings from source and deliverable rule stages. Every evaluated rule writes an explicit verdict (`pass`, `violation`, or `insufficient_evidence`).
- **Gate 2 opens for every evaluation, adverse or not.** It used to open only when something was wrong, so the runs nobody looked at were exactly the runs that reported themselves clean. When nothing is adverse the gate asks for one item instead — a `deliverable_confirmation` carrying every evaluation it stands for, the rule counts, and the extraction warnings — so "no adverse findings" is a named human's claim rather than the pipeline's. A run whose playbook evaluated nothing at all skips the gate: `rules_expected` is zero, `clean` is already unreachable, and there is nothing to put a name to.
- **Decision binding:** every review item carries `basis_hash`, `ruleset_hash` and `item_versions` — the exact register and playbook the decision is a decision *about*.

#### Stage 16: `await_finding_review` (Human Gate 2)
- **Purpose:** Graph pauses at a second `interrupt()` for every deliverable evaluation.
- **No silent skips:** a resume payload that omits any open item is refused, not obeyed. Leaving an item out would otherwise walk past a finding — or past the confirmation itself — with no trace anywhere.
- **Decisions recorded here, not at commit:** `record_finding_decisions` writes `review_decision`, `decided_by` and `decided_at` beside the verdict. Writing them at commit meant a run that was blocked, refused as stale, or abandoned lost every decision a human had already made, which is precisely the set of runs whose audit trail matters. `verdict` is never in the `SET` list.
- **Dismissal is disagreement, not deletion:** the verdict, rationale and citations are untouched; the finding is flagged `recheck_required`, reported under `rules.dismissed` with the name of whoever dismissed it, and the document's `source_rule_cache` entry is dropped so the next upload of the same bytes re-earns the verdict instead of inheriting one reviewer's call as policy.
- **Declining the confirmation stops the run:** it routes straight to the report with `status: unconfirmed`, nothing committed. Treating "I do not accept this" as consent is the failure the gate exists to prevent.

#### Stage 17: `enforce_blockers`
- **Purpose:** Checks for approved finding violations with `severity = blocker`.
- **Commit Guard:** If a blocker finding is approved, the run status updates to `blocked`, and the commit is aborted. Passing a blocker requires document remediation or an explicit, reasoned override (`POST /api/runs/{id}/override`).

#### Stage 17b: `verify_review_binding`
- **Purpose:** Re-checks, immediately before the commit, that what the human agreed to is still what would be written. `_candidate_rows` is recomputed from live storage and re-hashed; the active ruleset hash is compared against the pinned one.
- **Why the per-key check is not enough:** `stale_proposal` catches a key *this* run is writing. A deliverable verdict is a statement about the whole candidate register, so a row this run never touches moving underneath it still invalidates the verdict a human upheld. Drift here reports `status: stale`, blocks the run, and writes nothing — one re-run against the register as it now stands.

#### Stage 18: `commit_approved`
- **Purpose:** Commits approved register changes within a single database transaction.
- **Concurrency Control:**
  1. Acquires collection-level advisory lock: `pg_advisory_xact_lock(collection_id)`.
  2. Verifies optimistic item version numbers (`version = expected_version`).
  3. Refuses stale proposals (`StaleProposal`), reporting `status: stale` if target register items modified concurrently.
  4. Writes `register_items`, `register_item_citations`, `run_snapshots` (before/after SHA-256 content hashes), and `change_log`. A review item's `target_key` is parsed back into `(agreement_id, key)`, and the row is selected `FOR UPDATE` on all three columns, so one agreement's commit takes no lock on and writes no version to another's row.

#### Stage 19: `snapshot_diff_report`
- **Purpose:** Generates the final execution report summary, calculating overall evaluation completion, total rules expected vs. completed, and the `clean` health indicator. `clean: true` requires zero violations, zero failed rules, all expected rules run, no extraction warnings — **and** a named human who confirmed the deliverable at Gate 2. A clean result is a person's claim, not the pipeline's, and the assertion in `_rules_summary` refuses to emit one without a name.
- **Review disclosure:** `report["review"]` carries `deliverable_reviewed_by`, `deliverable_confirmed`, `candidate_basis_hash`, `ruleset_hash` and any `decisions_stale` drift; `report["rules"]["dismissed"]` names every adverse verdict a human overrode and who overrode it.
- **Register display:** `register_by_agreement` groups every stored row under the agreement that owns it (`(no agreement named)` for the unnamed bucket). `register_hashes`, `affected_keys`, `committed_keys`, `unchanged_keys` and `stale_keys` all speak `RegisterKey` text, so "unchanged" is a claim about one agreement's row rather than about an obligation shared across several.

---

## 5. Security & Human-In-The-Loop Invariants

### 5.1 Role-Based Token Authentication (`doctask/auth.py`)

Credentials are defined as `token:actor_id` pairs via environment variables:
- `DOCTASK_SERVICE_TOKENS`: Machine credentials for proposing operations (ingestion, graph execution, tool calling).
- `DOCTASK_REVIEWER_TOKENS`: Authenticated human credentials required for decision actions (`approve`, `reject`, `override`).

```python
# Security Rule Enforcement
def require_reviewer(principal: Principal) -> Principal:
    if not principal.is_reviewer:
        raise AuthorizationError(
            f"{principal.actor_id} is a {principal.role}: only an authenticated reviewer "
            "can approve, reject, or override a blocker"
        )
    return principal
```

### 5.2 Database Level Security (`decide_review_item`)
Decisions execute via a `SECURITY DEFINER` PostgreSQL function (`decide_review_item`), enforcing compare-and-set semantics on review items (`WHERE state = 'pending'`). The model service database role lacks direct UPDATE access to `review_items`.

---

## 6. Storage & Concurrency Architecture

### 6.1 Deduplication & Idempotency
- **Document Deduplication:** `UNIQUE (collection_id, sha256)` on `documents`. Re-uploading an identical file returns the existing document immediately.
- **Run Idempotency:** `UNIQUE (collection_id, idempotency_key)` on `runs`. Re-submitting requests with identical keys returns original run details without re-executing LLM steps.
- **Fact Replay Safety:** `UNIQUE (collection_id, document_id, block_id, fact_fingerprint)` on `facts`. Replaying a graph node after a system crash will not duplicate facts.
- **Stage ledger (`run_stage_ledger`):** one row per `(run_id, stage, input_hash)` carrying `output_hash`, `status` (`started`/`completed`) and `attempts`. Every node that writes domain state records one, through `_event`, so the two are produced together rather than by a second call site that can be forgotten. Idempotent writes are a property of each statement; the ledger is the *record* — after a SIGKILL it can answer "did this exact stage complete, and with what result", which no amount of `ON CONFLICT` can. `attempts > 1` with an unchanged `output_hash` is a replay that agreed with itself, which is the only thing exactly-once can concretely mean here. `input_hash` includes the validation attempt, because `retry_extract` re-enters extraction with wider context on purpose and that is different work under the same stage name.
- **`started` before the write, where the write is self-identifying:** `ingest` records `started` *before* `put_document`. A process killed between the insert and its checkpoint otherwise leaves a document row with nothing saying which run put it there — so the retry sees its own write, calls it a duplicate, short-circuits, and never extracts a fact from a document that was never actually processed. The `started` row is what lets the replay recognise its own earlier attempt.
- **Commit idempotency:** `commit_approved(collection_id, run_id, basis_hash=...)`. The ledger row is written **inside the same transaction as the register writes**, so there is no window in which the register has moved and the ledger does not know. A replay of the node — the ordinary consequence of a SIGKILL between the commit and LangGraph's checkpoint — returns what was written instead of versioning every row again. Relying on the per-key content hashes to no-op is true most of the time and false in exactly the case that matters, which is a concurrent commit in between.
- **Advisory lock scope:** `pg_advisory_xact_lock` serialises every commit in the collection, so everything held under it is time other runs spend waiting. It is taken immediately before the first canonical-register read — *after* the ledger check and after reading this run's own approved review items, neither of which touches shared state — and released with the transaction.
- **Run lease (`runs.lease_owner`, `runs.lease_expires_at`):** `thread_id = run_id` makes resume addressable by anyone who knows the run, which is the right design and also means a retrying HTTP client, a watcher and two replicas can drive the same thread at once. The domain writes survive that; the human gates do not, because two processes can each `interrupt()` and each be answered. `acquire_run_lease` is compare-and-set — the `WHERE` clause is the precondition, so exactly one caller gets a row back — wrapped around every graph invocation in `runtime`. It expires, because a process SIGKILLed holding a lease must not lock its own run out of the resume that would recover it. A refusal is `409 RunBusyError`, which is a different answer from "your decision was rejected".

### 6.2 Content Hashing & Audit Integrity
Register item hashes are computed canonically:
$$\text{content\_hash} = \text{SHA256}(\text{canonical\_json}(\text{value}, \text{sorted\_citation\_ids}, \text{state}))$$
Diffing snapshots before and after a run (`run_snapshots`) guarantees untouched items remain byte-for-byte identical.

---

## 7. LLM Integration & OCR Subsystem

```text
[ Uploaded Page ]
       |
       v
< Native Extraction (PyMuPDF) >
       |
       +---> [ Usable Text Extracted ] ------> Quality Check
       |                                             |
       |                                             +---> Clean prose? ------------> [ Mark: native_pdf ]
       |                                             |
       |                                             +---> Garbled/Mojibake/Image? -> ( OCR Fallback )
       |                                                                                    |
       +---> [ No Text / Image Only Page ] -------------------------------------------------+
                                                                                            |
                                                                                            v
                                                                             [ Render Page PNG @ 200 DPI ]
                                                                                            |
                                                                                            v
                                                                             [ Call Gemma 3 27B VLM OCR ]
                                                                                            |
                                                                                            +---> Legible text parsed? ---> [ Mark: gemma_vlm ]
                                                                                            |
                                                                                            +---> Unreadable / Failed? ---> [ 422 ExtractionError: Abort ]
```

### LLM Gateway (`doctask/llm/gateway.py`)
- Interfaces with OpenAI-compatible servers (e.g., vLLM hosting `Medha` / `gemma-3-27b-it`).
- Features structured JSON parsing defenses that repair truncated responses or whitespace loops from guided decoding engines.

---

## 8. Machine & API Surfaces

### 8.1 FastAPI Service Endpoints (`doctask/api.py`)
- `POST /api/runs/upload`: Multipart upload for PDF, DOCX, TXT contracts.
- `POST /api/runs`: JSON ingestion endpoint for pre-parsed block text.
- `POST /api/runs/{run_id}/resume`: Human review decision submission.
- `POST /api/runs/{run_id}/override`: Authenticated blocker override.
- `POST /api/runs/{stale_run_id}/rederive`: Redo stale proposals against current register state.
- `PUT /api/collections/{collection_id}/ruleset`: Playbook schema ingestion.
- `GET /api/runs/{run_id}/findings`: Fetch all rule verdicts and citations.

### 8.2 MCP Server Surface (`doctask/mcp_server.py`)
Provides Model Context Protocol (MCP) tool bindings:
- `ingest_document`: Submits documents into collections.
- `get_run_status`: Retrieves execution progress and stage telemetry.
- `resume_run`: Submits reviewer decisions over MCP.
- `get_register`: Fetches the current grounded obligations register.

---

## 9. Verification & Synthetic Demo Matrix

The end-to-end flow is validated deterministically via `scripts/run_demo.py` over 5 synthetic files:

| # | File | Format | Key Obligations Tested | Demonstrated Pipeline Behavior |
|---|------|--------|------------------------|--------------------------------|
| 1 | `01_Master_Services_Agreement` | PDF | `payment_due_days: 30`, `liability_cap: 250000`, `notice_days: 60` | Baseline agreement ingest. All 3 playbook rules pass (`PAY-01`, `LIAB-01`, `NOT-01`). |
| 2 | `02_Amendment` | PDF | `payment_due_days: 45`, `notice_days: 90` | Amendment detection. Proposes supersession of MSA terms. `liability_cap` remains untouched at 250,000. |
| 3 | `03_Invoice` | PDF | `payment_due_days: 10` | Ingests NET 10 invoice. Triggers `PAY-01` source rule violation. Contractual term remains 45 days. |
| 4 | `04_DPA` | DOCX | No obligation changes | Native DOCX parsing. Document parsed cleanly without modifying payment or liability terms (no-op update). |
| 5 | `05_Notice` | TXT | Unclassifiable document type | Low classification confidence (`< 0.70`). Triggers human escalation interrupt without committing false register data. |

---

## 10. Summary Matrix of Design Invariants

| Invariant | Architectural Mechanism | Enforced At |
|---|---|---|
| **Grounding** | Exact quote matching & character span offset verification (`validate_citation`), then the value re-read out of the quote — unit, date, polarity, negation, anchor, qualifiers (`services/grounding.py`). | `validate_citations` node |
| **Evidence Identity** | `EvidenceSpan` over document hash, block index, page, offsets, quote hash and parser version; register content hashes are built from its fingerprint and `register_content_hash` rejects anything that is not a SHA-256, so a row id cannot be passed. | `validate_citations` & `commit_approved` |
| **Human Authority** | Model proposes `pending` items; only `reviewer` token principal can update state (`require_reviewer`). Gate 2 opens on every evaluation; an omitted item is refused, not skipped. | API layer & LangGraph Gates |
| **Decision Integrity** | `verdict` immutable and separate from `review_decision`/`decided_by`; decisions written at the gate, not at commit, so a blocked or stale run keeps them. Dismissal flags `recheck_required` and drops the source-rule cache entry. | `await_review` / `await_finding_review` |
| **Decision Binding** | Every review item stamped with `basis_hash` (register rows + versions), `ruleset_hash` and `item_versions`; re-verified before commit. | `verify_review_binding` node |
| **Database Concurrency** | `pg_advisory_xact_lock` around the canonical-register writes only + optimistic version numbers (`version = expected`). | `commit_approved` node |
| **Exactly-once** | `run_stage_ledger (run_id, stage, input_hash)` with output hash and attempt count; the commit's row is written in the same transaction as the register writes, making commit idempotent on `(run_id, candidate_basis_hash)`. | every writing node; `commit_approved` |
| **Single Driver** | Compare-and-set run lease with a TTL around every graph invocation, so two resume requests cannot execute one LangGraph thread. | `runtime.run_lease` |
| **Audit Provenance** | Immutable `change_log` table & before/after content SHA-256 snapshots (`run_snapshots`). | Postgres Repository Commit |
| **Rule Integrity** | Pinned ruleset SHA-256 (`pin_ruleset`) + explicit `pass` verdicts for complete denominators. | `pin_ruleset` & `assemble_findings` |
| **Extraction Safety** | Fail-fast `422 ExtractionError` on unreadable text/scans to prevent false clean evaluations. | `ingest` node |

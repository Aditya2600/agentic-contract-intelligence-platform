# Progress Log

## Initial assumptions

- Domain: vendor contracts, amendments, SOWs, purchase orders, and invoices.
- Deliverable: obligations register.
- Initial file support: TXT in the offline demo; PDF/DOCX are planned production adapters.
- Human identity and authorization are represented in interfaces but require deployment-specific authentication.
- The included `FakeLLM` is for offline proof only.

## Assumptions taken while implementing the model gateway

- Target server is the vLLM 0.22.0 instance at `http://164.52.193.211:8001`, model id `Medha`,
  bearer-key authenticated. The API key is configuration and is never committed.
- That server's `response_format=json_schema` guided decoding stalls in a whitespace loop
  after the first field, so the gateway uses `json_object` plus the schema in the prompt and
  enforces the schema itself after parsing. Revisit if the server is upgraded.
- The model answers `value` as a bare scalar for some keys. A scalar is wrapped as
  `{"value": <scalar>}` rather than guessing a field name the source never used.
- The model returns a `quote` only. Offsets are recomputed with `block.text.find(quote)`,
  so a paraphrased quote gets offsets the deterministic validator rejects.
- Facts whose key is outside `OBLIGATION_KEYS` are dropped before they reach the graph.
- `run_events.tokens_in/out` come from the server's `usage` block. `cost_usd` stays 0
  until a price per model is configured.
- The citation-repair retry now re-extracts with a stricter prompt instead of only
  re-validating the same candidates.

## Assumptions taken while implementing the Postgres repository

- `review_items.target_id` is a UUID but register keys are text, so the target id is
  `uuid5(NAMESPACE_OID, "<kind>:<target_key>")`. `UNIQUE(run_id, kind, target_id)` therefore
  means "one decision per key per run", and the human-readable key is carried in `payload.target_key`.
- Replayed nodes adopt the persisted row identity: `put_blocks` and `add_review_items` write the
  stored id back into the caller's objects, so a resumed run cites one row rather than a second copy.
- `commit_approved` treats an item whose `content_hash` and `last_run_id` already match as
  already applied. A replayed commit is a no-op rather than a version bump.
- `run_events.seq` is `MAX(seq) + 1`, which is safe only because a run's nodes execute serially.
- Register `title` defaults to the key until the ontology supplies display names.
- The checkpointer uses its own autocommit pool because `AsyncPostgresSaver` requires one.

## Assumptions taken while implementing register derivation and conflicts

- Precedence is decided by explicit source language plus a document link, never by
  recency or document type. An amendment supersedes only the document its
  `supersedes_id` points at; everything else disagreeing is a contradiction.
- The supersession target is resolved deterministically as the most recent
  `master_agreement`/`sow` in the collection. A real deployment should read the
  amendment's own reference to the agreement it modifies.
- Rationale evidence is key-specific: the sentence carrying that key's quote wins,
  and the document-level supersession sentence is only the fallback.
- A contradiction proposal carries the *incumbent* value, so approving it can never
  install the contradicting value. It marks the item `disputed` and closes the conflict.
- Rejecting a proposal leaves its conflict `open`; only an approved proposal resolves one.
- Conflicts are keyed by `UNIQUE(collection_id, key, fact_a_id, fact_b_id)`, so a
  replayed detection returns the stored conflict rather than opening a second one.
- Register citations are replaced wholesale on commit; a superseded fact stops being
  cited so the reverse invalidation index does not accumulate stale edges.
- Re-derivation producing the stored content hash emits no review item at all, which
  is what keeps untouched items at their original version.

## Assumptions taken while implementing multi-stage rules

- A playbook is data. `parse_ruleset` is the trust boundary: severity, scope, codes and
  version are validated there, so an uploaded file cannot smuggle an unknown scope past
  the evaluator.
- Every rule in scope writes exactly one finding per run and target.
- A model outage is not a verdict. `insufficient_evidence` is a judgement - the model read
  the target and found nothing to weigh - so an unreachable server, a timeout or an
  unparsable response must not be recorded as one, or a silent contract and a dead
  dependency become the same row. Any failure to obtain a verdict fails the run: the stage
  writes nothing (verdicts already collected in that stage are discarded, so a rules result
  is never half a stage), logs a `transient` run event naming the rule, and re-raises. The
  checkpoint holds, so re-invoking the run resumes at that node. A broken stage still
  cannot be mistaken for a clean corpus - it produces no report at all.
- A `violation` whose quote is not verbatim in a block is downgraded to
  `insufficient_evidence`. An accusation with no citation is not a finding.

## Assumptions taken while bounding and pinning rule evaluation

- The playbook is pinned once at run start (`pin_ruleset`), by id, with a `sha256` of its
  source text recorded in the event log. Both stages used to call `get_active_ruleset`
  independently, so an upload landing between them would have judged one document against
  two playbooks. The hash is the audit artefact: `name v2` can be edited, a hash cannot.
- Evidence per evaluation is bounded by configuration (`rule_context_blocks`,
  `rule_context_chars`), not by whatever the model server happens to accept. Cost then
  scales with rules x affected keys rather than with the corpus, and a silently truncated
  prompt - the way a violation turns into a `pass` - is no longer reachable.
- Selection is term overlap, not a model call. A finding has to be re-derivable from the
  same inputs during an audit, so the choice of evidence must be deterministic.
- A block larger than the whole character budget is skipped rather than truncated. A
  truncated block would leave the model quoting text that is verbatim in nothing.
- Blocks that share no term with the rule are still eligible once the scoring ones run
  out: a rule about a subject the document never mentions has to be able to reach
  `insufficient_evidence`, and it can only do that by being shown something.
- The model cites an excerpt by index, not by UUID - an index it can copy correctly and
  the caller can check, where a mangled UUID would silently ground nothing. `ground_verdict`
  validates the index *and* the quote inside that one excerpt. Scanning every excerpt for
  the quote would repair a model that cited the wrong location, and location is half of
  what a citation is for.
- In the deliverable stage the thing judged and the thing cited are separate arguments:
  the candidate value travels as `statement`, the source blocks behind it as excerpts.
  Offering the rendered value as a quotable excerpt invited citations that proved the
  rendering rather than the contract.
- Source stage targets the uploaded document. The deliverable stage targets one register
  key at a time (`target_kind = "register_item"`, id derived from collection + key), so a
  violation can name the obligation it is about. Judging the whole register as one unit
  gave every verdict one shared row and diluted the evidence for each key with every other
  key in the prompt.
- A deliverable rule may name `keys` in the playbook; one that names none applies to every
  affected key. Only keys this run touched are evaluated - re-judging state nobody changed
  would reopen settled questions on every upload.
- `pass` rows raise no review item; `violation` and `insufficient_evidence` do. Approving
  a finding records the human decision and changes no register value.

## Assumptions taken while ordering the two review gates

- The deliverable stage runs **after** the first human gate, against a candidate register
  built from the stored register overlaid with the proposals the human approved. Running
  it on the proposal set would have judged the request rather than the deliverable: a
  rejected proposal would still have been scored, and an approved one would have been
  scored without the untouched keys around it.
- The candidate register lives in graph state only. It is a list of
  `{key, value, state, citation_fact_ids}` for the whole collection, which keeps the
  checkpoint proportional to the register rather than to the corpus. Nothing is written
  before `commit_approved`, so a crash between the gates loses no work and commits none.
- Deliverable findings get their own gate (`assemble_findings` writes the review items,
  `await_finding_review` interrupts). The write is a separate node because an `interrupt`
  restarts its node from the top on resume, and re-running `add_review_items` there would
  mint duplicate rows.
- A deliverable stage with nothing to decide skips its gate entirely, so the ordinary path
  is still one resume. Callers must loop until the result carries a `report`; the demo,
  both harnesses, and `scripts/run_demo.py` do.

## Assumptions taken while making "no findings" provable

- A count of zero is not a result. The report carries `rules_expected`,
  `rules_completed`, `rules_failed` and `evaluation_complete` alongside the verdict
  counts, so a stage that never ran cannot be read as a corpus that passed.
- `clean` is the only field allowed to mean "no findings", and it is the conjunction of
  every condition that has to hold: complete evaluation, zero failures, zero violations,
  zero insufficient evidence.
- `clean` additionally requires `rules_expected > 0`, which goes beyond the arithmetic. A
  collection with no playbook satisfies "no errors and no violations" vacuously, and that
  is the same misreading the counters exist to prevent - unchecked is not clean.
- `rules_failed` is zero on every path today, because an evaluation error re-raises and
  the stage writes nothing. It is wired from `len(rules) - len(stored)` anyway, so a
  repository that silently drops rows shows up as an incomplete evaluation rather than as
  a smaller clean-looking result.
- `_rules_summary` asserts that a `clean` summary has one pass per expected rule. The
  invariant is cheap enough to check at the point it is claimed.

## Assumptions taken while enforcing blockers

- Approving a finding means "this problem is real". It cannot also mean "commit anyway",
  so an approved `blocker` finding - from either stage - stops the commit. Rejecting the
  finding is the human saying it does not apply, and the run continues; that is the whole
  difference between the two decisions.
- Getting past an upheld blocker takes a second, explicit act, which is why
  `enforce_blockers` is its own gate rather than a flag on the finding decision.
  Remediating and re-running is the intended path; the override is the escape hatch.
- An override with no reason is refused (`ValueError`), not silently downgraded to "leave
  it blocked". A stated intent to commit past a blocker must leave a written
  justification. The run stays parked at the interrupt, so a corrected override still
  works - the same recovery shape as a rejected review decision.
- A blocked run writes nothing at all: no register rows, no version bumps. `runs.status`
  becomes `blocked` (new enum value) so an operator can find held runs instead of seeing
  them as merely unfinished.
- Findings upheld on a blocked run keep `state = 'proposed'`, because `findings.state` is
  only advanced inside the commit transaction. The human decision is not lost - it is on
  the `review_items` row with `decided_by`. Promoting finding state outside a commit is
  the upgrade path if auditors want it in one place.

## Current status

- [x] Repository skeleton
- [x] Initial schema
- [x] Graph state and node signatures
- [x] In-memory repository
- [x] Deterministic fake model
- [x] FastAPI route skeleton
- [x] MCP tool skeleton
- [x] Minimal review UI
- [x] Offline tests
- [x] Production PostgreSQL repository
- [x] Durable Postgres checkpointer wiring
- [x] Register derivation, supersession proposals, and conflict rows
- [x] Reverse citation index driving affected-key invalidation
- [x] Two-stage rule evaluation with explicit pass/violation/insufficient-evidence rows
- [x] Playbook upload over HTTP and MCP (`PUT /collections/{id}/ruleset`, `upload_ruleset`)
- [ ] PDF/DOCX parsing
- [x] Production LLM gateway (verified end to end against the live vLLM server)
- [ ] Authentication and database role separation
- [ ] Postgres integration tests executed against a live database (`make test-pg`; written, not yet run)
- [x] Live model proof: classify + extract + commit with grounded quotes (`scripts/check_gateway.py`)
- [ ] Crash/resume integration proof
- [ ] Concurrent-run integration proof

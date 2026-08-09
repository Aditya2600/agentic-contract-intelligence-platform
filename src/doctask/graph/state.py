from __future__ import annotations

from typing import Any, TypedDict


class GraphState(TypedDict, total=False):
    run_id: str
    collection_id: str
    idempotency_key: str
    actor_id: str

    # Pinned once at run start. Every stage evaluates against this exact playbook, so a
    # mid-run upload cannot change the rules between the source and deliverable stages.
    ruleset_id: str | None
    ruleset_hash: str | None

    # Set instead of `input_document` when this run exists to redo what an earlier run
    # could not commit. The document is already stored, so there is nothing to upload.
    rederive_from_run: str | None

    input_document: dict[str, Any]

    # What the extractor was unsure about: a page a vision model read, a rotated page, a
    # flattened table, an encrypted file. Not fatal, and not ignorable either -- every
    # one means a rule may have been judged on evidence that is not quite what the file
    # says, so the report cannot claim a clean result while any of these stand.
    extraction_warnings: list[str]
    document_id: str | None
    # Half of the source-rule cache key. Carried in state so the stage does not have to
    # re-read the document row to find out which bytes it is judging.
    document_sha256: str | None
    duplicate_document: bool
    document_type: str | None
    document_type_confidence: float | None

    block_ids: list[str]

    # How this document says it stands to the others, read from its own sentences.
    # `agreement_id` is what scopes every fact it produces, and therefore what decides
    # which existing facts those can be in conflict with at all.
    agreement_id: str | None
    amends_agreement: bool
    relations: list[dict[str, Any]]

    fact_candidates: list[dict[str, Any]]
    affected_keys: list[str]
    unsupported_count: int
    validation_attempt: int
    # Telemetry, never permission: true when any block carried an injection signal.
    # Nothing routes on it, and nothing is relaxed when it is false.
    injection_flag: bool
    # One entry per block withheld from extraction, rule context and register updates,
    # with the signals that withheld it and the text as the model would have read it.
    quarantined_blocks: list[dict[str, Any]]
    supersession_evidence: str | None

    # Only affected keys are read, so the checkpoint stays proportional to the change.
    register_before: dict[str, Any]
    derivations: list[dict[str, Any]]
    conflict_ids: list[str]
    unchanged_keys: list[str]
    findings: list[dict[str, Any]]

    # Denominator for the findings list. Without it, "0 violations" cannot be told apart
    # from "no rule ever ran", which is the reading that gets someone hurt.
    rules_expected: int
    rules_completed: int
    rules_failed: int

    # The register as it would stand if this run committed: stored items overlaid with
    # the proposals a human approved. Deliverable rules read this, not the request.
    candidate_register: list[dict[str, Any]]

    review_item_ids: list[str]
    finding_review_item_ids: list[str]

    # What a reviewer's decision was a decision about: the candidate register, its item
    # versions, and the pinned playbook, hashed together. Re-checked before the commit;
    # anything that moved in between means the approval no longer describes what would
    # be written. See `verify_review_binding`.
    candidate_basis_hash: str | None
    decisions_stale: list[dict[str, Any]]

    # Gate 2 runs on every deliverable evaluation, so every run carries the identity of
    # whoever signed off. A run with no name here was never reviewed, and `clean` is not
    # a claim it may make.
    deliverable_review_by: str | None
    deliverable_confirmed: bool

    # Rule codes a human confirmed as blockers, and the override that let the commit
    # through anyway. An override without a reason is not an override.
    blocked_by: list[str]
    commit_blocked: bool
    override_reason: str | None
    override_by: str | None
    approved_review_item_ids: list[str]
    rejected_review_item_ids: list[str]
    committed_keys: list[str]

    # Proposals refused at commit because their register item moved after they were
    # derived from it. Approved by a human, never applied: the run reports them so the
    # document can be re-run against the register as it now stands.
    stale_keys: list[dict[str, Any]]

    current_stage: str
    status: str
    report: dict[str, Any]

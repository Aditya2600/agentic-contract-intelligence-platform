from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID

from doctask.domain import (
    Block,
    CommitResult,
    Conflict,
    Document,
    DocumentRelation,
    FactCandidate,
    Finding,
    RegisterChange,
    RegisterItem,
    ReviewItem,
    Ruleset,
    RunEvent,
    RunSummary,
    StageRecord,
    StaleProposal,
    StoredFact,
)


class CollectionConflictError(ValueError):
    """A collection slug already names something else.

    Raised instead of silently renaming the existing row: a caller who repeats their
    own request must get the same collection back, but a caller who reuses somebody
    else's slug by accident needs to find out, not overwrite their name.
    """


def stale_proposal(
    key: str,
    payload: dict[str, Any],
    *,
    actual_version: int | None,
    actual_hash: str | None,
) -> StaleProposal | None:
    """Compare what a proposal was derived from against what is stored now.

    `payload["before"]` is written when the proposal is assembled: the version and
    content hash of the register item it read, or None when the key did not exist
    yet. Either having moved means someone else committed this key in between, so
    the approval no longer describes the change it would make.
    """
    before = payload.get("before") or None
    expected_hash = before.get("content_hash") if before else None
    expected_version = before.get("version") if before else None
    moved = expected_hash != actual_hash or (
        expected_version is not None and expected_version != actual_version
    )
    if not moved:
        return None
    return StaleProposal(
        key=key,
        expected_version=expected_version,
        expected_hash=expected_hash,
        actual_version=actual_version,
        actual_hash=actual_hash,
    )


class Repository(Protocol):
    # `slug` defaults to a normalised form of `name` (see `services.ids.slugify`) when
    # omitted. Same slug, same name: returns the existing collection. Same slug, a
    # different name: raises `CollectionConflictError` rather than renaming it.
    async def create_collection(self, name: str, *, slug: str | None = None) -> UUID: ...
    async def create_run(
        self,
        *,
        collection_id: UUID,
        run_id: UUID,
        idempotency_key: str,
        trigger: str = "api",
    ) -> tuple[UUID, bool]: ...
    async def block_run(self, run_id: UUID, reason: str) -> None: ...
    async def put_document(self, document: Document) -> tuple[Document, bool]: ...
    async def get_document(self, document_id: UUID) -> Document | None: ...
    async def set_document_type(
        self, document_id: UUID, doc_type: str, confidence: float | None
    ) -> None: ...
    async def link_supersession(self, document_id: UUID, supersedes_id: UUID) -> None: ...
    async def put_document_relations(self, relations: list[DocumentRelation]) -> None: ...
    async def list_document_relations(self, document_id: UUID) -> list[DocumentRelation]: ...
    # Documents in this collection that declare an agreement identifier, by identifier.
    # How a relation's `target_ref` becomes a `target_document_id`.
    async def documents_by_agreement_ref(self, collection_id: UUID) -> dict[str, UUID]: ...
    async def set_agreement_ref(self, document_id: UUID, agreement_ref: str) -> None: ...
    async def find_supersession_target(
        self, collection_id: UUID, exclude_document_id: UUID
    ) -> UUID | None: ...
    async def put_blocks(self, blocks: list[Block]) -> None: ...
    async def get_blocks(self, document_id: UUID) -> dict[UUID, Block]: ...
    async def get_blocks_by_ids(self, block_ids: list[UUID]) -> dict[UUID, Block]: ...
    async def put_facts(self, collection_id: UUID, facts: list[FactCandidate]) -> list[FactCandidate]: ...
    async def get_active_facts(self, collection_id: UUID, keys: list[str]) -> list[StoredFact]: ...
    # Both sides speak `RegisterKey.text`: "AGREEMENT::key", or "::key" when the
    # collection names no agreement. A bare obligation key addresses no row.
    async def affected_register_keys(
        self, collection_id: UUID, scoped_keys: list[str], document_id: UUID
    ) -> list[str]: ...
    async def get_register_items(
        self, collection_id: UUID, scoped_keys: list[str]
    ) -> dict[str, RegisterItem]: ...
    async def put_conflicts(self, conflicts: list[Conflict]) -> list[Conflict]: ...
    async def list_conflicts(
        self, collection_id: UUID, *, state: str | None = "open"
    ) -> list[Conflict]: ...
    async def put_ruleset(self, ruleset: Ruleset) -> Ruleset: ...
    async def get_active_ruleset(self, collection_id: UUID) -> Ruleset | None: ...
    async def get_ruleset(self, ruleset_id: UUID) -> Ruleset | None: ...
    async def put_findings(self, findings: list[Finding]) -> list[Finding]: ...
    async def list_findings(self, run_id: UUID) -> list[Finding]: ...
    # Recorded at the gate that made the decision, not at commit: a run that never
    # commits still has to keep what a human decided. `verdict` is never written here.
    async def record_finding_decisions(
        self, run_id: UUID, actor_id: str, decisions: dict[UUID, str]
    ) -> list[Finding]: ...
    async def drop_source_rule_cache(self, cache_key: str) -> None: ...
    # The run whose source findings were produced for this exact (document, ruleset,
    # evaluator) triple, or None. A miss means the stage re-evaluates; there is no
    # partial match, because a near-miss verdict is the wrong verdict.
    async def get_source_rule_cache(self, cache_key: str) -> UUID | None: ...
    async def put_source_rule_cache(
        self,
        cache_key: str,
        *,
        collection_id: UUID,
        run_id: UUID,
        document_sha256: str,
        ruleset_hash: str,
        evaluator_version: str,
    ) -> None: ...
    # --- exactly-once bookkeeping -------------------------------------------------
    # One row per (run, stage, input): what the stage was given and what it produced.
    # `record_stage` is an upsert that bumps `attempts`, so a replay is visible as a
    # replay rather than overwriting the first execution's history.
    async def record_stage(
        self,
        run_id: UUID,
        stage: str,
        *,
        input_hash: str,
        output_hash: str | None = None,
        status: str = "completed",
    ) -> None: ...
    # The ledger row for this exact work, or None. A `started` row is not a result -- it
    # means a process began the stage and did not live to finish it -- but it is still a
    # record that *this run* attempted it, which is what tells a replay apart from a
    # genuinely repeated request.
    async def stage_record(
        self, run_id: UUID, stage: str, input_hash: str
    ) -> StageRecord | None: ...
    async def list_stages(self, run_id: UUID) -> list[StageRecord]: ...
    # Compare-and-set. True only for the caller that won; an expired lease is takeable,
    # because a process SIGKILLed while holding one must not lock its own run out.
    async def acquire_run_lease(
        self, run_id: UUID, owner: str, *, ttl_seconds: int
    ) -> bool: ...
    async def release_run_lease(self, run_id: UUID, owner: str) -> None: ...
    async def add_event(self, event: RunEvent) -> None: ...
    async def list_events(self, run_id: UUID) -> list[RunEvent]: ...
    async def add_review_items(self, items: list[ReviewItem]) -> None: ...
    async def list_review_items(self, run_id: UUID) -> list[ReviewItem]: ...
    async def decide_review_items(
        self, run_id: UUID, actor_id: str, decisions: dict[UUID, str]
    ) -> list[ReviewItem]: ...
    # `basis_hash` makes the commit idempotent on (run, exactly this deliverable): a
    # replay returns what the first commit wrote instead of versioning the register again.
    async def commit_approved(
        self, collection_id: UUID, run_id: UUID, *, basis_hash: str
    ) -> CommitResult: ...
    async def list_register(self, collection_id: UUID) -> list[RegisterItem]: ...
    # --- read access for callers that only hold a run id -----------------------
    async def get_run(self, run_id: UUID) -> RunSummary | None: ...
    # The register rows this run actually changed: old value, new value, both hashes.
    # Sourced from `change_log`, written in the same transaction as the commit that
    # produced it, so this never disagrees with what was actually written.
    async def list_run_changes(self, run_id: UUID) -> list[RegisterChange]: ...

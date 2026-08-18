from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from doctask.domain import (
    Block,
    CollectionSummary,
    CommitResult,
    Conflict,
    Document,
    DocumentRelation,
    DocumentSummary,
    FactCandidate,
    Finding,
    RegisterChange,
    RegisterItem,
    RegisterKey,
    ReviewItem,
    Ruleset,
    RunEvent,
    RunSummary,
    StageRecord,
    StoredFact,
    WatchedCollection,
)
from doctask.repositories.base import (
    CollectionConflictError,
    ItemAlreadyDecidedError,
    stale_proposal,
)
from doctask.services.hashing import register_content_hash
from doctask.services.ids import slugify


class InMemoryRepository:
    def __init__(self) -> None:
        self.collections: dict[UUID, str] = {}
        self.collection_by_slug: dict[str, UUID] = {}
        self.collection_slug: dict[UUID, str] = {}
        # collection_id -> the directory the watcher polls for it.
        self.collection_watch_path: dict[UUID, str] = {}
        self.documents: dict[UUID, Document] = {}
        self.document_by_sha: dict[tuple[UUID, str], UUID] = {}
        self.document_ingested_at: dict[UUID, datetime] = {}
        self.supersedes: dict[UUID, UUID] = {}
        self.relations: dict[UUID, list[DocumentRelation]] = defaultdict(list)
        self.blocks: dict[UUID, Block] = {}
        self.facts_by_fingerprint: dict[tuple[UUID, str], FactCandidate] = {}
        self.events: dict[UUID, list[RunEvent]] = defaultdict(list)
        self.review_items: dict[UUID, ReviewItem] = {}
        # Keyed by (collection, RegisterKey.text): one row per agreement per obligation.
        self.register: dict[tuple[UUID, str], RegisterItem] = {}
        self.conflicts: dict[UUID, Conflict] = {}
        self.rulesets: dict[UUID, Ruleset] = {}
        self.findings: dict[UUID, Finding] = {}
        # cache_key -> the run holding the source findings that key stands for.
        self.source_rule_cache: dict[str, UUID] = {}
        self.runs: dict[tuple[UUID, str], UUID] = {}
        self.run_status: dict[UUID, str] = {}
        self.run_collection: dict[UUID, UUID] = {}
        self.run_started: dict[UUID, datetime] = {}
        self.run_ended: dict[UUID, datetime] = {}
        self.run_trigger: dict[UUID, str] = {}
        self.run_trigger_document: dict[UUID, UUID] = {}
        self.run_ruleset: dict[UUID, UUID] = {}
        # run_id -> the register rows that run's own commit actually changed.
        self.change_log: dict[UUID, list[RegisterChange]] = defaultdict(list)
        # (run_id, stage, input_hash) -> what that work produced.
        self.stage_ledger: dict[tuple[UUID, str, str], StageRecord] = {}
        # run_id -> (owner, expires_at). One driver per LangGraph thread at a time.
        self.leases: dict[UUID, tuple[str, datetime]] = {}
        self._lock = asyncio.Lock()

    async def create_collection(self, name: str, *, slug: str | None = None) -> UUID:
        slug = slug or slugify(name)
        if not slug:
            raise ValueError("collection needs a name or an explicit slug")
        async with self._lock:
            existing_id = self.collection_by_slug.get(slug)
            if existing_id is not None:
                if self.collections[existing_id] != name:
                    raise CollectionConflictError(
                        f"collection slug {slug!r} already names "
                        f"{self.collections[existing_id]!r}"
                    )
                return existing_id
            collection_id = uuid4()
            self.collections[collection_id] = name
            self.collection_by_slug[slug] = collection_id
            self.collection_slug[collection_id] = slug
            return collection_id

    async def set_collection_watch_path(
        self, collection_id: UUID, watch_path: str | None
    ) -> None:
        if collection_id not in self.collections:
            raise KeyError(f"collection {collection_id} does not exist")
        if watch_path:
            self.collection_watch_path[collection_id] = watch_path
        else:
            self.collection_watch_path.pop(collection_id, None)

    async def list_watched_collections(self) -> list[WatchedCollection]:
        return [
            WatchedCollection(
                id=collection_id, name=self.collections[collection_id], watch_path=path
            )
            for collection_id, path in sorted(
                self.collection_watch_path.items(), key=lambda entry: str(entry[0])
            )
            if collection_id in self.collections
        ]

    def _collection_summary(self, collection_id: UUID) -> CollectionSummary | None:
        name = self.collections.get(collection_id)
        if name is None:
            return None
        document_ids = [
            doc.id for doc in self.documents.values() if doc.collection_id == collection_id
        ]
        run_ids = [
            run_id
            for (cid, _key), run_id in self.runs.items()
            if cid == collection_id
        ]
        open_statuses = {"running", "awaiting_review", "committing", "blocked"}
        open_run_count = sum(
            1 for run_id in run_ids if self.run_status.get(run_id) in open_statuses
        )
        activity_stamps = [
            self.document_ingested_at[doc_id]
            for doc_id in document_ids
            if doc_id in self.document_ingested_at
        ]
        activity_stamps += [
            stamp
            for run_id in run_ids
            for stamp in (self.run_started.get(run_id), self.run_ended.get(run_id))
            if stamp is not None
        ]
        register_row_count = sum(
            1 for (cid, _key) in self.register if cid == collection_id
        )
        return CollectionSummary(
            id=collection_id,
            name=name,
            slug=self.collection_slug.get(collection_id, ""),
            document_count=len(document_ids),
            register_row_count=register_row_count,
            open_run_count=open_run_count,
            last_activity_at=max(activity_stamps) if activity_stamps else None,
            watch_path=self.collection_watch_path.get(collection_id),
        )

    async def list_collections(self) -> list[CollectionSummary]:
        summaries = [self._collection_summary(cid) for cid in self.collections]
        return sorted(
            (s for s in summaries if s is not None), key=lambda s: s.name
        )

    async def get_collection(self, collection_id: UUID) -> CollectionSummary | None:
        return self._collection_summary(collection_id)

    async def list_documents(self, collection_id: UUID) -> list[DocumentSummary]:
        summaries = []
        for doc in self.documents.values():
            if doc.collection_id != collection_id:
                continue
            pages = {
                block.page
                for block in self.blocks.values()
                if block.document_id == doc.id and block.page is not None
            }
            summaries.append(
                DocumentSummary(
                    id=doc.id,
                    collection_id=doc.collection_id,
                    filename=doc.filename,
                    doc_type=doc.doc_type,
                    sha256=doc.sha256,
                    ingested_at=self.document_ingested_at.get(doc.id),
                    pages=len(pages) or 1,
                )
            )
        return sorted(
            summaries,
            key=lambda s: s.ingested_at or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )

    async def create_run(
        self, *, collection_id: UUID, run_id: UUID, idempotency_key: str, trigger: str = "api"
    ) -> tuple[UUID, bool]:
        key = (collection_id, idempotency_key)
        async with self._lock:
            existing = self.runs.get(key)
            if existing is not None:
                return existing, True
            self.runs[key] = run_id
            self.run_status[run_id] = "running"
            self.run_collection[run_id] = collection_id
            self.run_started[run_id] = datetime.now(UTC)
            self.run_trigger[run_id] = trigger
            return run_id, False

    async def find_run_by_idempotency_key(
        self, collection_id: UUID, idempotency_key: str
    ) -> UUID | None:
        return self.runs.get((collection_id, idempotency_key))

    async def block_run(self, run_id: UUID, reason: str) -> None:
        self.run_status[run_id] = "blocked"
        self.run_ended[run_id] = datetime.now(UTC)

    async def fail_run(self, run_id: UUID, reason: str) -> None:
        self.run_status[run_id] = "failed"
        self.run_ended[run_id] = datetime.now(UTC)

    async def set_run_trigger_document(self, run_id: UUID, document_id: UUID) -> None:
        self.run_trigger_document[run_id] = document_id

    async def set_run_ruleset(self, run_id: UUID, ruleset_id: UUID) -> None:
        self.run_ruleset[run_id] = ruleset_id

    async def put_document(self, document: Document) -> tuple[Document, bool]:
        key = (document.collection_id, document.sha256)
        async with self._lock:
            existing_id = self.document_by_sha.get(key)
            if existing_id:
                return self.documents[existing_id], True
            self.documents[document.id] = document
            self.document_by_sha[key] = document.id
            self.document_ingested_at[document.id] = datetime.now(UTC)
            return document, False

    async def get_document(self, document_id: UUID) -> Document | None:
        return self.documents.get(document_id)

    async def set_document_type(
        self, document_id: UUID, doc_type: str, confidence: float | None
    ) -> None:
        document = self.documents[document_id]
        document.doc_type = doc_type  # type: ignore[assignment]
        document.doc_type_confidence = confidence

    async def link_supersession(self, document_id: UUID, supersedes_id: UUID) -> None:
        self.supersedes[document_id] = supersedes_id

    async def put_document_relations(self, relations: list[DocumentRelation]) -> None:
        async with self._lock:
            for relation in relations:
                stored = self.relations[relation.document_id]
                identity = (relation.kind, relation.target_ref, relation.block_id,
                            relation.quote_start)
                # Replay-safe: the same claim re-read from the same block is one relation.
                if any(
                    (other.kind, other.target_ref, other.block_id, other.quote_start)
                    == identity
                    for other in stored
                ):
                    continue
                stored.append(relation)

    async def list_document_relations(self, document_id: UUID) -> list[DocumentRelation]:
        return list(self.relations.get(document_id, []))

    async def documents_by_agreement_ref(self, collection_id: UUID) -> dict[str, UUID]:
        return {
            document.agreement_ref: document_id
            for document_id, document in self.documents.items()
            if document.collection_id == collection_id and document.agreement_ref
        }

    async def set_agreement_ref(self, document_id: UUID, agreement_ref: str) -> None:
        self.documents[document_id].agreement_ref = agreement_ref

    async def find_supersession_target(
        self, collection_id: UUID, exclude_document_id: UUID
    ) -> UUID | None:
        candidates = [
            document_id
            for document_id, document in self.documents.items()
            if document.collection_id == collection_id
            and document_id != exclude_document_id
            and document.doc_type in {"master_agreement", "sow"}
        ]
        return candidates[-1] if candidates else None

    async def put_blocks(self, blocks: list[Block]) -> None:
        for block in blocks:
            self.blocks[block.id] = block

    async def get_blocks(self, document_id: UUID) -> dict[UUID, Block]:
        return {
            block_id: block
            for block_id, block in self.blocks.items()
            if block.document_id == document_id
        }

    async def get_blocks_by_ids(self, block_ids: list[UUID]) -> dict[UUID, Block]:
        return {
            block_id: self.blocks[block_id] for block_id in block_ids if block_id in self.blocks
        }

    async def put_facts(
        self, collection_id: UUID, facts: list[FactCandidate]
    ) -> list[FactCandidate]:
        inserted: list[FactCandidate] = []
        async with self._lock:
            for fact in facts:
                key = (collection_id, fact.fingerprint)
                existing = self.facts_by_fingerprint.get(key)
                if existing is None:
                    fact.id = fact.id or uuid4()
                    self.facts_by_fingerprint[key] = fact
                    inserted.append(fact)
                else:
                    # Replay: the caller's copy adopts the stored identity.
                    fact.id = existing.id
                    inserted.append(existing)
        return inserted

    async def get_active_facts(self, collection_id: UUID, keys: list[str]) -> list[StoredFact]:
        wanted = set(keys)
        stored: list[StoredFact] = []
        for (cid, _), fact in self.facts_by_fingerprint.items():
            if cid != collection_id or fact.key not in wanted or fact.id is None:
                continue
            document_id = self.blocks[fact.block_id].document_id
            document = self.documents[document_id]
            stored.append(
                StoredFact(
                    id=fact.id,
                    key=fact.key,
                    value=fact.value,
                    fingerprint=fact.fingerprint,
                    quote=fact.quote,
                    document_id=document_id,
                    block_id=fact.block_id,
                    doc_type=document.doc_type,
                    supersedes_id=self.supersedes.get(document_id),
                    scope=fact.scope,
                    evidence=fact.evidence,
                )
            )
        return stored

    async def get_facts_by_ids(self, fact_ids: list[UUID]) -> dict[UUID, StoredFact]:
        wanted = set(fact_ids)
        found: dict[UUID, StoredFact] = {}
        for fact in self.facts_by_fingerprint.values():
            if fact.id is None or fact.id not in wanted:
                continue
            document_id = self.blocks[fact.block_id].document_id
            document = self.documents[document_id]
            found[fact.id] = StoredFact(
                id=fact.id,
                key=fact.key,
                value=fact.value,
                fingerprint=fact.fingerprint,
                quote=fact.quote,
                document_id=document_id,
                block_id=fact.block_id,
                doc_type=document.doc_type,
                supersedes_id=self.supersedes.get(document_id),
                scope=fact.scope,
                evidence=fact.evidence,
            )
        return found

    async def affected_register_keys(
        self, collection_id: UUID, scoped_keys: list[str], document_id: UUID
    ) -> list[str]:
        affected = set(scoped_keys)
        superseded_id = self.supersedes.get(document_id)
        if superseded_id is not None:
            # Reverse citation index: any register item grounded in the superseded
            # document has to be re-derived, even if this document never mentions its key.
            # The invalidated row carries its own agreement, so amending one agreement
            # cannot drag another agreement's row into this run's affected set.
            superseded_fact_ids = {
                fact.id
                for (cid, _), fact in self.facts_by_fingerprint.items()
                if cid == collection_id
                and fact.id is not None
                and self.blocks[fact.block_id].document_id == superseded_id
            }
            for (cid, scoped), item in self.register.items():
                if cid == collection_id and superseded_fact_ids.intersection(
                    item.citation_fact_ids
                ):
                    affected.add(scoped)
        return sorted(affected)

    async def get_register_items(
        self, collection_id: UUID, scoped_keys: list[str]
    ) -> dict[str, RegisterItem]:
        return {
            scoped: item
            for (cid, scoped), item in self.register.items()
            if cid == collection_id and scoped in set(scoped_keys)
        }

    async def put_conflicts(self, conflicts: list[Conflict]) -> list[Conflict]:
        stored: list[Conflict] = []
        async with self._lock:
            for conflict in conflicts:
                existing = next(
                    (
                        candidate
                        for candidate in self.conflicts.values()
                        if (
                            candidate.collection_id,
                            candidate.key,
                            candidate.fact_a_id,
                            candidate.fact_b_id,
                        )
                        == (
                            conflict.collection_id,
                            conflict.key,
                            conflict.fact_a_id,
                            conflict.fact_b_id,
                        )
                    ),
                    None,
                )
                if existing is not None:
                    stored.append(existing)
                    continue
                self.conflicts[conflict.id] = conflict
                stored.append(conflict)
        return stored

    async def list_conflicts(
        self, collection_id: UUID, *, state: str | None = "open"
    ) -> list[Conflict]:
        return [
            conflict
            for conflict in self.conflicts.values()
            if conflict.collection_id == collection_id
            and (state is None or conflict.state == state)
        ]

    async def put_ruleset(self, ruleset: Ruleset) -> Ruleset:
        async with self._lock:
            existing = next(
                (
                    candidate
                    for candidate in self.rulesets.values()
                    if (candidate.collection_id, candidate.name, candidate.version)
                    == (ruleset.collection_id, ruleset.name, ruleset.version)
                ),
                None,
            )
            if existing is not None:
                return existing
            ruleset.id = ruleset.id or uuid4()
            for rule in ruleset.rules:
                rule.id = rule.id or uuid4()
            self.rulesets[ruleset.id] = ruleset
            return ruleset

    async def get_active_ruleset(self, collection_id: UUID) -> Ruleset | None:
        candidates = [
            ruleset for ruleset in self.rulesets.values() if ruleset.collection_id == collection_id
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda ruleset: ruleset.version)

    async def get_ruleset(self, ruleset_id: UUID) -> Ruleset | None:
        return self.rulesets.get(ruleset_id)

    async def put_findings(self, findings: list[Finding]) -> list[Finding]:
        stored: list[Finding] = []
        async with self._lock:
            for finding in findings:
                existing = next(
                    (
                        candidate
                        for candidate in self.findings.values()
                        if (
                            candidate.run_id,
                            candidate.rule_id,
                            candidate.target_kind,
                            candidate.target_id,
                        )
                        == (
                            finding.run_id,
                            finding.rule_id,
                            finding.target_kind,
                            finding.target_id,
                        )
                    ),
                    None,
                )
                if existing is not None:
                    # Replayed stage: keep the row already recorded.
                    stored.append(existing)
                    continue
                finding.id = finding.id or uuid4()
                self.findings[finding.id] = finding
                stored.append(finding)
        return stored

    async def list_findings(self, run_id: UUID) -> list[Finding]:
        return [finding for finding in self.findings.values() if finding.run_id == run_id]

    async def record_finding_decisions(
        self, run_id: UUID, actor_id: str, decisions: dict[UUID, str]
    ) -> list[Finding]:
        decided: list[Finding] = []
        async with self._lock:
            for finding_id, decision in decisions.items():
                finding = self.findings.get(finding_id)
                if finding is None or finding.run_id != run_id:
                    raise ValueError(f"finding {finding_id} does not belong to run {run_id}")
                if decision not in {"upheld", "dismissed"}:
                    raise ValueError(f"invalid review decision: {decision}")
                finding.review_decision = decision
                finding.decided_by = actor_id
                # The verdict, its rationale and its citations are untouched. A dismissal
                # is a recorded disagreement, and the next run re-earns the verdict rather
                # than inheriting the dismissal.
                finding.recheck_required = decision == "dismissed" and finding.adverse
                decided.append(finding)
        return decided

    async def drop_source_rule_cache(self, cache_key: str) -> None:
        self.source_rule_cache.pop(cache_key, None)

    async def get_source_rule_cache(self, cache_key: str) -> UUID | None:
        return self.source_rule_cache.get(cache_key)

    async def put_source_rule_cache(
        self,
        cache_key: str,
        *,
        collection_id: UUID,
        run_id: UUID,
        document_sha256: str,
        ruleset_hash: str,
        evaluator_version: str,
    ) -> None:
        del collection_id, document_sha256, ruleset_hash, evaluator_version  # audit columns
        # First writer wins: a later run that re-evaluated the same triple reached the
        # same verdicts, so there is nothing to gain by repointing the key.
        self.source_rule_cache.setdefault(cache_key, run_id)

    # ------------------------------------------------------- exactly-once bookkeeping

    async def record_stage(
        self,
        run_id: UUID,
        stage: str,
        *,
        input_hash: str,
        output_hash: str | None = None,
        status: str = "completed",
    ) -> None:
        async with self._lock:
            key = (run_id, stage, input_hash)
            existing = self.stage_ledger.get(key)
            if existing is None:
                self.stage_ledger[key] = StageRecord(
                    run_id=run_id,
                    stage=stage,
                    input_hash=input_hash,
                    output_hash=output_hash,
                    status=status,
                )
                return
            # A replay is recorded as a replay. Keeping the first output is deliberate:
            # the ledger is a record of what the run actually produced, and a second
            # execution quietly rewriting it is the thing it exists to make visible.
            # Finishing an execution that already announced itself is not a second
            # execution; re-entering the stage after a crash is.
            if not (existing.status == "started" and status == "completed"):
                existing.attempts += 1
            existing.status = status
            if existing.output_hash is None:
                existing.output_hash = output_hash

    async def stage_record(
        self, run_id: UUID, stage: str, input_hash: str
    ) -> StageRecord | None:
        return self.stage_ledger.get((run_id, stage, input_hash))

    async def list_stages(self, run_id: UUID) -> list[StageRecord]:
        return [record for record in self.stage_ledger.values() if record.run_id == run_id]

    async def acquire_run_lease(self, run_id: UUID, owner: str, *, ttl_seconds: int) -> bool:
        async with self._lock:
            held = self.leases.get(run_id)
            now = datetime.now(UTC)
            if held is not None and held[0] != owner and held[1] > now:
                return False
            self.leases[run_id] = (owner, now + timedelta(seconds=ttl_seconds))
            return True

    async def release_run_lease(self, run_id: UUID, owner: str) -> None:
        async with self._lock:
            if self.leases.get(run_id, (None, None))[0] == owner:
                self.leases.pop(run_id, None)

    async def add_event(self, event: RunEvent) -> None:
        self.events[event.run_id].append(event)

    async def list_events(self, run_id: UUID) -> list[RunEvent]:
        return list(self.events.get(run_id, []))

    async def add_review_items(self, items: list[ReviewItem]) -> None:
        async with self._lock:
            for item in items:
                self.review_items.setdefault(item.id, item)

    async def list_review_items(self, run_id: UUID) -> list[ReviewItem]:
        return [item for item in self.review_items.values() if item.run_id == run_id]

    async def decide_review_items(
        self, run_id: UUID, actor_id: str, decisions: dict[UUID, str]
    ) -> list[ReviewItem]:
        decided: list[ReviewItem] = []
        async with self._lock:
            for item_id, decision in decisions.items():
                item = self.review_items[item_id]
                if item.run_id != run_id:
                    raise ValueError("review item belongs to a different run")
                if item.state != "pending":
                    # A replayed node re-sending its own decision is not a second
                    # decision; anything else lost the compare-and-set.
                    if item.state == decision and item.decided_by == actor_id:
                        decided.append(item)
                        continue
                    raise ItemAlreadyDecidedError(f"review item {item_id} was already decided")
                if decision not in {"approved", "rejected"}:
                    raise ValueError(f"invalid decision: {decision}")
                item.state = decision  # type: ignore[assignment]
                item.decided_by = actor_id
                decided.append(item)
        return decided

    async def commit_approved(
        self, collection_id: UUID, run_id: UUID, *, basis_hash: str
    ) -> CommitResult:
        result = CommitResult()
        committed = result.committed
        ledger_key = (run_id, "commit_approved", basis_hash)
        async with self._lock:
            done = self.stage_ledger.get(ledger_key)
            if done is not None and done.status == "completed":
                # This run already committed this exact deliverable. The loop below would
                # find every content hash unchanged and no-op -- but only by luck of the
                # hashes still matching, and a concurrent commit is exactly the case where
                # they do not. The ledger answers outright, and returns what was written.
                done.attempts += 1
                keys = {
                    RegisterKey.parse(review.target_key).text
                    for review in self.review_items.values()
                    if review.run_id == run_id
                    and review.state == "approved"
                    and review.kind in {"register_update", "conflict"}
                }
                result.committed = [
                    item
                    for (cid, scoped), item in self.register.items()
                    if cid == collection_id and scoped in keys
                ]
                return result
        self.run_status[run_id] = "committed"
        self.run_ended[run_id] = datetime.now(UTC)
        async with self._lock:
            for review in self.review_items.values():
                if review.run_id != run_id or review.state != "approved":
                    continue
                # A finding's decision was recorded at the gate that made it, by
                # `record_finding_decisions`. Writing it here too would mean a run that
                # never reached commit -- blocked, stale, refused -- lost every decision
                # a human had already made.
                if review.kind not in {"register_update", "conflict"}:
                    continue
                after = review.payload["after"]
                value = after["value"]
                state = after.get("state", "supported")
                fingerprints = after.get("citation_fingerprints", [])
                fact_ids = [UUID(str(value)) for value in after.get("citation_fact_ids", [])]
                scoped = RegisterKey.parse(review.target_key)
                existing = self.register.get((collection_id, scoped.text))
                content_hash = register_content_hash(
                    value=value, evidence_fingerprints=fingerprints, state=state
                )
                if existing is not None and existing.content_hash == content_hash:
                    # Nothing changed: no version bump, no rewrite.
                    committed.append(existing)
                    continue

                stale = stale_proposal(
                    review.target_key,
                    review.payload,
                    actual_version=existing.version if existing else None,
                    actual_hash=existing.content_hash if existing else None,
                )
                if stale is not None:
                    # Derived from a version that has since moved: refuse rather than
                    # overwrite a value this approval never saw.
                    result.stale.append(stale)
                    continue
                item = RegisterItem(
                    collection_id=collection_id,
                    key=scoped.key,
                    value=value,
                    state=state,
                    content_hash=content_hash,
                    version=1 if existing is None else existing.version + 1,
                    id=existing.id if existing else uuid4(),
                    citation_fact_ids=fact_ids,
                    citation_fingerprints=list(fingerprints),
                    agreement_id=scoped.agreement_id,
                )
                self.register[(collection_id, scoped.text)] = item
                committed.append(item)
                self.change_log[run_id].append(
                    RegisterChange(
                        run_id=run_id,
                        register_item_id=item.id,
                        key=scoped.text,
                        old_value=existing.value if existing else None,
                        new_value=value,
                        old_hash=existing.content_hash if existing else None,
                        new_hash=content_hash,
                        changed_at=datetime.now(UTC),
                    )
                )

                conflict = review.payload.get("conflict")
                if conflict:
                    stored = self.conflicts.get(UUID(conflict["id"]))
                    if stored is not None:
                        stored.state = "accepted"

            # Written under the same lock as the register mutations above, so a replay
            # cannot observe a committed register without the ledger row that records it.
            self.stage_ledger[ledger_key] = StageRecord(
                run_id=run_id,
                stage="commit_approved",
                input_hash=basis_hash,
                output_hash=register_content_hash(
                    value={"committed": sorted(item.register_key.text for item in committed)},
                    evidence_fingerprints=[],
                    state="committed",
                ),
            )
        return result

    async def list_register(self, collection_id: UUID) -> list[RegisterItem]:
        return sorted(
            [item for (cid, _), item in self.register.items() if cid == collection_id],
            key=lambda item: (item.agreement_id, item.key),
        )

    async def get_run(self, run_id: UUID) -> RunSummary | None:
        status = self.run_status.get(run_id)
        collection_id = self.run_collection.get(run_id)
        started_at = self.run_started.get(run_id)
        if status is None or collection_id is None or started_at is None:
            return None
        return RunSummary(
            run_id=run_id,
            collection_id=collection_id,
            status=status,  # type: ignore[arg-type]
            started_at=started_at,
            ended_at=self.run_ended.get(run_id),
            trigger=self.run_trigger.get(run_id, "api"),
            trigger_document_id=self.run_trigger_document.get(run_id),
            ruleset_id=self.run_ruleset.get(run_id),
        )

    async def list_run_changes(self, run_id: UUID) -> list[RegisterChange]:
        return list(self.change_log.get(run_id, []))

    async def list_runs(
        self, collection_id: UUID, *, status: str | None = None
    ) -> list[RunSummary]:
        run_ids = [
            run_id
            for (cid, _key), run_id in self.runs.items()
            if cid == collection_id
            and (status is None or self.run_status.get(run_id) == status)
        ]
        summaries = [await self.get_run(run_id) for run_id in run_ids]
        return sorted(
            (summary for summary in summaries if summary is not None),
            key=lambda summary: summary.started_at,
            reverse=True,
        )

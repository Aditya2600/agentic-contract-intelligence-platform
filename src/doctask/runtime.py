from __future__ import annotations

import asyncio
import os
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from typing import Any
from uuid import UUID, uuid4

from langgraph.types import Command

from doctask.auth import Principal, require_reviewer
from doctask.config import settings
from doctask.domain import ReviewItem, Ruleset, RunEvent
from doctask.graph.builder import build_graph
from doctask.graph.nodes import NodeDependencies
from doctask.llm.base import ModelGateway
from doctask.llm.fake import FakeLLM
from doctask.repositories.base import Repository
from doctask.services.cost_report import build_run_cost_report
from doctask.services.extraction import ExtractedDocument, extract_document
from doctask.services.hashing import stage_input_hash
from doctask.services.rules import parse_ruleset, ruleset_content_hash


@dataclass(slots=True)
class Services:
    repository: Repository
    graph: Any
    # Also the OCR fallback for uploads: the same gateway, used on rendered pages.
    model: ModelGateway | None = None
    close: Any = None


def build_model() -> ModelGateway:
    if settings.llm != "gateway":
        return FakeLLM()

    from doctask.llm.gateway import OpenAICompatibleGateway

    if not settings.llm_api_key or not settings.llm_model:
        raise RuntimeError(
            "DOCTASK_LLM=gateway needs DOCTASK_LLM_API_KEY and DOCTASK_LLM_MODEL"
        )
    return OpenAICompatibleGateway(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        timeout=settings.llm_timeout,
        vision_model=settings.vlm_model,
    )


_services: Services | None = None
_init_lock = asyncio.Lock()


async def _build_services() -> Services:
    model = build_model()

    if settings.repository == "postgres":
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from psycopg.rows import dict_row
        from psycopg_pool import AsyncConnectionPool

        from doctask.repositories.postgres import PostgresRepository

        repository = PostgresRepository(settings.database_url)
        await repository.open()

        # The checkpointer needs autocommit + dict rows, so it gets its own pool.
        checkpoint_pool = AsyncConnectionPool(
            settings.database_url,
            open=False,
            kwargs={"autocommit": True, "row_factory": dict_row},
        )
        await checkpoint_pool.open()
        checkpointer = AsyncPostgresSaver(checkpoint_pool)
        await checkpointer.setup()

        async def close() -> None:
            await checkpoint_pool.close()
            await repository.close()
            await _close_model(model)

        return Services(
            repository=repository,
            graph=build_graph(
                NodeDependencies(repository=repository, model=model),
                checkpointer=checkpointer,
            ),
            model=model,
            close=close,
        )

    from doctask.repositories.memory import InMemoryRepository

    repository = InMemoryRepository()

    async def close_memory() -> None:
        await _close_model(model)

    return Services(
        repository=repository,
        graph=build_graph(NodeDependencies(repository=repository, model=model)),
        model=model,
        close=close_memory,
    )


async def _close_model(model: ModelGateway) -> None:
    aclose = getattr(model, "aclose", None)
    if aclose is not None:
        await aclose()


async def get_services() -> Services:
    global _services
    if _services is None:
        async with _init_lock:
            if _services is None:
                _services = await _build_services()
    return _services


async def shutdown_services() -> None:
    global _services
    if _services is not None and _services.close is not None:
        await _services.close()
    _services = None


async def start_run(
    *,
    collection_id: UUID,
    idempotency_key: str,
    filename: str,
    mime_type: str,
    text: str,
    blocks: list[dict] | None = None,
    extraction_warnings: list[str] | None = None,
    trigger: str = "api",
    principal: Principal | None = None,
) -> tuple[UUID, dict]:
    """Start one run over one document -- the only place a run is created.

    `trigger` records what put the run here (`api`, `mcp`, `watcher`) and `principal`
    records who, in the run's own event log, before any node executes. Neither is a
    permission: a run started by a service is exactly as constrained at the human gates
    as one started by a person, because those gates read the resume payload's
    credential and nothing else.
    """
    services = await get_services()
    run_id, duplicate = await services.repository.create_run(
        collection_id=collection_id,
        run_id=uuid4(),
        idempotency_key=idempotency_key,
        trigger=trigger,
    )
    if duplicate:
        # Same idempotency key: return the first run, spend nothing.
        return run_id, {"status": "duplicate_run", "run_id": str(run_id)}

    if principal is not None:
        # First event of the run, written before the graph moves, so the log reads in
        # the order things happened rather than carrying the origin as a footnote.
        await services.repository.add_event(
            RunEvent(
                run_id=run_id,
                stage="trigger",
                decision="continue",
                reason=f"{trigger} run started by {principal.actor_id} ({principal.role})",
                next_node="ingest",
            )
        )

    config = {"configurable": {"thread_id": str(run_id)}}
    async with run_lease(services.repository, run_id):
        result = await services.graph.ainvoke(
            {
                "run_id": str(run_id),
                "collection_id": str(collection_id),
                "idempotency_key": idempotency_key,
                "input_document": {
                    "filename": filename,
                    "mime_type": mime_type,
                    "text": text,
                    # Present for uploads: page and extraction method per block, which
                    # plain text has no way to carry.
                    "blocks": blocks or [],
                },
                "extraction_warnings": extraction_warnings or [],
                "validation_attempt": 0,
                "status": "running",
            },
            config=config,
        )

    document_id = result.get("document_id") if isinstance(result, dict) else None
    if document_id:
        # The document this run was started over, recorded once ingest has actually
        # stored it. Written after the fact because the id does not exist before then:
        # the bytes are addressed by content hash, and the row id is assigned by
        # whichever insert won.
        await services.repository.set_run_trigger_document(run_id, UUID(document_id))
    ruleset_id = result.get("ruleset_id") if isinstance(result, dict) else None
    if ruleset_id:
        # What `pin_ruleset` actually resolved, recorded once so a later export of this
        # run reads what it was judged against rather than whatever is active today.
        await services.repository.set_run_ruleset(run_id, UUID(ruleset_id))
    return run_id, result


async def ingest_file(
    *,
    collection_id: UUID,
    idempotency_key: str,
    filename: str,
    mime_type: str,
    data: bytes,
    trigger: str = "api",
    principal: Principal | None = None,
) -> tuple[UUID, ExtractedDocument, dict]:
    """Turn a file's bytes into a run. The one path from a file to the graph.

    `POST /runs/upload` is this function plus HTTP status codes; the collection watcher
    is this function plus a directory scan. Neither has an ingestion path of its own,
    so a change to how a file becomes a run cannot apply to one and miss the other.

    Extraction happens before the run exists, which is deliberate and is why
    `ExtractionError` propagates to the caller rather than being turned into a run: a
    file this server cannot read never becomes a run at all, because ingesting it as
    empty text would produce a register that looks clean because the evidence was
    missing. What the caller does with that refusal is the caller's business -- the API
    answers 422, the watcher records the attempt so it stops re-reading the file.
    """
    services = await get_services()
    extracted = await extract_document(
        filename=filename, mime_type=mime_type, data=data, ocr=services.model
    )
    run_id, result = await start_run(
        collection_id=collection_id,
        idempotency_key=idempotency_key,
        filename=filename,
        mime_type=mime_type,
        text=extracted.text,
        blocks=[asdict(block) for block in extracted.blocks],
        extraction_warnings=extracted.warnings,
        trigger=trigger,
        principal=principal,
    )
    # OCR ran before `run_id` existed, so it could not log itself against one. Replayed
    # here, once it does, as the external-service calls the cost report reads: each page
    # a vision model actually transcribed, at the cost and duration that call actually
    # took, attributed to `ingest` -- the stage the pipeline's regular event log already
    # narrates this document's extraction under.
    for call in extracted.ocr_calls:
        await services.repository.add_event(
            RunEvent(
                run_id=run_id,
                stage="ingest",
                decision="external_call",
                reason=f"page {call['page']} read by vision model {call['model']}",
                next_node="ingest",
                duration_ms=call["duration_ms"],
                tokens_in=call["tokens_in"],
                tokens_out=call["tokens_out"],
                model=call["model"],
                external_service="ocr",
            )
        )
    return run_id, extracted, result


async def rederive_run(
    *, collection_id: UUID, stale_run_id: UUID, idempotency_key: str
) -> tuple[UUID, dict]:
    """Start a new run that redoes what `stale_run_id` approved but never committed.

    No document travels with it: the bytes are already stored, and re-uploading them
    would short-circuit on the SHA-256 rather than re-derive anything.
    """
    services = await get_services()
    run_id, duplicate = await services.repository.create_run(
        collection_id=collection_id, run_id=uuid4(), idempotency_key=idempotency_key
    )
    if duplicate:
        return run_id, {"status": "duplicate_run", "run_id": str(run_id)}

    async with run_lease(services.repository, run_id):
        result = await services.graph.ainvoke(
            {
                "run_id": str(run_id),
                "collection_id": str(collection_id),
                "idempotency_key": idempotency_key,
                "rederive_from_run": str(stale_run_id),
                "validation_attempt": 0,
                "status": "running",
            },
            config={"configurable": {"thread_id": str(run_id)}},
        )
    ruleset_id = result.get("ruleset_id") if isinstance(result, dict) else None
    if ruleset_id:
        await services.repository.set_run_ruleset(run_id, UUID(ruleset_id))
    return run_id, result


class RunBusyError(RuntimeError):
    """Another process is already driving this run's LangGraph thread."""


# One driver at a time, for at most this long. Long enough that a slow model call inside
# a node does not lose the lease mid-flight; short enough that a SIGKILLed process does
# not lock its own run out of the resume that would recover it.
LEASE_TTL_SECONDS = 300


@asynccontextmanager
async def run_lease(repository: Repository, run_id: UUID) -> AsyncIterator[str]:
    """Hold the exclusive right to advance this run, or refuse.

    `thread_id = run_id` makes resume addressable by anyone who knows the run, which is
    the right design and also means a retrying HTTP client, a watcher and a human can all
    resume the same run at once. They then read the same checkpoint and execute the same
    nodes. The domain writes survive that -- they are idempotent, and the ledger records
    it -- but the human gates do not: two processes can each interrupt and each be
    answered, and the run ends up with two answers to one question.
    """
    owner = f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:8]}"
    if not await repository.acquire_run_lease(run_id, owner, ttl_seconds=LEASE_TTL_SECONDS):
        raise RunBusyError(f"run {run_id} is already being advanced by another process")
    try:
        yield owner
    finally:
        # Released even when the node raised. A crash that skips this leaves the lease to
        # expire instead, which is why it has a TTL at all.
        await repository.release_run_lease(run_id, owner)


class RunNotFoundError(LookupError):
    """No run exists under this id."""


class RunClosedError(RuntimeError):
    """The run already left every gate a decision could apply to.

    `blocked`, `committed` and `failed` are the only statuses this repository ever
    actually writes (`domain.RunStatus` also names `committing`/`cancelled`, but
    nothing writes them). Each is the end of the graph's own run: the checkpoint has
    no pending interrupt left to resume, so a second decision call has nothing to
    apply to -- refused here, before a resume is attempted against a thread that
    cannot answer it, rather than surfacing whatever LangGraph does with that.
    """

    def __init__(self, run_id: UUID, status: str):
        super().__init__(f"run {run_id} is already {status}; there is nothing left to decide")
        self.run_id = run_id
        self.status = status


_CLOSED_STATUSES = frozenset({"blocked", "committed", "failed"})


async def resume_run(run_id: UUID, payload: dict, *, principal: Principal) -> dict:
    """Answer a human gate: item decisions, or a blocker override.

    Every caller comes through here, which is the point: the actor is the authenticated
    principal and is stamped over whatever the payload claimed, so `decided_by` records
    who presented a credential rather than who said they were. And exactly one caller
    advances the thread at a time -- see `run_lease`.
    """
    require_reviewer(principal)
    services = await get_services()
    summary = await services.repository.get_run(run_id)
    if summary is None:
        raise RunNotFoundError(f"run {run_id} does not exist")
    if summary.status in _CLOSED_STATUSES:
        raise RunClosedError(run_id, summary.status)
    config = {"configurable": {"thread_id": str(run_id)}}
    async with run_lease(services.repository, run_id):
        return await services.graph.ainvoke(
            Command(resume={**payload, **principal.as_payload()}), config=config
        )


class IdempotencyConflictError(RuntimeError):
    """The same idempotency key was reused for a call with different arguments."""


class StaleDecisionError(RuntimeError):
    """A decision's stated version or basis hash no longer matches what is stored."""


async def _claim_idempotency_key(
    repository: Repository, run_id: UUID, tool: str, idempotency_key: str, *fingerprint: Any
) -> None:
    """Refuse a reused key unless it is describing the exact same call.

    Reuses the stage ledger every node in the graph already ledgers exact-once work
    into (`record_stage`/`stage_record`, keyed on `(run_id, stage, input_hash)`), rather
    than standing up a second dedup table: a resume-tool call is exact-once work in
    precisely the sense that ledger already exists to prove.
    """
    fingerprint_hash = stage_input_hash(*fingerprint)
    existing = await repository.stage_record(run_id, tool, idempotency_key)
    if existing is not None and existing.output_hash != fingerprint_hash:
        raise IdempotencyConflictError(
            f"idempotency key {idempotency_key!r} was already used for a different {tool} call"
        )
    await repository.record_stage(
        run_id, tool, input_hash=idempotency_key, output_hash=fingerprint_hash
    )


def _review_item_basis(item: ReviewItem) -> dict[str, Any]:
    """What a caller has to prove they read before they may decide this item.

    `register_update` / `conflict` / `scope_question` items are a decision about one
    register row: the basis is that row's version and content hash at proposal time,
    already carried in `payload["before"]`. `finding` / `deliverable_confirmation`
    items are a decision about the whole candidate register a deliverable rule was
    judged against: the basis is that run-wide hash, already stamped into every such
    item's payload by `_decision_binding`. `injection_review` items bind to nothing
    mutable -- the block they are about does not change under them -- so they carry no
    basis to check.
    """
    if item.kind in {"register_update", "conflict", "scope_question"}:
        before = item.payload.get("before")
        return {
            "version": before.get("version") if before else None,
            "content_hash": before.get("content_hash") if before else None,
        }
    if item.kind in {"finding", "deliverable_confirmation"}:
        return {
            "basis_hash": item.payload.get("basis_hash"),
            "ruleset_hash": item.payload.get("ruleset_hash"),
        }
    return {}


async def decide_reviewed_items(
    run_id: UUID,
    decisions: dict[UUID, str],
    *,
    basis: dict[UUID, dict[str, Any]],
    idempotency_key: str,
    principal: Principal,
    document_type: str | None = None,
) -> dict:
    """Approve or reject review items -- the one path both REST and MCP call.

    Every decision states, per item, the version and basis hash the caller read it
    under. An item whose basis has moved since -- another reviewer decided it, a
    concurrent run replaced the candidate register -- is refused here, before anything
    is applied, instead of surfacing three stages later as a stale commit nobody
    connects back to this call.
    """
    services = await get_services()
    items = {item.id: item for item in await services.repository.list_review_items(run_id)}
    for item_id in decisions:
        item = items.get(item_id)
        if item is None:
            raise ValueError(f"review item {item_id} does not exist for run {run_id}")
        expected = _review_item_basis(item)
        if expected and basis.get(item_id) != expected:
            raise StaleDecisionError(
                f"review item {item_id} ({item.kind} {item.target_key}) has moved since "
                "it was last read; call list_review_items again and decide against that"
            )
    await _claim_idempotency_key(
        services.repository,
        run_id,
        "decide_review_items",
        idempotency_key,
        sorted((str(item_id), decision) for item_id, decision in decisions.items()),
        document_type,
    )
    return await resume_run(
        run_id,
        {
            "decisions": {str(item_id): decision for item_id, decision in decisions.items()},
            "document_type": document_type,
        },
        principal=principal,
    )


async def override_run_blockers(
    run_id: UUID, *, override: bool, reason: str, idempotency_key: str, principal: Principal
) -> dict:
    """Answer the blocker gate -- the one path both REST and MCP call."""
    services = await get_services()
    await _claim_idempotency_key(
        services.repository, run_id, "override_blockers", idempotency_key, override, reason
    )
    return await resume_run(
        run_id, {"override": override, "reason": reason}, principal=principal
    )


async def get_run_status(run_id: UUID) -> dict | None:
    """What a poller needs, from a run id alone: no lease taken, no graph touched.

    `runs.status` only ever moves to `blocked` or `committed`; a run parked on a human
    gate is still `running` there; the checkpoint that would say `awaiting_review` is
    not something this can read without the lease a live resume needs. Pending review
    items are the same fact from the other table, so that is what stands in for it.
    """
    services = await get_services()
    summary = await services.repository.get_run(run_id)
    if summary is None:
        return None
    items = await services.repository.list_review_items(run_id)
    pending = [item for item in items if item.state == "pending"]
    status = "awaiting_review" if summary.status == "running" and pending else summary.status

    # Where the run actually is, from the last thing it recorded. A node writes its event
    # on the way out, naming where it routed to, so for a run still in flight the stage it
    # is *at* is the last event's `next_node` -- a run parked on a human gate last wrote
    # `assemble_proposals -> await_review`, and `await_review` is the honest answer. A
    # finished run has nowhere left to go, so its last completed stage is the answer.
    events = await services.repository.list_events(run_id)
    last = events[-1] if events else None
    finished = summary.status not in {"running", "committing"}
    return {
        "run_id": str(summary.run_id),
        "collection_id": str(summary.collection_id),
        "status": status,
        "started_at": summary.started_at.isoformat(),
        "ended_at": summary.ended_at.isoformat() if summary.ended_at else None,
        "pending_review_items": len(pending),
        "current_stage": (
            None if last is None else (last.stage if finished else (last.next_node or last.stage))
        ),
        "last_completed_stage": last.stage if last else None,
        "stages_recorded": len(events),
    }


async def get_run_stages(run_id: UUID) -> list[dict]:
    """This run's ordered stage history, from the exactly-once ledger.

    One row per `(stage, input_hash)`: what the stage was given, what it produced, and
    how many times it executed. `attempts > 1` with an unchanged `output_hash` is a
    replay that agreed with itself -- which is what makes a crash recovery checkable
    rather than merely claimed. Distinct from `list_events`, which is the ordered prose
    narrative; this is the keyed record of work.
    """
    services = await get_services()
    if await services.repository.get_run(run_id) is None:
        raise RunNotFoundError(f"run {run_id} does not exist")
    return [asdict(record) | {"run_id": str(record.run_id)}
            for record in await services.repository.list_stages(run_id)]


class RulesetConflictError(RuntimeError):
    """A ruleset upload changed content without proving it knew the current version."""


async def install_ruleset(
    collection_id: UUID, payload: dict, *, if_match: int | None = None
) -> tuple[Ruleset, bool]:
    """Install a playbook once, by what it says -- the one path both REST and MCP call.

    Same content as what is already active is a no-op: it returns the stored ruleset
    unchanged, at its existing version, and writes nothing. Changed content has to name
    the version it is replacing (`if_match`), the same compare-and-swap shape used for a
    review decision's basis -- an upload that never read the current playbook must not
    silently become the next version of one it does not know it is replacing. The
    version returned is always server-assigned; whatever version the payload itself
    claimed is discarded.
    """
    candidate = parse_ruleset(payload, collection_id)
    services = await get_services()
    active = await services.repository.get_active_ruleset(collection_id)
    if active is not None and ruleset_content_hash(active) == ruleset_content_hash(candidate):
        return active, False
    if active is None:
        candidate.version = 1
    else:
        if if_match is None:
            raise RulesetConflictError(
                f"collection already has ruleset {active.name!r} at version "
                f"{active.version}; supply if_match={active.version} to replace it"
            )
        if if_match != active.version:
            raise RulesetConflictError(
                f"if_match={if_match} does not match the current version "
                f"({active.version}); re-fetch it and try again"
            )
        candidate.version = active.version + 1
    stored = await services.repository.put_ruleset(candidate)
    return stored, True


async def export_register(collection_id: UUID, run_id: UUID) -> dict:
    """A collection's register as a self-contained artifact.

    Every row carries its value, its state, and the quotes that support it -- each
    with the document it came from, the page, and the character offsets `validate_
    citations` minted from the stored block rather than from anything a model said --
    so a reader with no database access can check any claim against a source file.
    Named alongside `run_id`, which must belong to `collection_id`: the run this
    export was taken next to, and the ruleset `pin_ruleset` actually pinned for that
    run -- not the collection's currently active one, which may since have moved to a
    newer version. A run that pinned no playbook, or one recorded before this was
    tracked, reports `ruleset: null` rather than guessing. A row citing a fact this
    call cannot find is exported with an empty quote list rather than failing the whole
    export over one missing citation.
    """
    services = await get_services()
    run = await services.repository.get_run(run_id)
    if run is None:
        raise RunNotFoundError(f"run {run_id} does not exist")
    if run.collection_id != collection_id:
        raise RunNotFoundError(f"run {run_id} does not belong to collection {collection_id}")

    register = await services.repository.list_register(collection_id)
    fact_ids = sorted({fact_id for item in register for fact_id in item.citation_fact_ids})
    facts = await services.repository.get_facts_by_ids(fact_ids)

    documents: dict[UUID, Any] = {}
    for fact in facts.values():
        if fact.document_id not in documents:
            document = await services.repository.get_document(fact.document_id)
            if document is not None:
                documents[fact.document_id] = document

    rows = []
    for item in register:
        quotes = []
        for fact_id in item.citation_fact_ids:
            fact = facts.get(fact_id)
            if fact is None:
                continue
            document = documents.get(fact.document_id)
            evidence = fact.evidence
            quotes.append(
                {
                    "quote": fact.quote,
                    "document_id": str(fact.document_id),
                    "document_filename": document.filename if document else None,
                    "document_sha256": (
                        document.sha256
                        if document
                        else (evidence.document_sha256 if evidence else None)
                    ),
                    "page": evidence.page if evidence else None,
                    "block_index": evidence.block_index if evidence else None,
                    "char_start": evidence.char_start if evidence else None,
                    "char_end": evidence.char_end if evidence else None,
                }
            )
        rows.append(
            {
                "agreement_id": item.agreement_id,
                "key": item.key,
                "register_key": item.register_key.text,
                "value": item.value,
                "state": item.state,
                "version": item.version,
                "content_hash": item.content_hash,
                "quotes": quotes,
            }
        )

    ruleset = (
        await services.repository.get_ruleset(run.ruleset_id) if run.ruleset_id else None
    )
    return {
        "collection_id": str(collection_id),
        "run": {
            "run_id": str(run.run_id),
            "status": run.status,
            "started_at": run.started_at.isoformat(),
            "ended_at": run.ended_at.isoformat() if run.ended_at else None,
            "trigger": run.trigger,
        },
        "ruleset": (
            {
                "ruleset_id": str(ruleset.id),
                "name": ruleset.name,
                "version": ruleset.version,
                "rules": [rule.code for rule in ruleset.rules],
            }
            if ruleset is not None
            else None
        ),
        "register": rows,
    }


async def get_run_cost_report(run_id: UUID) -> dict:
    """What this run cost and where the time went -- callable any time after the fact.

    Reads only what the pipeline already wrote: `run_events` for the time, tokens and
    model behind every stage, `run_stage_ledger` for which of those executions the
    exact-once ledger can prove were a crash replay rather than a new attempt. The same
    aggregation also runs inline, still mid-graph, as the `cost` key on the report
    `snapshot_diff_report` builds -- this is the version a caller reaches independently
    of that, for a run that finished a session ago.
    """
    services = await get_services()
    run = await services.repository.get_run(run_id)
    if run is None:
        raise RunNotFoundError(f"run {run_id} does not exist")
    events = await services.repository.list_events(run_id)
    stages = await services.repository.list_stages(run_id)
    report = build_run_cost_report(events, stages)
    report["run_id"] = str(run_id)
    report["collection_id"] = str(run.collection_id)
    report["status"] = run.status
    return report

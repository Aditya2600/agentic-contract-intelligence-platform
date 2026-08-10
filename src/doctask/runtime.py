from __future__ import annotations

import asyncio
import os
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from langgraph.types import Command

from doctask.auth import Principal, require_reviewer
from doctask.config import settings
from doctask.domain import ReviewItem, Ruleset
from doctask.graph.builder import build_graph
from doctask.graph.nodes import NodeDependencies
from doctask.llm.base import ModelGateway
from doctask.llm.fake import FakeLLM
from doctask.repositories.base import Repository
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
) -> tuple[UUID, dict]:
    services = await get_services()
    run_id, duplicate = await services.repository.create_run(
        collection_id=collection_id, run_id=uuid4(), idempotency_key=idempotency_key
    )
    if duplicate:
        # Same idempotency key: return the first run, spend nothing.
        return run_id, {"status": "duplicate_run", "run_id": str(run_id)}

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
    return run_id, result


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


async def resume_run(run_id: UUID, payload: dict, *, principal: Principal) -> dict:
    """Answer a human gate: item decisions, or a blocker override.

    Every caller comes through here, which is the point: the actor is the authenticated
    principal and is stamped over whatever the payload claimed, so `decided_by` records
    who presented a credential rather than who said they were. And exactly one caller
    advances the thread at a time -- see `run_lease`.
    """
    require_reviewer(principal)
    services = await get_services()
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
    return {
        "run_id": str(summary.run_id),
        "collection_id": str(summary.collection_id),
        "status": status,
        "started_at": summary.started_at.isoformat(),
        "ended_at": summary.ended_at.isoformat() if summary.ended_at else None,
        "pending_review_items": len(pending),
    }


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

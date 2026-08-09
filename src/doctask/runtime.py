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
from doctask.graph.builder import build_graph
from doctask.graph.nodes import NodeDependencies
from doctask.llm.base import ModelGateway
from doctask.llm.fake import FakeLLM
from doctask.repositories.base import Repository


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

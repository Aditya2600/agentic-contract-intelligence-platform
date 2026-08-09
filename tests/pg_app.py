"""Shared Postgres wiring for the integration tests.

One `App` is one process's worth of state: its own repository pool, its own
checkpointer pool, its own compiled graph. Two `App`s against one database are
what two processes look like to Postgres, and throwing one away and building
another is what a restart looks like.
"""

from __future__ import annotations

import os
from typing import Any
from uuid import UUID

import psycopg
import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from doctask.auth import REVIEWER, Principal
from doctask.graph.builder import build_graph
from doctask.graph.nodes import NodeDependencies
from doctask.llm.fake import FakeLLM
from doctask.repositories.postgres import PostgresRepository

DATABASE_URL = os.getenv("DOCTASK_TEST_DATABASE_URL")

requires_postgres = pytest.mark.skipif(
    not DATABASE_URL, reason="set DOCTASK_TEST_DATABASE_URL to run Postgres integration tests"
)


class App:
    def __init__(self) -> None:
        self.repository = PostgresRepository(DATABASE_URL)
        self.pool = AsyncConnectionPool(
            DATABASE_URL, open=False, kwargs={"autocommit": True, "row_factory": dict_row}
        )

    async def open(self) -> App:
        await self.repository.open()
        await self.pool.open()
        checkpointer = AsyncPostgresSaver(self.pool)
        await checkpointer.setup()
        self.graph = build_graph(
            NodeDependencies(repository=self.repository, model=FakeLLM()),
            checkpointer=checkpointer,
        )
        return self

    async def close(self) -> None:
        await self.pool.close()
        await self.repository.close()

    @staticmethod
    def config(run_id: UUID) -> dict:
        # thread_id = run_id is the whole resume story: the checkpoint is addressed
        # by the run, not by the process that happens to be driving it.
        return {"configurable": {"thread_id": str(run_id)}}

    async def start(
        self, *, collection_id: UUID, run_id: UUID, filename: str, text: str
    ) -> dict[str, Any]:
        await self.repository.create_run(
            collection_id=collection_id, run_id=run_id, idempotency_key=f"{filename}-{run_id}"
        )
        return await self.graph.ainvoke(
            {
                "run_id": str(run_id),
                "collection_id": str(collection_id),
                "idempotency_key": f"{filename}-{run_id}",
                "input_document": {
                    "filename": filename,
                    "mime_type": "text/plain",
                    "text": text,
                },
                "validation_attempt": 0,
                "status": "running",
            },
            config=self.config(run_id),
        )

    async def rederive(
        self, *, collection_id: UUID, run_id: UUID, source_run: UUID
    ) -> dict[str, Any]:
        """Start a new run that redoes what `source_run` could not commit. No document."""
        await self.repository.create_run(
            collection_id=collection_id, run_id=run_id, idempotency_key=f"rederive-{run_id}"
        )
        return await self.graph.ainvoke(
            {
                "run_id": str(run_id),
                "collection_id": str(collection_id),
                "idempotency_key": f"rederive-{run_id}",
                "rederive_from_run": str(source_run),
                "validation_attempt": 0,
                "status": "running",
            },
            config=self.config(run_id),
        )

    async def resume(self, run_id: UUID, payload: dict | None = None) -> dict[str, Any]:
        """Resume the run. No payload means "continue from the last checkpoint"."""
        command = None if payload is None else Command(resume=payload)
        return await self.graph.ainvoke(command, config=self.config(run_id))

    async def approve_all(self, run_id: UUID) -> dict[str, Any]:
        pending = [
            item
            for item in await self.repository.list_review_items(run_id)
            if item.state == "pending"
        ]
        return {
            "decisions": {str(item.id): "approved" for item in pending},
            # Stamped by `resume_run` in production; the tests drive the graph directly,
            # so they stamp it themselves. Without it the gate refuses the payload.
            **Principal(actor_id="reviewer-1", role=REVIEWER).as_payload(),
        }

    async def finish(self, run_id: UUID, payload: dict | None = None) -> dict[str, Any]:
        """Answer every remaining human gate with an approval, return the report."""
        result = await self.resume(run_id, payload)
        while "report" not in result:
            result = await self.resume(run_id, await self.approve_all(run_id))
        return result["report"]


async def restart() -> App:
    return await App().open()


async def query(sql: str, *params: Any) -> list[dict]:
    async with await psycopg.AsyncConnection.connect(DATABASE_URL, row_factory=dict_row) as conn:
        cur = await conn.execute(sql, params)
        return await cur.fetchall()


async def stages(run_id: UUID) -> dict[str, dict]:
    """The stage ledger for one run, keyed by stage: attempts, and what it produced."""
    rows = await query(
        "SELECT stage, input_hash, output_hash, status, attempts "
        "FROM run_stage_ledger WHERE run_id = %s ORDER BY created_at, stage",
        run_id,
    )
    return {row["stage"]: row for row in rows}


async def facts(collection_id: UUID) -> tuple[int, int]:
    """(rows, distinct fingerprints) -- equal means nothing wrote a duplicate fact."""
    rows = await query(
        "SELECT count(*) AS rows, count(DISTINCT fact_fingerprint) AS unique_rows "
        "FROM facts WHERE collection_id = %s",
        collection_id,
    )
    return rows[0]["rows"], rows[0]["unique_rows"]

"""One leg of the crash/resume proof, run as a real process that gets SIGKILLed.

Started by `tests/test_crash_resume.py`. It has to be a separate process: SIGKILL
skips `finally` blocks, pool drain and any checkpoint flush, which is exactly the
failure mode the test is trying to prove survivable. Reads a JSON spec file.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from doctask.graph.builder import build_graph
from doctask.graph.nodes import NodeDependencies
from doctask.llm.fake import FakeLLM
from doctask.repositories.postgres import PostgresRepository


class KillAfter:
    """Repository proxy that kills this process once `method` has committed.

    That is the dangerous window: the side effect is durable, the checkpoint that
    records it is not, so a resumed run replays the node that wrote it.
    """

    def __init__(self, inner, method: str) -> None:
        self._inner = inner
        self._method = method

    def __getattr__(self, name: str):
        attr = getattr(self._inner, name)
        if name != self._method:
            return attr

        async def kill_after(*args, **kwargs):
            await attr(*args, **kwargs)
            os.kill(os.getpid(), signal.SIGKILL)

        return kill_after


async def main() -> None:
    spec = json.loads(Path(sys.argv[1]).read_text())
    database_url = spec["database_url"]

    repository = PostgresRepository(database_url)
    await repository.open()
    pool = AsyncConnectionPool(
        database_url, open=False, kwargs={"autocommit": True, "row_factory": dict_row}
    )
    await pool.open()
    checkpointer = AsyncPostgresSaver(pool)
    await checkpointer.setup()
    graph = build_graph(
        NodeDependencies(
            repository=KillAfter(repository, spec["kill_after"]), model=FakeLLM()
        ),
        checkpointer=checkpointer,
    )

    config = {"configurable": {"thread_id": spec["run_id"]}}
    if "input" in spec:
        await repository.create_run(
            collection_id=UUID(spec["collection_id"]),
            run_id=UUID(spec["run_id"]),
            idempotency_key=spec["idempotency_key"],
        )
        await graph.ainvoke(spec["input"], config=config)
    elif spec.get("resume") is None:
        # Plain continue from the last checkpoint: the run is mid-graph, not at a gate.
        # `Command(resume=None)` is not the same thing -- it answers an interrupt with
        # None, which the gate then refuses.
        #
        # A process killed before the first checkpoint leaves the thread with no state at
        # all, and `ainvoke(None)` on an empty thread returns immediately. Re-submitting
        # the original request is what a real retry does, and it is safe: the run id is
        # the thread id, and the ledger tells `ingest` that the document row it is about
        # to find is its own earlier attempt rather than a re-upload.
        snapshot = await graph.aget_state(config)
        await graph.ainvoke(
            None if (snapshot.next or snapshot.values) else spec["retry_input"],
            config=config,
        )
    else:
        await graph.ainvoke(Command(resume=spec["resume"]), config=config)

    raise SystemExit(f"worker reached the end without hitting {spec['kill_after']}")


if __name__ == "__main__":
    asyncio.run(main())
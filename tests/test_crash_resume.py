"""Crash/resume proof: a SIGKILLed run continues on the same run_id.

Skipped unless DOCTASK_TEST_DATABASE_URL points at a migrated database:

    make db && make migrate
    DOCTASK_TEST_DATABASE_URL=postgresql://doctask:doctask@localhost:5432/doctask pytest

The run is killed twice, each time in the window where a side effect is already
committed but the checkpoint that records it is not:

  1. after `put_facts` -- ingest, classify and extraction are durable, the node
     that wrote the facts is not checkpointed, so the restart replays it;
  2. after `decide_review_items` -- the human decisions are durable, the node
     that recorded them is not checkpointed, so the restart replays that too.

Each restart builds a brand-new process-level app (repository pool, checkpointer,
compiled graph) and resumes with thread_id = run_id.
"""

from __future__ import annotations

import asyncio
import json
import signal
import sys
from pathlib import Path
from uuid import UUID, uuid4

from pg_app import DATABASE_URL, facts, query, requires_postgres, restart, stages

pytestmark = requires_postgres

CORPUS = Path(__file__).resolve().parent.parent / "data" / "sample_data"
# The same worker `scripts/crash_demo.py` drives. One kill mechanism, used by both the
# integration test and the reviewer-facing demo, so the demo cannot drift from the proof.
WORKER = Path(__file__).resolve().parent.parent / "scripts" / "crash_worker.py"


async def _crash(tmp_path: Path, name: str, spec: dict) -> None:
    """Run one leg in a subprocess and require that it died by SIGKILL."""
    spec_file = tmp_path / f"{name}.json"
    spec_file.write_text(json.dumps({"database_url": DATABASE_URL, **spec}))
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(WORKER),
        str(spec_file),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    assert process.returncode == -signal.SIGKILL, (
        f"{name} did not crash (rc={process.returncode}): {stderr.decode()}"
    )


async def _decisions(run_id: UUID) -> dict[str, tuple]:
    rows = await query(
        "SELECT id, state, decided_by, decided_at FROM review_items WHERE run_id = %s", run_id
    )
    return {
        str(row["id"]): (row["state"], row["decided_by"], row["decided_at"])
        for row in rows
        if row["decided_at"] is not None
    }


async def test_a_killed_run_resumes_on_the_same_run_id(tmp_path) -> None:
    app = await restart()
    collection_id = await app.repository.create_collection(f"crash-{uuid4().hex[:8]}")
    run_id = uuid4()

    # 1. Start the run in another process and kill it the moment the extracted facts
    #    are committed, before LangGraph checkpoints the node that wrote them.
    await _crash(
        tmp_path,
        "extraction",
        {
            "kill_after": "put_facts",
            "run_id": str(run_id),
            "collection_id": str(collection_id),
            "idempotency_key": f"crash-{run_id}",
            "input": {
                "run_id": str(run_id),
                "collection_id": str(collection_id),
                "idempotency_key": f"crash-{run_id}",
                "input_document": {
                    "filename": "vendor_msa.txt",
                    "mime_type": "text/plain",
                    "text": (CORPUS / "vendor_msa.txt").read_text(),
                },
                "validation_attempt": 0,
                "status": "running",
            },
        },
    )
    rows, unique_rows = await facts(collection_id)
    assert rows > 0, "the killed process was supposed to commit its facts first"
    assert rows == unique_rows

    # 2. Restart: fresh pool, fresh checkpointer, fresh graph, same run_id. The
    #    unfinished node replays, re-extracts and re-writes the same facts.
    await app.close()
    app = await restart()
    result = await app.resume(run_id)
    assert "__interrupt__" in result, "the resumed run should stop at the human gate"
    assert await facts(collection_id) == (rows, unique_rows)

    documents = await query(
        "SELECT count(*) AS rows FROM documents WHERE collection_id = %s", collection_id
    )
    assert documents[0]["rows"] == 1, "the replayed ingest must not create a second document"

    # 3. Decide the register proposals, and die once the decisions are committed.
    payload = await app.approve_all(run_id)
    assert payload["decisions"], "the run should have produced something to decide"
    await _crash(
        tmp_path,
        "review",
        {"kill_after": "decide_review_items", "run_id": str(run_id), "resume": payload},
    )
    decided = await _decisions(run_id)
    assert set(decided) == set(payload["decisions"])
    assert all(state == "approved" and by == "reviewer-1" for state, by, _ in decided.values())

    # 4. Restart again and drive the run to its report.
    await app.close()
    app = await restart()
    report = await app.finish(run_id)
    assert report["run_id"] == str(run_id)

    # The decisions taken before the crash are the ones that stand: same actor, same
    # timestamps, no second decision overwriting them on replay.
    after = await _decisions(run_id)
    assert {key: after[key] for key in decided} == decided

    # Replaying extraction and the commit wrote no duplicate facts and no phantom
    # second version of any register item.
    assert await facts(collection_id) == (rows, unique_rows)
    register = await app.repository.list_register(collection_id)
    assert register, "the resumed run should have committed the approved proposals"
    assert {item.version for item in register} == {1}

    await app.close()


async def _register_rows(collection_id: UUID) -> list[dict]:
    return await query(
        "SELECT agreement_id, key, value, content_hash, version FROM register_items "
        "WHERE collection_id = %s ORDER BY agreement_id, key",
        collection_id,
    )


async def test_a_kill_at_every_writing_stage_leaves_exactly_one_of_everything(
    tmp_path,
) -> None:
    """SIGKILL after each stage that writes, then resume, and count what exists.

    Every one of these is the same window: the side effect is committed and the LangGraph
    checkpoint that records it is not, so the restart replays the node that wrote it. The
    node's own idempotency is what makes the replay safe; the stage ledger is what makes
    it *checkable* -- `attempts > 1` on a stage whose output hash is unchanged is a replay
    that agreed with itself, which is the only thing exactly-once can actually mean here.
    """
    app = await restart()
    collection_id = await app.repository.create_collection(f"kill-{uuid4().hex[:8]}")
    run_id = uuid4()
    text = (CORPUS / "vendor_msa.txt").read_text()
    spec_input = {
        "run_id": str(run_id),
        "collection_id": str(collection_id),
        "idempotency_key": f"kill-{run_id}",
        "input_document": {
            "filename": "vendor_msa.txt",
            "mime_type": "text/plain",
            "text": text,
        },
        "validation_attempt": 0,
        "status": "running",
    }

    # 1. Killed the instant the document row is durable, before anything else runs.
    await _crash(
        tmp_path,
        "ingestion",
        {
            "kill_after": "put_document",
            "run_id": str(run_id),
            "collection_id": str(collection_id),
            "idempotency_key": f"kill-{run_id}",
            "input": spec_input,
        },
    )
    documents = await query(
        "SELECT count(*) AS rows FROM documents WHERE collection_id = %s", collection_id
    )
    assert documents[0]["rows"] == 1

    # 2. Resume, and die once the extracted facts are durable.
    await app.close()
    app = await restart()
    await _crash(
        tmp_path,
        "facts",
        {"kill_after": "put_facts", "run_id": str(run_id), "resume": None,
         "retry_input": spec_input},
    )
    rows, unique_rows = await facts(collection_id)
    assert rows > 0 and rows == unique_rows

    # 3. Resume, and die once the review items a human will answer are durable.
    await app.close()
    app = await restart()
    await _crash(
        tmp_path,
        "review_creation",
        {"kill_after": "add_review_items", "run_id": str(run_id), "resume": None,
         "retry_input": spec_input},
    )
    created = await query(
        "SELECT count(*) AS rows FROM review_items WHERE run_id = %s", run_id
    )
    assert created[0]["rows"] > 0

    # The replayed ingest and extraction added nothing: one document, no duplicate facts,
    # and no second copy of a review item a human would otherwise be asked twice about.
    await app.close()
    app = await restart()
    result = await app.resume(run_id)
    assert "__interrupt__" in result
    assert (await query(
        "SELECT count(*) AS rows FROM documents WHERE collection_id = %s", collection_id
    ))[0]["rows"] == 1
    assert await facts(collection_id) == (rows, unique_rows)
    assert (await query(
        "SELECT count(*) AS rows FROM review_items WHERE run_id = %s", run_id
    ))[0]["rows"] == created[0]["rows"]

    # 4. Answer Gate 1 and die once the decisions are durable.
    payload = await app.approve_all(run_id)
    await _crash(
        tmp_path,
        "decisions",
        {"kill_after": "decide_review_items", "run_id": str(run_id), "resume": payload},
    )
    decided = await _decisions(run_id)
    assert set(decided) == set(payload["decisions"])

    # 5. Resume to the commit and die the instant the register is durable. The ledger row
    #    is written in that same transaction, so this kill cannot land between them.
    await app.close()
    app = await restart()
    await _crash(
        tmp_path,
        "commit",
        {
            "kill_after": "commit_approved",
            "run_id": str(run_id),
            "resume": None,
            "retry_input": spec_input,
        },
    )
    committed = await _register_rows(collection_id)
    assert committed, "the killed process was supposed to commit first"
    assert {row["version"] for row in committed} == {1}
    ledger = await stages(run_id)
    assert ledger["commit_approved"]["status"] == "completed"

    # 6. Resume once more. The commit node replays; the ledger recognises the basis and
    #    returns what was written rather than versioning every row a second time.
    await app.close()
    app = await restart()
    report = await app.finish(run_id)
    assert report["run_id"] == str(run_id)
    assert await _register_rows(collection_id) == committed
    assert await facts(collection_id) == (rows, unique_rows)
    assert {key: (await _decisions(run_id))[key] for key in decided} == decided

    replayed = await stages(run_id)
    assert replayed["commit_approved"]["attempts"] >= 2, "the commit node did replay"
    assert replayed["commit_approved"]["output_hash"] == (
        ledger["commit_approved"]["output_hash"]
    ), "a replayed stage that produced a different result is not exactly-once"

    # Every ledgered stage agrees with itself across all of those restarts.
    for stage, row in replayed.items():
        assert row["status"] == "completed", stage
        assert row["output_hash"] is not None, stage

    await app.close()


async def test_two_resume_requests_cannot_drive_one_thread(tmp_path) -> None:
    """`thread_id = run_id` is what makes resume addressable. It is also what lets two
    callers -- a retrying client, a watcher, two replicas -- execute the same nodes at
    once. The lease is compare-and-set, so exactly one of them proceeds."""
    app = await restart()
    collection_id = await app.repository.create_collection(f"lease-{uuid4().hex[:8]}")
    run_id = uuid4()
    await app.start(
        collection_id=collection_id,
        run_id=run_id,
        filename="vendor_msa.txt",
        text=(CORPUS / "vendor_msa.txt").read_text(),
    )

    assert await app.repository.acquire_run_lease(run_id, "worker-a", ttl_seconds=300)
    assert not await app.repository.acquire_run_lease(run_id, "worker-b", ttl_seconds=300)
    # Re-entrant for the holder: a retry inside one process is not contention.
    assert await app.repository.acquire_run_lease(run_id, "worker-a", ttl_seconds=300)

    await app.repository.release_run_lease(run_id, "worker-b")  # not the holder: no-op
    assert not await app.repository.acquire_run_lease(run_id, "worker-b", ttl_seconds=300)
    await app.repository.release_run_lease(run_id, "worker-a")
    assert await app.repository.acquire_run_lease(run_id, "worker-b", ttl_seconds=300)

    # An expired lease is takeable. A process SIGKILLed holding one must not lock its own
    # run out of the resume that would recover it -- which is the whole point of the
    # ledger making that resume safe.
    await query(
        "UPDATE runs SET lease_expires_at = now() - interval '1 second' "
        "WHERE id = %s RETURNING id",
        run_id,
    )
    assert await app.repository.acquire_run_lease(run_id, "worker-c", ttl_seconds=300)

    await app.close()

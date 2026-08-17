"""Resume-after-restart and concurrent-run behaviour, with no database.

Requirement 7 names six scenarios the test suite has to cover. Four of them were already
offline-only (`test_injection_containment.py`, `test_review_authority.py`,
`test_evidence.py`, `test_register_flow.py`). The other two -- a killed-and-resumed
process and concurrent runs -- were proved only in `test_crash_resume.py` and
`test_concurrent_runs.py`, both of which skip unless `DOCTASK_TEST_DATABASE_URL` is set.
A reviewer running plain `pytest` therefore saw neither.

This file closes that: the same properties, at the level a single process can actually
establish them, so `pytest` alone exercises all six.

What this file does *not* claim, and deliberately leaves to the Postgres suite: durability
across a real SIGKILL, and genuine cross-process contention. `InMemorySaver` dies with the
process and `asyncio` tasks do not race the way two connection pools do. What is provable
here is that resume is addressed by `thread_id = run_id` rather than by anything held in
the app that started the run, that a replayed writing stage is ledgered as a replay and
agrees with itself, and that the compare-and-set guards refuse the loser of a race rather
than letting the last write win. `make demo-crash` is the end-to-end version, and it does
use SIGKILL and Postgres.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from doctask.auth import REVIEWER, Principal
from doctask.domain import RegisterKey
from doctask.graph.builder import build_graph
from doctask.graph.nodes import NodeDependencies
from doctask.llm.fake import FakeLLM
from doctask.repositories.memory import InMemoryRepository
from doctask.services.hashing import register_content_hash
from doctask.services.rules import parse_ruleset

CORPUS = Path(__file__).resolve().parent.parent / "data" / "sample_data"
PLAYBOOK = json.loads((CORPUS / "rules.json").read_text())
REVIEWER_PAYLOAD = Principal(actor_id="reviewer-1", role=REVIEWER).as_payload()


class Process:
    """One process's worth of state, over storage that outlives it.

    A restart throws the compiled graph and the node dependencies away and builds new
    ones against the same repository and the same checkpointer -- which is exactly what
    survives a real restart, and the reason `thread_id = run_id` is the whole resume
    story. Nothing about a run may live in the object that started it.
    """

    def __init__(self, repository: InMemoryRepository, saver: InMemorySaver) -> None:
        self.repository = repository
        self.saver = saver
        self.graph = build_graph(
            NodeDependencies(repository=repository, model=FakeLLM()), checkpointer=saver
        )

    def restart(self) -> Process:
        return Process(self.repository, self.saver)

    @staticmethod
    def config(run_id: UUID) -> dict:
        return {"configurable": {"thread_id": str(run_id)}}

    async def start(self, collection_id: UUID, run_id: UUID, filename: str, text: str) -> dict:
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

    async def approvals(self, run_id: UUID) -> dict:
        pending = [
            item
            for item in await self.repository.list_review_items(run_id)
            if item.state == "pending"
        ]
        return {
            "decisions": {str(item.id): "approved" for item in pending},
            **REVIEWER_PAYLOAD,
        }

    async def finish(self, run_id: UUID) -> dict:
        """Answer every remaining gate with an approval and return the report."""
        result = await self.graph.ainvoke(None, config=self.config(run_id))
        while "report" not in result:
            result = await self.graph.ainvoke(
                Command(resume=await self.approvals(run_id)), config=self.config(run_id)
            )
        return result["report"]


@pytest.fixture
async def process():
    repository = InMemoryRepository()
    collection_id = await repository.create_collection("acme")
    await repository.put_ruleset(parse_ruleset(PLAYBOOK, collection_id))
    current = Process(repository, InMemorySaver())
    current.collection_id = collection_id
    return current


def _register(repository: InMemoryRepository, collection_id: UUID) -> dict:
    return {
        scoped: item
        for (cid, scoped), item in repository.register.items()
        if cid == collection_id
    }


# ------------------------------------------------- process killed and resumed ----


async def test_a_run_resumes_after_the_app_that_started_it_is_thrown_away(process) -> None:
    """The graph, the node dependencies and the model instance that ran the first half
    of this run are all discarded before it is resumed. Only storage carries over."""
    run_id = uuid4()
    result = await process.start(
        process.collection_id, run_id, "vendor_msa.txt", (CORPUS / "vendor_msa.txt").read_text()
    )
    assert "__interrupt__" in result, "the run should stop at the first human gate"
    stopped_at = {record.stage for record in await process.repository.list_stages(run_id)}
    assert stopped_at, "some stage should have been ledgered before the gate"

    # The restart. New graph object, new NodeDependencies, new FakeLLM.
    resumed = process.restart()
    assert resumed.graph is not process.graph

    report = await resumed.finish(run_id)

    assert report["run_id"] == str(run_id), "resume is addressed by run_id, not by the app"
    assert report["status"] == "committed"
    # Everything the first app had already ledgered is still ledgered, and still says
    # what it said. A restart that re-did the work would show new output hashes.
    after = {record.stage for record in await resumed.repository.list_stages(run_id)}
    assert stopped_at <= after
    assert len(process.repository.documents) == 1, "the resumed run stored no second document"
    fingerprints = [fact.fingerprint for fact in process.repository.facts_by_fingerprint.values()]
    assert len(fingerprints) == len(set(fingerprints)), "no duplicate facts"
    assert {item.version for item in _register(process.repository, process.collection_id).values()}\
        == {1}, "each register row was written exactly once"


async def test_a_replayed_commit_is_ledgered_as_a_replay_and_agrees_with_itself(process) -> None:
    """The window a SIGKILL lands in: the register write is durable and the checkpoint
    that records it is not, so the resumed run executes `commit_approved` a second time.

    The ledger row is written in the same transaction as the register rows, so the replay
    can recognise its own earlier work instead of versioning every row again -- and
    `attempts > 1` with an unchanged `output_hash` is what makes that checkable rather
    than merely claimed.
    """
    run_id = uuid4()
    await process.start(
        process.collection_id, run_id, "vendor_msa.txt", (CORPUS / "vendor_msa.txt").read_text()
    )
    report = await process.finish(run_id)
    assert report["status"] == "committed"

    committed = {key: (item.version, item.content_hash)
                 for key, item in _register(process.repository, process.collection_id).items()}
    assert committed, "the run was supposed to commit something"
    basis = report["review"]["candidate_basis_hash"]
    ledger_before = {
        (record.stage, record.input_hash): record
        for record in await process.repository.list_stages(run_id)
    }
    commit_row = next(
        record for (stage, _), record in ledger_before.items() if stage == "commit_approved"
    )
    assert commit_row.attempts == 1

    # Replay the node's write, exactly as a resumed run would.
    await process.repository.commit_approved(process.collection_id, run_id, basis_hash=basis)

    replayed = next(
        record
        for record in await process.repository.list_stages(run_id)
        if record.stage == "commit_approved"
    )
    assert replayed.attempts == 2, "the replay is recorded as a replay, not hidden"
    assert replayed.output_hash == commit_row.output_hash, (
        "a replayed stage that produced a different result is not exactly-once"
    )
    after = {key: (item.version, item.content_hash)
             for key, item in _register(process.repository, process.collection_id).items()}
    assert after == committed, "the replayed commit versioned nothing a second time"


# ------------------------------------------------------------ concurrent runs ----


async def test_two_runs_on_different_keys_both_commit(process) -> None:
    """Two runs in one collection that touch no key in common must both land, and
    neither may bump the other's version."""
    alpha, bravo = uuid4(), uuid4()
    await process.start(process.collection_id, alpha, "alpha.txt",
                        "MASTER SERVICES AGREEMENT\n\nPayment is due within 30 calendar days "
                        "of receipt.\n")
    await process.start(process.collection_id, bravo, "bravo.txt",
                        "MASTER SERVICES AGREEMENT\n\nLiability is capped at $250,000.\n")
    await process.finish(alpha)
    await process.finish(bravo)

    register = _register(process.repository, process.collection_id)
    payment = RegisterKey("", "payment_due_days").text
    liability = RegisterKey("", "liability_cap").text
    assert payment in register and liability in register, sorted(register)
    assert register[payment].version == 1, "bravo's commit must not version alpha's row"
    assert register[liability].version == 1, "alpha's commit must not version bravo's row"


async def test_the_loser_of_a_race_on_one_key_is_refused_rather_than_overwriting(
    process,
) -> None:
    """Both runs derive the same key from the version they each read. One commits; the
    other is refused as stale instead of last-write-winning."""
    first, second = uuid4(), uuid4()
    await process.start(process.collection_id, first, "first.txt",
                        "MASTER SERVICES AGREEMENT\n\nPayment is due within 30 calendar days "
                        "of receipt.\n")
    # The second run reads the register while the first is still parked at its gate, so
    # both hold the same expected version for the row they are about to write.
    await process.start(process.collection_id, second, "second.txt",
                        "FIRST AMENDMENT TO MASTER SERVICES AGREEMENT\n\nThis amendment "
                        "replaces the payment provision.\n\nPayment is due within 45 calendar "
                        "days of receipt.\n")

    await process.finish(first)
    losing = await process.finish(second)

    register = _register(process.repository, process.collection_id)
    payment = register[RegisterKey("", "payment_due_days").text]
    assert losing["status"] == "stale", losing["status"]
    assert losing["stale_keys"], "the refused key has to be named, not silently dropped"
    assert not losing["committed_keys"], "a stale run writes nothing"
    assert payment.version == 1, "the loser did not overwrite the winner"
    assert payment.value["days"] == 30, payment.value


async def test_the_same_idempotency_key_yields_one_run(process) -> None:
    """A retrying client must get its original run back rather than a second one."""
    first, duplicate = await process.repository.create_run(
        collection_id=process.collection_id, run_id=uuid4(), idempotency_key="upload-1"
    )
    assert duplicate is False
    second, duplicate = await process.repository.create_run(
        collection_id=process.collection_id, run_id=uuid4(), idempotency_key="upload-1"
    )
    assert duplicate is True
    assert second == first, "the same key must resolve to the same run"


async def test_only_one_caller_can_hold_a_run_lease(process) -> None:
    """`thread_id = run_id` makes resume addressable by anyone who knows the run, so the
    lease is what stops two callers each answering the same human gate."""
    run_id = uuid4()
    await process.repository.create_run(
        collection_id=process.collection_id, run_id=run_id, idempotency_key="lease"
    )
    assert await process.repository.acquire_run_lease(run_id, "worker-a", ttl_seconds=300)
    assert not await process.repository.acquire_run_lease(run_id, "worker-b", ttl_seconds=300)
    # Re-entrant for the holder: a retry inside one process is not contention.
    assert await process.repository.acquire_run_lease(run_id, "worker-a", ttl_seconds=300)

    await process.repository.release_run_lease(run_id, "worker-b")  # not the holder: no-op
    assert not await process.repository.acquire_run_lease(run_id, "worker-b", ttl_seconds=300)
    await process.repository.release_run_lease(run_id, "worker-a")
    assert await process.repository.acquire_run_lease(run_id, "worker-b", ttl_seconds=300)


async def test_a_review_decision_cannot_be_taken_twice(process) -> None:
    """Compare-and-set from `pending`: the second decider loses rather than overwriting
    the first one's answer."""
    run_id = uuid4()
    await process.start(
        process.collection_id, run_id, "vendor_msa.txt", (CORPUS / "vendor_msa.txt").read_text()
    )
    pending = [
        item
        for item in await process.repository.list_review_items(run_id)
        if item.state == "pending"
    ]
    assert pending, "the run should have produced something to decide"
    target = pending[0]

    decided = await process.repository.decide_review_items(
        run_id, "reviewer-1", {target.id: "approved"}
    )
    assert [item.id for item in decided] == [target.id]

    with pytest.raises(ValueError, match="already decided"):
        await process.repository.decide_review_items(run_id, "reviewer-2", {target.id: "rejected"})

    stored = next(
        item for item in await process.repository.list_review_items(run_id) if item.id == target.id
    )
    assert stored.state == "approved" and stored.decided_by == "reviewer-1"


def test_a_register_hash_is_built_from_evidence_not_from_row_ids() -> None:
    """Guards the property every "unchanged" claim in the other tests rests on: two
    collections rebuilt from the same documents produce the same content hash."""
    left = register_content_hash(
        value={"days": 30}, evidence_fingerprints=["a" * 64, "b" * 64], state="supported"
    )
    right = register_content_hash(
        value={"days": 30}, evidence_fingerprints=["b" * 64, "a" * 64], state="supported"
    )
    assert left == right, "citation order is not part of the value"
    assert left != register_content_hash(
        value={"days": 45}, evidence_fingerprints=["a" * 64, "b" * 64], state="supported"
    )

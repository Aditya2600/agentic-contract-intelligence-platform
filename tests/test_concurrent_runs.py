"""Two runs working the same collection at the same time.

Skipped unless DOCTASK_TEST_DATABASE_URL points at a migrated database:

    make db && make migrate
    DOCTASK_TEST_DATABASE_URL=postgresql://doctask:doctask@localhost:5432/doctask pytest

Each run drives its own `App` -- separate connection pools, separate checkpointer,
separate compiled graph -- so to Postgres these are two processes competing for the
same rows. The gates are synchronised with a barrier so the commits actually
overlap instead of politely queueing.
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest
from pg_app import App, facts, query, requires_postgres, restart, stages

pytestmark = requires_postgres

ALPHA = """MASTER SERVICES AGREEMENT - ALPHA

Payment is due within 30 calendar days of receipt.
"""

BRAVO = """MASTER SERVICES AGREEMENT - BRAVO

Liability is capped at $250,000.

Either party may terminate the agreement with 60 days' written notice.
"""

CHARLIE = """MASTER SERVICES AGREEMENT - CHARLIE

Payment is due within 45 calendar days of receipt.
"""


@pytest.fixture
async def apps():
    pair = [await restart(), await restart()]
    yield pair
    for app in pair:
        await app.close()


async def _to_gate(app: App, collection_id, run_id, filename: str, text: str) -> dict:
    """Run until the human gate and return the approval payload for it."""
    result = await app.start(
        collection_id=collection_id, run_id=run_id, filename=filename, text=text
    )
    if "report" in result:
        return result  # short-circuited (duplicate document), nothing to decide
    return await app.approve_all(run_id)


async def _commit_together(app: App, run_id, payload: dict, barrier: asyncio.Barrier) -> dict:
    if "report" in payload:
        await barrier.wait()
        return payload["report"]
    await barrier.wait()
    return await app.finish(run_id, payload)


async def _register(collection_id) -> dict[str, dict]:
    rows = await query(
        "SELECT key, value, state, version, last_run_id, content_hash "
        "FROM register_items WHERE collection_id = %s",
        collection_id,
    )
    return {row["key"]: row for row in rows}


async def test_two_runs_touching_different_keys_both_land(apps) -> None:
    """Neither run's commit may swallow the other's, and neither may bump the
    other's version: the advisory lock serialises them, it does not merge them."""
    app_a, app_b = apps
    collection_id = await app_a.repository.create_collection(f"race-{uuid4().hex[:8]}")
    run_a, run_b = uuid4(), uuid4()

    payloads = await asyncio.gather(
        _to_gate(app_a, collection_id, run_a, "alpha.txt", ALPHA),
        _to_gate(app_b, collection_id, run_b, "bravo.txt", BRAVO),
    )
    barrier = asyncio.Barrier(2)
    report_a, report_b = await asyncio.gather(
        _commit_together(app_a, run_a, payloads[0], barrier),
        _commit_together(app_b, run_b, payloads[1], barrier),
    )
    assert report_a["run_id"] == str(run_a)
    assert report_b["run_id"] == str(run_b)

    register = await _register(collection_id)
    assert sorted(register) == ["liability_cap", "notice_days", "payment_due_days"]
    # One commit per key: a concurrent run rewriting someone else's item would show
    # up here as a second version.
    assert {row["version"] for row in register.values()} == {1}
    assert register["payment_due_days"]["last_run_id"] == run_a
    assert register["liability_cap"]["last_run_id"] == run_b
    assert register["notice_days"]["last_run_id"] == run_b

    documents = await query(
        "SELECT count(*) AS rows FROM documents WHERE collection_id = %s", collection_id
    )
    assert documents[0]["rows"] == 2
    rows, unique_rows = await facts(collection_id)
    assert rows == unique_rows == 3

    # Every landed value is on the audit trail, each as a fresh item.
    changes = await query(
        "SELECT run_id, old_hash FROM change_log WHERE collection_id = %s", collection_id
    )
    assert sorted(change["run_id"] for change in changes) == sorted([run_a, run_b, run_b])
    assert all(change["old_hash"] is None for change in changes)


async def test_the_loser_of_a_race_is_refused_rather_than_last_write_winning(apps) -> None:
    """Same key, different values, both derived from the same empty slot.

    Both humans approved a change from "no value" to their own number. Whoever
    commits second is describing a change that no longer exists: its `before` no
    longer matches the stored item. Last-write-wins would silently erase a value
    that reviewer never saw, so the second commit is refused and reported.
    """
    app_a, app_b = apps
    collection_id = await app_a.repository.create_collection(f"clash-{uuid4().hex[:8]}")
    run_a, run_b = uuid4(), uuid4()

    payloads = await asyncio.gather(
        _to_gate(app_a, collection_id, run_a, "alpha.txt", ALPHA),
        _to_gate(app_b, collection_id, run_b, "charlie.txt", CHARLIE),
    )
    barrier = asyncio.Barrier(2)
    reports = await asyncio.gather(
        _commit_together(app_a, run_a, payloads[0], barrier),
        _commit_together(app_b, run_b, payloads[1], barrier),
    )
    by_run = {UUID(report["run_id"]): report for report in reports}

    register = await _register(collection_id)
    assert list(register) == ["payment_due_days"]
    item = register["payment_due_days"]
    # One commit landed, the other never touched the row: no second version.
    assert item["version"] == 1

    changes = await query(
        "SELECT run_id, new_value, new_hash FROM change_log WHERE collection_id = %s ORDER BY id",
        collection_id,
    )
    assert len(changes) == 1, "a refused proposal writes nothing, not even history"
    assert item["last_run_id"] == changes[0]["run_id"]
    assert item["value"] == changes[0]["new_value"]
    assert item["content_hash"] == changes[0]["new_hash"]

    winner = by_run[item["last_run_id"]]
    loser = by_run[run_b if item["last_run_id"] == run_a else run_a]
    assert winner["status"] == "committed"
    assert winner["committed_keys"] == ["::payment_due_days"]
    assert winner["stale_keys"] == []

    # The refusal is reported, names the key, and says what it was derived from.
    assert loser["status"] == "stale"
    assert loser["committed_keys"] == []
    assert [entry["key"] for entry in loser["stale_keys"]] == ["::payment_due_days"]
    assert loser["stale_keys"][0]["expected_version"] is None
    assert loser["stale_keys"][0]["actual_version"] == 1
    assert loser["stale_keys"][0]["actual_hash"] == item["content_hash"]

    rows, unique_rows = await facts(collection_id)
    assert rows == unique_rows == 2, "the refused run's evidence is still stored"


async def test_a_stale_run_is_redone_from_stored_facts_without_re_uploading(apps) -> None:
    """The refusal is not a dead end, and clearing it costs no upload and no extraction.

    The stale run's document is already stored, so re-uploading it would short-circuit
    on its SHA-256 rather than re-derive anything, and the run itself cannot propose the
    same key twice. The re-derive run is the way out: same document, stored facts, but
    the register read fresh, a new run_id, and a new decision from a human who is now
    looking at the value that actually landed.
    """
    app_a, app_b = apps
    collection_id = await app_a.repository.create_collection(f"redo-{uuid4().hex[:8]}")
    run_a, run_b = uuid4(), uuid4()

    payloads = await asyncio.gather(
        _to_gate(app_a, collection_id, run_a, "alpha.txt", ALPHA),
        _to_gate(app_b, collection_id, run_b, "charlie.txt", CHARLIE),
    )
    barrier = asyncio.Barrier(2)
    reports = await asyncio.gather(
        _commit_together(app_a, run_a, payloads[0], barrier),
        _commit_together(app_b, run_b, payloads[1], barrier),
    )
    stale_run = next(UUID(report["run_id"]) for report in reports if report["status"] == "stale")
    committed = (await _register(collection_id))["payment_due_days"]
    facts_before = await facts(collection_id)

    redo_run = uuid4()
    result = await app_a.rederive(
        collection_id=collection_id, run_id=redo_run, source_run=stale_run
    )
    assert "report" not in result, "a re-derive run must stop for a human, not auto-commit"

    # What the human is shown was derived from the value that is stored now, not from
    # the empty slot the refused proposal was written against.
    proposals = {
        item.target_key: item
        for item in await app_a.repository.list_review_items(redo_run)
        if item.kind in {"register_update", "conflict"}
    }
    assert list(proposals) == ["::payment_due_days"]
    assert proposals["::payment_due_days"].payload["before"]["version"] == committed["version"]
    assert (
        proposals["::payment_due_days"].payload["before"]["content_hash"]
        == committed["content_hash"]
    )

    report = await app_a.finish(redo_run, await app_a.approve_all(redo_run))
    assert report["status"] == "committed"
    assert report["committed_keys"] == ["::payment_due_days"]
    assert report["stale_keys"] == []

    after = (await _register(collection_id))["payment_due_days"]
    assert after["version"] == 2
    assert after["last_run_id"] == redo_run
    assert after["content_hash"] != committed["content_hash"]
    # Re-deriving is not a second chance to install the refused value. Both documents are
    # in evidence now and they disagree, so the key commits disputed, citing both sides,
    # with a conflict left open for a human rather than a winner picked automatically.
    assert after["state"] == "disputed"
    conflicts = await app_a.repository.list_conflicts(collection_id, state=None)
    assert {(conflict.key, conflict.kind) for conflict in conflicts} == {
        ("::payment_due_days", "contradiction")
    }
    items = await app_a.repository.get_register_items(collection_id, ["::payment_due_days"])
    assert len(items["::payment_due_days"].citation_fact_ids) == 2

    documents = await query(
        "SELECT count(*) AS rows FROM documents WHERE collection_id = %s", collection_id
    )
    assert documents[0]["rows"] == 2, "the re-derive run stored no document of its own"
    assert await facts(collection_id) == facts_before, "it reused the stored facts"
    stages = {
        event.stage for event in await app_a.repository.list_events(redo_run)
    }
    assert "rederive" in stages
    assert stages.isdisjoint({"ingest", "classify", "extract_facts"}), "no re-extraction"


async def test_rederiving_a_run_that_committed_everything_does_nothing(apps) -> None:
    app_a, _ = apps
    collection_id = await app_a.repository.create_collection(f"noop-{uuid4().hex[:8]}")
    run_id = uuid4()
    payload = await _to_gate(app_a, collection_id, run_id, "alpha.txt", ALPHA)
    await app_a.finish(run_id, payload)
    before = await _register(collection_id)

    result = await app_a.rederive(
        collection_id=collection_id, run_id=uuid4(), source_run=run_id
    )

    assert result["report"]["status"] == "nothing_to_rederive"
    assert result["report"]["committed_keys"] == []
    assert await _register(collection_id) == before


async def test_the_same_idempotency_key_from_two_runs_creates_one_run(apps) -> None:
    app_a, app_b = apps
    collection_id = await app_a.repository.create_collection(f"idem-{uuid4().hex[:8]}")
    key = f"same-key-{uuid4()}"

    results = await asyncio.gather(
        *(
            app.repository.create_run(
                collection_id=collection_id, run_id=uuid4(), idempotency_key=key
            )
            for app in (app_a, app_b)
        )
    )
    assert len({run_id for run_id, _ in results}) == 1
    assert sorted(duplicate for _, duplicate in results) == [False, True]

    rows = await query(
        "SELECT count(*) AS rows FROM runs WHERE collection_id = %s AND idempotency_key = %s",
        collection_id,
        key,
    )
    assert rows[0]["rows"] == 1


async def test_the_same_document_uploaded_twice_at_once_is_stored_once(apps) -> None:
    app_a, app_b = apps
    collection_id = await app_a.repository.create_collection(f"dupe-{uuid4().hex[:8]}")
    run_a, run_b = uuid4(), uuid4()

    payloads = await asyncio.gather(
        _to_gate(app_a, collection_id, run_a, "alpha.txt", ALPHA),
        _to_gate(app_b, collection_id, run_b, "alpha-again.txt", ALPHA),
    )
    barrier = asyncio.Barrier(2)
    reports = await asyncio.gather(
        _commit_together(app_a, run_a, payloads[0], barrier),
        _commit_together(app_b, run_b, payloads[1], barrier),
    )

    documents = await query(
        "SELECT count(*) AS rows FROM documents WHERE collection_id = %s", collection_id
    )
    assert documents[0]["rows"] == 1
    # Exactly one run pays for the work; the loser of the insert race short-circuits.
    assert [report["status"] for report in reports].count("duplicate_noop") == 1
    rows, unique_rows = await facts(collection_id)
    assert rows == unique_rows == 1
    assert [item["version"] for item in (await _register(collection_id)).values()] == [1]


async def test_two_runs_on_one_key_leave_one_commit_and_one_stale_run(apps) -> None:
    """The whole concurrency story in one assertion: one commit, one refusal, no revision
    written twice.

    Both runs read the same empty slot for `payment_due_days` and both humans approved a
    change from "no value" to their own number. Exactly one of those describes a change
    that still exists by the time it commits. The other is refused -- at
    `verify_review_binding` if the register moved before it got there, or by the per-key
    staleness check at commit if it moved after -- and either way it writes nothing, not
    a second version and not a change-log entry.
    """
    app_a, app_b = apps
    collection_id = await app_a.repository.create_collection(f"onekey-{uuid4().hex[:8]}")
    run_a, run_b = uuid4(), uuid4()

    payloads = await asyncio.gather(
        _to_gate(app_a, collection_id, run_a, "alpha.txt", ALPHA),
        _to_gate(app_b, collection_id, run_b, "charlie.txt", CHARLIE),
    )
    barrier = asyncio.Barrier(2)
    reports = await asyncio.gather(
        _commit_together(app_a, run_a, payloads[0], barrier),
        _commit_together(app_b, run_b, payloads[1], barrier),
    )

    outcomes = sorted(report["status"] for report in reports)
    assert outcomes == ["committed", "stale"], outcomes

    register = await _register(collection_id)
    assert list(register) == ["payment_due_days"]
    assert register["payment_due_days"]["version"] == 1, "one commit, one revision"

    changes = await query(
        "SELECT run_id FROM change_log WHERE collection_id = %s", collection_id
    )
    assert len(changes) == 1
    assert changes[0]["run_id"] == register["payment_due_days"]["last_run_id"]

    # Both runs reached the commit node -- they were barrier-synchronised past the
    # binding check, so the loser only discovers the move when it takes the lock. What
    # separates them is what came out: the winner's commit produced a register key, the
    # loser's produced nothing and said so in the ledger rather than writing anything.
    winner = next(UUID(r["run_id"]) for r in reports if r["status"] == "committed")
    loser = next(UUID(r["run_id"]) for r in reports if r["status"] == "stale")
    winning_report = next(r for r in reports if r["status"] == "committed")
    losing_report = next(r for r in reports if r["status"] == "stale")
    assert winning_report["committed_keys"] == ["::payment_due_days"]
    assert losing_report["committed_keys"] == []
    assert [entry["key"] for entry in losing_report["stale_keys"]] == ["::payment_due_days"]

    winner_ledger = (await stages(winner))["commit_approved"]
    loser_ledger = (await stages(loser))["commit_approved"]
    assert winner_ledger["status"] == loser_ledger["status"] == "completed"
    assert winner_ledger["output_hash"] != loser_ledger["output_hash"], (
        "a commit that wrote a register key and one that wrote nothing are not the "
        "same outcome, and the ledger has to be able to tell them apart"
    )
    assert winner_ledger["attempts"] == loser_ledger["attempts"] == 1

    # Both runs still have their human decisions on the record. The refused one is a run
    # that was reviewed and then not applied, which is a different thing from a run
    # nobody looked at, and the ledger of its earlier stages says which.
    for run_id in (winner, loser):
        decided = await query(
            "SELECT count(*) AS rows FROM review_items "
            "WHERE run_id = %s AND decided_at IS NOT NULL",
            run_id,
        )
        assert decided[0]["rows"] > 0, run_id
        assert (await stages(run_id))["await_review"]["status"] == "completed"


async def test_a_replayed_commit_returns_the_first_one_instead_of_revising_again(
    apps,
) -> None:
    """Commit is idempotent on (run, candidate basis), proven at the repository boundary.

    The graph replays this node whenever a process dies between the commit transaction
    and LangGraph's checkpoint. Without the ledger the second call would rely on every
    content hash still matching to no-op -- true most of the time, and false in exactly
    the case that matters, which is a concurrent commit in between.
    """
    app, _ = apps
    collection_id = await app.repository.create_collection(f"replay-{uuid4().hex[:8]}")
    run_id = uuid4()
    payload = await _to_gate(app, collection_id, run_id, "alpha.txt", ALPHA)
    report = await app.finish(run_id, payload)
    assert report["status"] == "committed"

    before = await _register(collection_id)
    basis = report["review"]["candidate_basis_hash"]

    again = await app.repository.commit_approved(collection_id, run_id, basis_hash=basis)

    assert [item.register_key.text for item in again.committed] == [
        "::payment_due_days"
    ]
    assert again.stale == []
    assert await _register(collection_id) == before, "a replay must write nothing"
    assert (await stages(run_id))["commit_approved"]["attempts"] >= 2

    changes = await query(
        "SELECT count(*) AS rows FROM change_log WHERE collection_id = %s", collection_id
    )
    assert changes[0]["rows"] == 1, "a replayed commit must not log a second change"

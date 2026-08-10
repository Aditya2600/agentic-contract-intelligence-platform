"""Integration proof for the production repository.

Skipped unless DOCTASK_TEST_DATABASE_URL points at a migrated database:

    make db && make migrate
    DOCTASK_TEST_DATABASE_URL=postgresql://doctask:doctask@localhost:5432/doctask pytest
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

from doctask.domain import (
    Block,
    Conflict,
    Document,
    DocumentRelation,
    FactCandidate,
    FactScope,
    RegisterKey,
    ReviewItem,
)
from doctask.services.grounding import span_for
from doctask.services.hashing import sha256_text

DATABASE_URL = os.getenv("DOCTASK_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="set DOCTASK_TEST_DATABASE_URL to run Postgres integration tests"
)


@pytest.fixture
async def repo():
    from doctask.repositories.postgres import PostgresRepository

    repository = PostgresRepository(DATABASE_URL)
    await repository.open()
    yield repository
    await repository.close()


async def _seeded_fact(
    repo,
    collection_id,
    run_id,
    *,
    text="Payment is due in 30 days.",
    key="payment_due_days",
    doc_type="master_agreement",
    filename="msa.txt",
):
    """Store one document, one block and one grounded fact; return the candidate."""
    document, _ = await repo.put_document(
        Document(collection_id, filename, "text/plain", text, sha256_text(text))
    )
    await repo.set_document_type(document.id, doc_type, 0.95)
    block = Block(
        document_id=document.id,
        index=0,
        text=text,
        text_sha256=sha256_text(text),
        char_start=0,
        char_end=len(text),
    )
    await repo.put_blocks([block])
    value = {"days": 30}
    candidate = FactCandidate(
        key=key,
        value=value,
        block_id=block.id,
        quote=text,
        quote_start=0,
        quote_end=len(text),
        supported=True,
    )
    # Minted the way the graph mints it: from the block, after the quote checked out.
    candidate.evidence = span_for(
        block, candidate, document_sha256=document.sha256, extractor_version="test"
    )
    candidate.fingerprint = candidate.evidence.fingerprint(key=key, value=value)
    await repo.put_facts(collection_id, [candidate])
    return document, block, candidate


def _basis(run_id) -> str:
    """A distinct deliverable basis per run.

    Commit is idempotent on (run, basis), so passing the same one twice for one run is
    how these tests exercise a replay -- and a different run must never collide with it.
    """
    return sha256_text(f"basis-{run_id}")


def _scoped(key: str) -> str:
    """This module's fixtures name no agreement, so every row is in the "" bucket."""
    return RegisterKey("", key).text


async def _before(repo, collection_id, key):
    """What a proposal for `key` would have been derived from right now.

    Commit refuses a proposal whose register item moved since, so a proposal has to
    state the version and hash it read.
    """
    scoped = _scoped(key)
    stored = (await repo.get_register_items(collection_id, [scoped])).get(scoped)
    if stored is None:
        return None
    return {"content_hash": stored.content_hash, "version": stored.version}


async def _approved_item(
    repo,
    run_id,
    candidate,
    document_id,
    *,
    value=None,
    state="supported",
    conflict=None,
    before=...,
    collection_id=None,
):
    if before is ...:
        before = await _before(repo, collection_id, candidate.key) if collection_id else None
    item = ReviewItem(
        run_id=run_id,
        kind="conflict" if conflict else "register_update",
        target_key=_scoped(candidate.key),
        payload={
            "before": before,
            "document_id": str(document_id),
            "conflict": conflict,
            "after": {
                "value": value or candidate.value,
                "state": state,
                "citation_fact_ids": [str(candidate.id)],
                "citation_fingerprints": [candidate.fingerprint],
            },
        },
    )
    await repo.add_review_items([item])
    await repo.decide_review_items(run_id, "human-1", {item.id: "approved"})
    return item


async def test_document_dedupe_is_collection_scoped(repo) -> None:
    c1 = await repo.create_collection(f"pg-one-{uuid4()}")
    c2 = await repo.create_collection(f"pg-two-{uuid4()}")
    text = "same bytes"

    _, first = await repo.put_document(
        Document(c1, "a.txt", "text/plain", text, sha256_text(text))
    )
    stored, second = await repo.put_document(
        Document(c1, "b.txt", "text/plain", text, sha256_text(text))
    )
    _, other = await repo.put_document(
        Document(c2, "c.txt", "text/plain", text, sha256_text(text))
    )

    assert (first, second, other) == (False, True, False)
    assert stored.filename == "a.txt"  # the duplicate resolves to the first document


async def test_run_idempotency_key_returns_the_first_run(repo) -> None:
    collection_id = await repo.create_collection(f"pg-idem-{uuid4()}")
    first_id, first_dup = await repo.create_run(
        collection_id=collection_id, run_id=uuid4(), idempotency_key="key-1"
    )
    second_id, second_dup = await repo.create_run(
        collection_id=collection_id, run_id=uuid4(), idempotency_key="key-1"
    )
    assert (first_dup, second_dup) == (False, True)
    assert first_id == second_id


async def test_blocks_and_facts_are_replay_safe(repo) -> None:
    collection_id = await repo.create_collection(f"pg-replay-{uuid4()}")
    run_id, _ = await repo.create_run(
        collection_id=collection_id, run_id=uuid4(), idempotency_key=str(uuid4())
    )
    document, block, candidate = await _seeded_fact(repo, collection_id, run_id)

    # Replay the same nodes: same block row, same fact row.
    replayed = Block(
        document_id=document.id,
        index=0,
        text=block.text,
        text_sha256=block.text_sha256,
        char_start=0,
        char_end=len(block.text),
    )
    await repo.put_blocks([replayed])
    assert replayed.id == block.id

    await repo.put_facts(collection_id, [candidate])
    blocks = await repo.get_blocks(document.id)
    assert list(blocks) == [block.id]
    assert blocks[block.id].text == block.text


async def test_review_decision_is_compare_and_set(repo) -> None:
    collection_id = await repo.create_collection(f"pg-cas-{uuid4()}")
    run_id, _ = await repo.create_run(
        collection_id=collection_id, run_id=uuid4(), idempotency_key=str(uuid4())
    )
    item = ReviewItem(
        run_id=run_id,
        kind="register_update",
        target_key="payment_due_days",
        payload={"after": {"value": {"days": 30}}},
    )
    await repo.add_review_items([item])
    await repo.decide_review_items(run_id, "human-1", {item.id: "approved"})

    with pytest.raises(ValueError, match="already decided"):
        await repo.decide_review_items(run_id, "human-2", {item.id: "rejected"})

    listed = await repo.list_review_items(run_id)
    assert [(entry.state, entry.decided_by, entry.target_key) for entry in listed] == [
        ("approved", "human-1", "payment_due_days")
    ]


async def test_commit_is_replay_safe_and_versions_the_register(repo) -> None:
    collection_id = await repo.create_collection(f"pg-commit-{uuid4()}")
    run_id, _ = await repo.create_run(
        collection_id=collection_id, run_id=uuid4(), idempotency_key=str(uuid4())
    )
    document, _, candidate = await _seeded_fact(repo, collection_id, run_id)
    await _approved_item(repo, run_id, candidate, document.id)

    committed = (await repo.commit_approved(collection_id, run_id, basis_hash=_basis(run_id))).committed
    assert [(item.key, item.version) for item in committed] == [("payment_due_days", 1)]

    # A crash between commit and checkpoint replays this node; it must not bump again.
    replayed = (await repo.commit_approved(collection_id, run_id, basis_hash=_basis(run_id))).committed
    assert [(item.key, item.version) for item in replayed] == [("payment_due_days", 1)]

    register = await repo.list_register(collection_id)
    assert [(item.key, item.value, item.version) for item in register] == [
        ("payment_due_days", {"days": 30}, 1)
    ]

    # A second run over the same key is a real update: version advances, hash changes.
    second_run, _ = await repo.create_run(
        collection_id=collection_id, run_id=uuid4(), idempotency_key=str(uuid4())
    )
    await _approved_item(
        repo, second_run, candidate, document.id, value={"days": 45}, collection_id=collection_id
    )
    second_commit = (await repo.commit_approved(collection_id, second_run, basis_hash=_basis(second_run))).committed
    assert [(item.key, item.version) for item in second_commit] == [("payment_due_days", 2)]
    assert second_commit[0].content_hash != committed[0].content_hash


async def test_get_run_and_list_run_changes_reflect_a_commit(repo) -> None:
    """What the MCP/REST read tools show a caller who only holds a run id."""
    collection_id = await repo.create_collection(f"pg-run-summary-{uuid4()}")
    run_id, _ = await repo.create_run(
        collection_id=collection_id, run_id=uuid4(), idempotency_key=str(uuid4())
    )

    running = await repo.get_run(run_id)
    assert running is not None
    assert (running.collection_id, running.status, running.ended_at) == (
        collection_id,
        "running",
        None,
    )
    assert await repo.list_run_changes(run_id) == []

    document, _, candidate = await _seeded_fact(repo, collection_id, run_id)
    await _approved_item(repo, run_id, candidate, document.id)
    committed = (
        await repo.commit_approved(collection_id, run_id, basis_hash=_basis(run_id))
    ).committed

    done = await repo.get_run(run_id)
    assert done is not None
    assert done.status == "committed"
    assert done.ended_at is not None

    changes = await repo.list_run_changes(run_id)
    assert [(c.key, c.old_value, c.new_value, c.old_hash) for c in changes] == [
        (_scoped("payment_due_days"), None, {"days": 30}, None)
    ]
    assert changes[0].new_hash == committed[0].content_hash
    assert changes[0].register_item_id == committed[0].id

    assert await repo.get_run(uuid4()) is None


async def test_a_stale_proposal_is_refused_instead_of_overwriting(repo) -> None:
    """Two runs derive from the same empty slot; only the first may commit.

    The second's approval describes a change from "no value" to 45 days. By the time
    it commits the item says 30 days, which nobody approved replacing.
    """
    collection_id = await repo.create_collection(f"pg-stale-{uuid4()}")
    first_run, _ = await repo.create_run(
        collection_id=collection_id, run_id=uuid4(), idempotency_key=str(uuid4())
    )
    second_run, _ = await repo.create_run(
        collection_id=collection_id, run_id=uuid4(), idempotency_key=str(uuid4())
    )
    document, _, candidate = await _seeded_fact(repo, collection_id, first_run)
    await _approved_item(repo, first_run, candidate, document.id, before=None)
    await _approved_item(
        repo, second_run, candidate, document.id, value={"days": 45}, before=None
    )

    winner = await repo.commit_approved(collection_id, first_run, basis_hash=_basis(first_run))
    assert [(item.key, item.version) for item in winner.committed] == [("payment_due_days", 1)]
    assert winner.stale == []

    loser = await repo.commit_approved(collection_id, second_run, basis_hash=_basis(second_run))
    assert loser.committed == []
    assert [
        (item.key, item.expected_version, item.actual_version) for item in loser.stale
    ] == [(_scoped("payment_due_days"), None, 1)]

    register = await repo.list_register(collection_id)
    assert [(item.value, item.version) for item in register] == [({"days": 30}, 1)]

    # Re-deriving against what is stored now is what makes the same update land.
    third_run, _ = await repo.create_run(
        collection_id=collection_id, run_id=uuid4(), idempotency_key=str(uuid4())
    )
    await _approved_item(
        repo, third_run, candidate, document.id, value={"days": 45}, collection_id=collection_id
    )
    redone = await repo.commit_approved(collection_id, third_run, basis_hash=_basis(third_run))
    assert [(item.value, item.version) for item in redone.committed] == [({"days": 45}, 2)]
    assert redone.stale == []


async def test_unaffected_register_items_keep_their_hash(repo) -> None:
    collection_id = await repo.create_collection(f"pg-untouched-{uuid4()}")
    run_id, _ = await repo.create_run(
        collection_id=collection_id, run_id=uuid4(), idempotency_key=str(uuid4())
    )
    document, _, candidate = await _seeded_fact(repo, collection_id, run_id)
    await _approved_item(repo, run_id, candidate, document.id)
    await repo.commit_approved(collection_id, run_id, basis_hash=_basis(run_id))
    before = {item.key: item.content_hash for item in await repo.list_register(collection_id)}

    other_text = "Liability is capped at fees paid."
    other_run, _ = await repo.create_run(
        collection_id=collection_id, run_id=uuid4(), idempotency_key=str(uuid4())
    )
    other_doc, _, other_candidate = await _seeded_fact(
        repo,
        collection_id,
        other_run,
        text=other_text,
        key="liability_cap",
        filename="sow.txt",
        doc_type="sow",
    )
    await _approved_item(
        repo,
        other_run,
        other_candidate,
        other_doc.id,
        value={"cap": "fees"},
        collection_id=collection_id,
    )
    await repo.commit_approved(collection_id, other_run, basis_hash=_basis(other_run))

    after = {item.key: item.content_hash for item in await repo.list_register(collection_id)}
    assert after["payment_due_days"] == before["payment_due_days"]
    assert "liability_cap" in after


async def test_facts_keep_one_identity_across_replays(repo) -> None:
    collection_id = await repo.create_collection(f"pg-fact-id-{uuid4()}")
    run_id, _ = await repo.create_run(
        collection_id=collection_id, run_id=uuid4(), idempotency_key=str(uuid4())
    )
    _, _, candidate = await _seeded_fact(repo, collection_id, run_id)
    first_id = candidate.id
    assert first_id is not None

    candidate.id = None  # a resumed run rebuilds the candidate from the checkpoint
    await repo.put_facts(collection_id, [candidate])
    assert candidate.id == first_id


async def test_active_facts_carry_the_document_context(repo) -> None:
    collection_id = await repo.create_collection(f"pg-active-{uuid4()}")
    run_id, _ = await repo.create_run(
        collection_id=collection_id, run_id=uuid4(), idempotency_key=str(uuid4())
    )
    msa, _, _ = await _seeded_fact(repo, collection_id, run_id)
    amendment, _, _ = await _seeded_fact(
        repo,
        collection_id,
        run_id,
        text="Payment is due in 45 days.",
        doc_type="amendment",
        filename="amendment.txt",
    )
    await repo.link_supersession(amendment.id, msa.id)

    facts = await repo.get_active_facts(collection_id, ["payment_due_days"])
    by_document = {fact.document_id: fact for fact in facts}
    assert by_document[msa.id].doc_type == "master_agreement"
    assert by_document[msa.id].supersedes_id is None
    assert by_document[amendment.id].doc_type == "amendment"
    assert by_document[amendment.id].supersedes_id == msa.id

    assert await repo.find_supersession_target(collection_id, amendment.id) == msa.id


async def test_affected_keys_come_from_the_reverse_citation_index(repo) -> None:
    collection_id = await repo.create_collection(f"pg-affected-{uuid4()}")
    run_id, _ = await repo.create_run(
        collection_id=collection_id, run_id=uuid4(), idempotency_key=str(uuid4())
    )
    msa, _, candidate = await _seeded_fact(repo, collection_id, run_id)
    await _approved_item(repo, run_id, candidate, msa.id)
    await repo.commit_approved(collection_id, run_id, basis_hash=_basis(run_id))

    amendment, _, _ = await _seeded_fact(
        repo,
        collection_id,
        run_id,
        text="The notice period is revised to 30 days.",
        key="notice_days",
        doc_type="amendment",
        filename="amendment.txt",
    )
    await repo.link_supersession(amendment.id, msa.id)

    # The amendment never mentions payment terms, but the register item it would
    # invalidate cites the document it replaces.
    affected = await repo.affected_register_keys(
        collection_id, [_scoped("notice_days")], amendment.id
    )
    assert affected == [_scoped("notice_days"), _scoped("payment_due_days")]

    unrelated_doc, _, _ = await _seeded_fact(
        repo,
        collection_id,
        run_id,
        text="Amount Due: $10.",
        key="invoice_amount_due",
        doc_type="invoice",
        filename="invoice.txt",
    )
    assert await repo.affected_register_keys(
        collection_id, ["invoice_amount_due"], unrelated_doc.id
    ) == ["invoice_amount_due"]


async def test_conflicts_are_replay_safe_and_closed_only_by_approval(repo) -> None:
    collection_id = await repo.create_collection(f"pg-conflict-{uuid4()}")
    run_id, _ = await repo.create_run(
        collection_id=collection_id, run_id=uuid4(), idempotency_key=str(uuid4())
    )
    msa, _, incumbent = await _seeded_fact(repo, collection_id, run_id)
    invoice_doc, _, challenger = await _seeded_fact(
        repo,
        collection_id,
        run_id,
        text="Payment is due in 10 days.",
        doc_type="invoice",
        filename="invoice.txt",
    )
    conflict = Conflict(
        collection_id=collection_id,
        key="payment_due_days",
        fact_a_id=incumbent.id,
        fact_b_id=challenger.id,
        kind="contradiction",
        rationale="contract says 30, invoice says 10",
        detected_run=run_id,
    )
    stored = await repo.put_conflicts([conflict])
    replayed = await repo.put_conflicts(
        [
            Conflict(
                collection_id=collection_id,
                key="payment_due_days",
                fact_a_id=incumbent.id,
                fact_b_id=challenger.id,
                kind="contradiction",
                rationale="contract says 30, invoice says 10",
                detected_run=run_id,
            )
        ]
    )
    assert replayed[0].id == stored[0].id
    assert [c.state for c in await repo.list_conflicts(collection_id)] == ["open"]

    # A rejected proposal leaves the conflict open.
    rejected = ReviewItem(
        run_id=run_id,
        kind="conflict",
        target_key=_scoped("payment_due_days"),
        payload={
            "conflict": {"id": str(stored[0].id)},
            "after": {"value": {"days": 30}, "state": "disputed"},
        },
    )
    await repo.add_review_items([rejected])
    await repo.decide_review_items(run_id, "human-1", {rejected.id: "rejected"})
    await repo.commit_approved(collection_id, run_id, basis_hash=_basis(run_id))
    assert [c.state for c in await repo.list_conflicts(collection_id)] == ["open"]

    # An approved one closes it and marks the register item disputed.
    second_run, _ = await repo.create_run(
        collection_id=collection_id, run_id=uuid4(), idempotency_key=str(uuid4())
    )
    await _approved_item(
        repo,
        second_run,
        incumbent,
        invoice_doc.id,
        state="disputed",
        conflict={"id": str(stored[0].id)},
        collection_id=collection_id,
    )
    await repo.commit_approved(collection_id, second_run, basis_hash=_basis(second_run))

    assert await repo.list_conflicts(collection_id) == []
    items = await repo.get_register_items(collection_id, [_scoped("payment_due_days")])
    assert items[_scoped("payment_due_days")].state == "disputed"
    assert items[_scoped("payment_due_days")].citation_fact_ids == [incumbent.id]


async def test_citations_are_replaced_rather_than_accumulated(repo) -> None:
    collection_id = await repo.create_collection(f"pg-citations-{uuid4()}")
    run_id, _ = await repo.create_run(
        collection_id=collection_id, run_id=uuid4(), idempotency_key=str(uuid4())
    )
    msa, _, first = await _seeded_fact(repo, collection_id, run_id)
    await _approved_item(repo, run_id, first, msa.id)
    await repo.commit_approved(collection_id, run_id, basis_hash=_basis(run_id))

    amendment, _, second = await _seeded_fact(
        repo,
        collection_id,
        run_id,
        text="Payment is due in 45 days.",
        doc_type="amendment",
        filename="amendment.txt",
    )
    second_run, _ = await repo.create_run(
        collection_id=collection_id, run_id=uuid4(), idempotency_key=str(uuid4())
    )
    await _approved_item(
        repo, second_run, second, amendment.id, value={"days": 45}, collection_id=collection_id
    )
    await repo.commit_approved(collection_id, second_run, basis_hash=_basis(second_run))

    items = await repo.get_register_items(collection_id, [_scoped("payment_due_days")])
    # The superseded fact stops being cited, so the invalidation index stays clean.
    assert items[_scoped("payment_due_days")].citation_fact_ids == [second.id]
    assert items[_scoped("payment_due_days")].version == 2


async def test_rulesets_and_findings_survive_a_replay(repo) -> None:
    from doctask.domain import Finding, FindingCitation
    from doctask.services.ids import target_uuid
    from doctask.services.rules import parse_ruleset

    collection_id = await repo.create_collection(f"pg-rules-{uuid4()}")
    run_id, _ = await repo.create_run(
        collection_id=collection_id, run_id=uuid4(), idempotency_key=str(uuid4())
    )
    document, block, _ = await _seeded_fact(repo, collection_id, run_id)

    playbook = {
        "name": "playbook",
        "version": 1,
        "rules": [
            {"code": "PAY-01", "text": "at least 30 days", "severity": "major", "scope": "source"},
            {"code": "LIA-01", "text": "not exceed 500000", "severity": "minor", "scope": "both"},
        ],
    }
    ruleset = await repo.put_ruleset(parse_ruleset(playbook, collection_id))
    assert all(rule.id is not None for rule in ruleset.rules)

    # Re-uploading the same version keeps the rule ids, so findings stay addressable.
    again = await repo.put_ruleset(parse_ruleset(playbook, collection_id))
    assert [rule.id for rule in again.rules] == [rule.id for rule in ruleset.rules]

    active = await repo.get_active_ruleset(collection_id)
    assert active is not None
    assert [rule.code for rule in active.rules] == ["LIA-01", "PAY-01"]

    finding = Finding(
        run_id=run_id,
        rule_id=ruleset.rules[0].id,
        rule_code="PAY-01",
        target_kind="document",
        target_id=document.id,
        system_verdict="violation",
        rationale="30 is below the required minimum 45",
        severity="major",
        citations=[
            FindingCitation(
                block_id=block.id, quote=block.text, quote_start=0, quote_end=len(block.text)
            )
        ],
    )
    await repo.put_findings([finding])
    await repo.put_findings([finding])  # replayed stage

    stored = await repo.list_findings(run_id)
    assert len(stored) == 1
    assert stored[0].system_verdict == "violation"
    assert stored[0].severity == "major"
    assert [citation.quote for citation in stored[0].citations] == [block.text]
    assert stored[0].review_decision == "pending"
    assert stored[0].decided_by is None

    # A deliverable-stage finding targets the proposed register, not a document row.
    await repo.put_findings(
        [
            Finding(
                run_id=run_id,
                rule_id=ruleset.rules[1].id,
                rule_code="LIA-01",
                target_kind="register_item",
                target_id=target_uuid("register", str(collection_id)),
                system_verdict="pass",
                rationale="within the cap",
                severity="minor",
            )
        ]
    )
    assert {f.target_kind for f in await repo.list_findings(run_id)} == {"document", "register_item"}


async def test_approving_a_finding_records_it_without_touching_the_register(repo) -> None:
    from doctask.domain import Finding
    from doctask.services.rules import parse_ruleset

    collection_id = await repo.create_collection(f"pg-finding-review-{uuid4()}")
    run_id, _ = await repo.create_run(
        collection_id=collection_id, run_id=uuid4(), idempotency_key=str(uuid4())
    )
    document, _, _ = await _seeded_fact(repo, collection_id, run_id)
    ruleset = await repo.put_ruleset(
        parse_ruleset(
            {
                "name": "playbook",
                "version": 1,
                "rules": [
                    {
                        "code": "PAY-01",
                        "text": "at least 45 days",
                        "severity": "blocker",
                        "scope": "source",
                    }
                ],
            },
            collection_id,
        )
    )
    finding = Finding(
        run_id=run_id,
        rule_id=ruleset.rules[0].id,
        rule_code="PAY-01",
        target_kind="document",
        target_id=document.id,
        system_verdict="violation",
        rationale="below the minimum",
        severity="blocker",
    )
    await repo.put_findings([finding])

    item = ReviewItem(
        run_id=run_id,
        kind="finding",
        target_key="PAY-01",
        payload={"finding_id": str(finding.id), "system_verdict": "violation"},
    )
    await repo.add_review_items([item])
    await repo.decide_review_items(run_id, "human-1", {item.id: "approved"})
    await repo.record_finding_decisions(run_id, "human-1", {finding.id: "upheld"})

    committed = (await repo.commit_approved(collection_id, run_id, basis_hash=_basis(run_id))).committed

    assert committed == []  # a finding decision changes no register value
    upheld = await repo.list_findings(run_id)
    # The decision is recorded beside the verdict, with a name, and the verdict itself is
    # exactly what the evaluation wrote.
    assert [(f.system_verdict, f.review_decision, f.decided_by) for f in upheld] == [
        ("violation", "upheld", "human-1")
    ]
    assert [f.recheck_required for f in upheld] == [False]

    # Dismissing it later changes the decision, never the verdict, and flags a recheck.
    await repo.record_finding_decisions(run_id, "human-2", {finding.id: "dismissed"})
    dismissed = await repo.list_findings(run_id)
    assert [(f.system_verdict, f.review_decision, f.decided_by) for f in dismissed] == [
        ("violation", "dismissed", "human-2")
    ]
    assert [f.recheck_required for f in dismissed] == [True]
    assert dismissed[0].rationale == "below the minimum"


async def test_relations_and_fact_scope_survive_the_round_trip(repo) -> None:
    """Scope only prevents a false conflict if it reads back exactly as it was written.

    A scope that loses its parties or its obligation kind on the way through the database
    compares equal to everything, which is the pre-scope behaviour wearing the new field
    names -- silently, and only in production.
    """
    collection_id = await repo.create_collection(f"pg-scope-{uuid4()}")
    run_id, _ = await repo.create_run(
        collection_id=collection_id, run_id=uuid4(), idempotency_key=str(uuid4())
    )
    document, block, candidate = await _seeded_fact(repo, collection_id, run_id)
    scope = FactScope(
        agreement_id="MSA-2024-001",
        clause="4.3",
        parties=("Acme Buyer LLC", "Northstar Vendor Inc"),
        effective_from="2024-01-15",
        conditions=("unless disputed in good faith",),
        obligation_scope="contractual",
    )
    candidate.scope = scope
    await repo.put_facts(collection_id, [candidate])

    stored = await repo.get_active_facts(collection_id, ["payment_due_days"])
    assert [fact.scope for fact in stored] == [scope]

    await repo.set_agreement_ref(document.id, "MSA-2024-001")
    assert await repo.documents_by_agreement_ref(collection_id) == {
        "MSA-2024-001": document.id
    }
    assert (await repo.get_document(document.id)).agreement_ref == "MSA-2024-001"

    relation = DocumentRelation(
        document_id=document.id,
        kind="amends",
        target_ref="MSA-2024-001",
        evidence_quote="This Amendment amends Agreement No. MSA-2024-001.",
        block_id=block.id,
        quote_start=0,
        quote_end=49,
        target_document_id=document.id,
    )
    await repo.put_document_relations([relation, relation])  # replay writes one row

    assert await repo.list_document_relations(document.id) == [relation]


async def test_two_agreements_hold_separate_rows_for_the_same_obligation(repo) -> None:
    """The constraint that makes an agreement-scoped register real.

    `UNIQUE (collection_id, key)` gave the whole collection one `payment_due_days`, so a
    second agreement's term overwrote the first's -- with no conflict raised, because two
    terms in different agreements correctly are not one. Nothing here is derived: this is
    the storage layer being asked directly whether the two rows can coexist, and whether
    writing one leaves the other's hash and version alone.
    """
    collection_id = await repo.create_collection(f"pg-agreements-{uuid4()}")
    alpha_key = RegisterKey("MSA-A", "payment_due_days").text
    beta_key = RegisterKey("MSA-B", "payment_due_days").text

    async def commit(target_key, value):
        run_id, _ = await repo.create_run(
            collection_id=collection_id, run_id=uuid4(), idempotency_key=str(uuid4())
        )
        stored = (await repo.get_register_items(collection_id, [target_key])).get(target_key)
        item = ReviewItem(
            run_id=run_id,
            kind="register_update",
            target_key=target_key,
            payload={
                "before": (
                    {"content_hash": stored.content_hash, "version": stored.version}
                    if stored
                    else None
                ),
                "after": {"value": value, "state": "supported", "citation_fingerprints": []},
            },
        )
        await repo.add_review_items([item])
        await repo.decide_review_items(run_id, "human-1", {item.id: "approved"})
        return await repo.commit_approved(collection_id, run_id, basis_hash=_basis(run_id))

    await commit(alpha_key, {"days": 30})
    await commit(beta_key, {"days": 45})

    listed = [item.register_key.text for item in await repo.list_register(collection_id)]
    assert listed == [alpha_key, beta_key], "one row per agreement, in agreement order"
    register = await repo.get_register_items(collection_id, [alpha_key, beta_key])
    assert register[alpha_key].value == {"days": 30}
    assert register[beta_key].value == {"days": 45}
    assert register[alpha_key].id != register[beta_key].id
    assert {item.version for item in register.values()} == {1}

    # Amend Alpha. Beta's row is not versioned, not rewritten, not even locked.
    alpha_before = register[alpha_key]
    beta_before = register[beta_key]
    result = await commit(alpha_key, {"days": 60})

    assert [item.register_key.text for item in result.committed] == [alpha_key]
    assert result.stale == []
    after = await repo.get_register_items(collection_id, [alpha_key, beta_key])
    assert after[alpha_key].version == 2
    assert after[alpha_key].content_hash != alpha_before.content_hash
    assert after[beta_key].version == beta_before.version == 1
    assert after[beta_key].content_hash == beta_before.content_hash

    # The change log has one entry per real change, and it names Alpha's row alone.
    changes = await _query(
        repo,
        "SELECT register_item_id FROM change_log WHERE collection_id = %s ORDER BY id",
        collection_id,
    )
    assert [row["register_item_id"] for row in changes] == [
        alpha_before.id,
        beta_before.id,
        alpha_before.id,
    ]


async def _query(repo, sql, *params):
    async with repo.pool.connection() as conn:
        cur = await conn.execute(sql, params)
        return await cur.fetchall()

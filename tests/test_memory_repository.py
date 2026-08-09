from uuid import uuid4

import pytest

from doctask.domain import Document, ReviewItem
from doctask.repositories.memory import InMemoryRepository
from doctask.services.hashing import sha256_text


def _basis(run_id) -> str:
    """Commit is idempotent on (run, basis); a distinct basis per run keeps them apart."""
    return sha256_text(f"basis-{run_id}")



@pytest.mark.asyncio
async def test_document_dedupe_is_collection_scoped() -> None:
    repo = InMemoryRepository()
    c1 = await repo.create_collection("one")
    c2 = await repo.create_collection("two")
    text = "same bytes"

    _, first_dup = await repo.put_document(
        Document(c1, "a.txt", "text/plain", text, sha256_text(text))
    )
    _, second_dup = await repo.put_document(
        Document(c1, "b.txt", "text/plain", text, sha256_text(text))
    )
    _, other_collection_dup = await repo.put_document(
        Document(c2, "c.txt", "text/plain", text, sha256_text(text))
    )

    assert first_dup is False
    assert second_dup is True
    assert other_collection_dup is False


@pytest.mark.asyncio
async def test_a_stale_proposal_is_refused_instead_of_overwriting() -> None:
    repo = InMemoryRepository()
    collection_id = await repo.create_collection("stale")

    async def approve(run_id, value, before):
        item = ReviewItem(
            run_id=run_id,
            kind="register_update",
            target_key="payment_due_days",
            payload={"before": before, "after": {"value": value, "state": "supported"}},
        )
        await repo.add_review_items([item])
        await repo.decide_review_items(run_id, "human-1", {item.id: "approved"})

    # Both proposals were derived from the same empty slot.
    first, second = uuid4(), uuid4()
    await approve(first, {"days": 30}, None)
    await approve(second, {"days": 45}, None)

    assert (await repo.commit_approved(collection_id, first, basis_hash=_basis(first))).committed[0].version == 1
    refused = await repo.commit_approved(collection_id, second, basis_hash=_basis(second))
    assert refused.committed == []
    assert [(item.key, item.actual_version) for item in refused.stale] == [
        ("payment_due_days", 1)
    ]
    register = await repo.list_register(collection_id)
    assert [(item.value, item.version) for item in register] == [({"days": 30}, 1)]

    # Derived from what is stored now, the same update commits.
    stored = register[0]
    third = uuid4()
    await approve(
        third,
        {"days": 45},
        {"content_hash": stored.content_hash, "version": stored.version},
    )
    landed = await repo.commit_approved(collection_id, third, basis_hash=_basis(third))
    assert [(item.value, item.version) for item in landed.committed] == [({"days": 45}, 2)]


@pytest.mark.asyncio
async def test_review_decision_is_compare_and_set() -> None:
    repo = InMemoryRepository()
    run_id = uuid4()
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

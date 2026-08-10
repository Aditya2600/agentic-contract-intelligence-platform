from __future__ import annotations

from uuid import NAMESPACE_OID, UUID, uuid5


def slugify(name: str) -> str:
    """The client-facing identity of a collection: stable across repeats of the same
    name, so `create_collection` can be idempotent on it without a caller having to
    mint and remember a UUID themselves."""
    return name.strip().lower().replace(" ", "-")


def target_uuid(kind: str, key: str) -> UUID:
    """Stable UUID for a text-keyed target.

    `review_items.target_id` and `findings.target_id` are UUIDs, but register keys are
    text and a proposed key has no row yet. A deterministic uuid5 makes the uniqueness
    constraints mean "one decision (or finding) per key", and makes a replayed node
    address the same row instead of creating a second one.
    """
    return uuid5(NAMESPACE_OID, f"{kind}:{key}")

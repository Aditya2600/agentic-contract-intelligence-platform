from __future__ import annotations

import hashlib
import json
import re
from typing import Any


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def stage_input_hash(*parts: Any) -> str:
    """Identity of one unit of work: this stage, given exactly this.

    The ledger is keyed on it, so it has to change when the work changes and not
    otherwise. `retry_extract` re-enters `extract_facts` with a wider context on purpose:
    that is different work under the same stage name, and hashing the attempt number in
    is what keeps the retry from being mistaken for a replay of the first try.
    """
    return sha256_text(canonical_json([_hashable(part) for part in parts]))


def _hashable(value: Any) -> Any:
    if isinstance(value, dict | list | str | int | float | bool) or value is None:
        return value
    return str(value)


def candidate_basis_hash(rows: list[dict[str, Any]], *, ruleset_hash: str | None) -> str:
    """What a reviewer's decision was a decision *about*.

    Every deliverable verdict is a statement about one exact register -- these values, in
    these states, at these versions -- judged against one exact playbook. A decision that
    does not name that basis cannot be checked later, so an approval made against a
    register that has since moved reads identically to one made a second ago.

    Only the fields that could change the verdict go in. Citation ids do not: they are
    row identities, and a decision that went stale because a fact was re-inserted under a
    new id would be a false alarm every time.
    """
    return sha256_text(
        canonical_json(
            {
                "ruleset_hash": ruleset_hash,
                "rows": [
                    {
                        "key": row["scoped_key"],
                        "value": row["value"],
                        "state": row["state"],
                        "version": row.get("version"),
                    }
                    for row in sorted(rows, key=lambda row: row["scoped_key"])
                ],
            }
        )
    )


def register_content_hash(
    *, value: dict[str, Any], evidence_fingerprints: list[str], state: str
) -> str:
    """The identity of a register row: its value, its state, and what grounds it.

    Grounded by evidence fingerprint, never by `facts.id`. A row id is assigned by
    whichever insert happened to win; it changes when a collection is rebuilt from the
    same documents, and it stays the same when a fact is corrected in place. A hash built
    on ids therefore reports change where there was none and silence where there was --
    the exact opposite of what "unchanged" is supposed to prove.

    The digest check is the part that makes this structural rather than a convention: a
    UUID does not look like a SHA-256, so passing a citation id here fails loudly at the
    first commit instead of quietly producing a hash that means nothing.
    """
    for fingerprint in evidence_fingerprints:
        if not _DIGEST.fullmatch(fingerprint):
            raise ValueError(
                f"register content hash needs evidence fingerprints, got {fingerprint!r}; "
                "a database id is not evidence"
            )
    return sha256_text(
        canonical_json(
            {
                "value": value,
                "citation_ids": sorted(evidence_fingerprints),
                "state": state,
            }
        )
    )

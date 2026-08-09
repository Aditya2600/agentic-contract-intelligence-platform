from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from doctask.domain import StoredFact
from doctask.services.hashing import canonical_json


@dataclass(frozen=True, slots=True)
class ConflictProposal:
    key: str
    fact_a_id: UUID  # the incumbent value already in evidence
    fact_b_id: UUID  # the challenger introduced by the newer document
    # contradiction | supersession_candidate | ambiguous_scope
    kind: str
    rationale: str


@dataclass(frozen=True, slots=True)
class Derivation:
    """The value a key *should* take, plus why. Nothing here is committed:
    a derivation becomes a review item, and only an approved review item mutates
    the register."""

    key: str
    value: dict[str, Any] | None
    state: str  # supported | disputed | missing | ambiguous
    citation_fact_ids: list[UUID] = field(default_factory=list)
    citation_fingerprints: list[str] = field(default_factory=list)
    conflict: ConflictProposal | None = None
    reason: str = ""
    # Which agreement's copy of `key` this derives. "" is the unnamed bucket, used by a
    # collection that names no agreement at all. Part of the register row's identity.
    agreement_id: str = ""


def _citations(facts: list[StoredFact]) -> tuple[list[UUID], list[str]]:
    ids: list[UUID] = []
    fingerprints: list[str] = []
    for fact in facts:
        if fact.id not in ids:
            ids.append(fact.id)
        if fact.fingerprint not in fingerprints:
            fingerprints.append(fact.fingerprint)
    return ids, sorted(fingerprints)


def _describe_parallel(facts: list[StoredFact]) -> str:
    """One line naming evidence that was deliberately not compared, and why.

    Excluding a fact from a conflict is a judgement, and an unexplained exclusion is
    indistinguishable from a fact the system lost. Every fact left out of the comparison
    is named here, with its value and its scope, and it stays queryable by id.
    """
    return "; ".join(
        f"{fact.doc_type} {canonical_json(fact.value)} [{fact.scope.describe()}]"
        for fact in facts
    )


def _authoritative(facts: list[StoredFact]) -> list[StoredFact]:
    """The facts that get to set the register value for this key.

    A contract term outranks an operational restatement of it, so when both exist the
    register follows the contract and the invoice is evidence beside it rather than a
    competing value. When only operational facts exist -- `invoice_amount_due` has no
    contractual counterpart and never will -- they are all there is, and they decide.
    """
    contractual = [fact for fact in facts if fact.scope.obligation_scope == "contractual"]
    return contractual or facts


def _already_superseded(facts: list[StoredFact], new_document_id: UUID | None) -> set[UUID]:
    """Documents a *previous* run's replacement has already retired.

    A supersession this run is proposing stays in the comparison -- that argument is the
    proposal, and a human has to see both sides of it. One that landed runs ago is not a
    live disagreement, and leaving it in meant the term it replaced came back as a rival
    value on the next unrelated upload: an invoice, contributing nothing contractual at
    all, re-opened the MSA-versus-amendment question a human closed two runs earlier, and
    the register flipped back to the superseded number as `disputed`.
    """
    return {
        fact.supersedes_id
        for fact in facts
        if fact.supersedes_id is not None and fact.document_id != new_document_id
    }


def derive_key(
    key: str,
    facts: list[StoredFact],
    *,
    new_document_id: UUID | None = None,
    supersession_evidence: str | None = None,
    agreement_id: str = "",
    ambiguous_among: tuple[str, ...] = (),
) -> Derivation:
    """Derive one register key from the grounded facts that are about the same obligation.

    `facts` must be ordered oldest first. `supersession_evidence` is the sentence
    from the new document that claims to replace an earlier term, or None.

    `agreement_id` is which agreement's copy of `key` this call derives; the caller
    partitions the facts by agreement before calling. `ambiguous_among` is the set of
    agreements an *unnamed* group could belong to -- non-empty only when the collection
    holds more than one and this group named none, which is the case where committing a
    contractual value would be a guess about which contract it changes.

    Two facts under one key are not automatically two answers to one question. They are
    only in conflict when their scopes are comparable: the same agreement, the same
    parties, the same kind of obligation, the same conditions. Everything else is parallel
    evidence -- reported, cited by id, and never turned into a conflict a human has to
    close. See `FactScope.comparable_to`.
    """
    if not facts:
        return Derivation(
            key=key,
            value=None,
            state="missing",
            reason="no grounded evidence",
            agreement_id=agreement_id,
        )

    authoritative = _authoritative(facts)
    retired = _already_superseded(authoritative, new_document_id)
    authoritative = [f for f in authoritative if f.document_id not in retired] or authoritative
    incumbent_pool, challenger = _sides_in_scope(authoritative, new_document_id)
    in_scope = incumbent_pool

    # An unnamed contractual term facing more than one agreement it could belong to.
    # Which agreement it changes decides which row it writes, and nothing in the text
    # says. Committing anywhere would be a guess -- into a bucket of its own, which
    # quietly grows a second `payment_due_days` nobody asked for, or into one of the
    # named agreements, which rewrites a contract this document may never mention. So
    # nothing is derived and a human names the agreement.
    if len(ambiguous_among) > 1 and challenger.scope.obligation_scope == "contractual":
        ids, fingerprints = _citations(in_scope)
        return Derivation(
            key=key,
            value=None,
            state="ambiguous",
            citation_fact_ids=ids,
            citation_fingerprints=fingerprints,
            conflict=ConflictProposal(
                key=key,
                fact_a_id=in_scope[0].id,
                fact_b_id=challenger.id,
                kind="ambiguous_scope",
                rationale=(
                    f"{challenger.doc_type} states {canonical_json(challenger.value)} for "
                    f"{key} but names no agreement, and this collection holds "
                    f"{len(ambiguous_among)} it could belong to "
                    f"({', '.join(ambiguous_among)}). Which agreement it belongs to "
                    "decides which register row it writes; a human names it."
                ),
            ),
            reason=(
                f"agreement scope unresolved across {len(ambiguous_among)} agreements; "
                "no contractual value committed"
            ),
            agreement_id=agreement_id,
        )

    parallel = [fact for fact in facts if fact.id not in {f.id for f in in_scope}]
    parallel_note = (
        f"; {len(parallel)} fact(s) out of scope, not compared: {_describe_parallel(parallel)}"
        if parallel
        else ""
    )

    grouped: dict[str, list[StoredFact]] = {}
    for fact in in_scope:
        grouped.setdefault(canonical_json(fact.value), []).append(fact)

    if len(grouped) == 1:
        ids, fingerprints = _citations(in_scope)
        return Derivation(
            key=key,
            value=in_scope[0].value,
            state="supported",
            citation_fact_ids=ids,
            citation_fingerprints=fingerprints,
            reason=f"{len(in_scope)} in-scope source(s) agree; citations merged{parallel_note}",
            agreement_id=agreement_id,
        )

    incumbent = _incumbent(in_scope, challenger)

    superseding = (
        supersession_evidence is not None
        and challenger.document_id == new_document_id
        and challenger.supersedes_id == incumbent.document_id
    )

    if superseding:
        winning = grouped[canonical_json(challenger.value)]
        ids, fingerprints = _citations(winning)
        return Derivation(
            key=key,
            value=challenger.value,
            state="supported",
            citation_fact_ids=ids,
            citation_fingerprints=fingerprints,
            conflict=ConflictProposal(
                key=key,
                fact_a_id=incumbent.id,
                fact_b_id=challenger.id,
                kind="supersession_candidate",
                rationale=(
                    f"{challenger.doc_type} states: {supersession_evidence!r}. "
                    f"Proposes {canonical_json(challenger.value)} in place of "
                    f"{canonical_json(incumbent.value)}. A human decides whether the "
                    "supersession is legally effective."
                ),
            ),
            reason=(
                f"explicit supersession language proposes a replacement value{parallel_note}"
            ),
            agreement_id=agreement_id,
        )

    # No explicit supersession: the register keeps the incumbent value and is
    # marked disputed. The newer value is evidence, never an automatic override.
    ids, fingerprints = _citations(in_scope)
    return Derivation(
        key=key,
        value=incumbent.value,
        state="disputed",
        citation_fact_ids=ids,
        citation_fingerprints=fingerprints,
        conflict=ConflictProposal(
            key=key,
            fact_a_id=incumbent.id,
            fact_b_id=challenger.id,
            kind="contradiction",
            rationale=(
                f"{incumbent.doc_type} says {canonical_json(incumbent.value)}, "
                f"{challenger.doc_type} says {canonical_json(challenger.value)}, "
                f"both in scope {challenger.scope.describe()}, "
                "and no source claims to replace the other."
            ),
        ),
        reason=f"incompatible active values with no supersession claim{parallel_note}",
        agreement_id=agreement_id,
    )


def _sides_in_scope(
    authoritative: list[StoredFact], new_document_id: UUID | None
) -> tuple[list[StoredFact], StoredFact]:
    """The facts that compare against each other, and the one this run is proposing.

    The challenger is the newest fact the run's own document contributed; when the run
    contributed none that reach the register -- an invoice, whose terms are operational --
    the newest authoritative fact stands in, and the run's own fact ends up in the
    parallel set rather than opening a conflict with a contract it does not amend.
    """
    from_new = [fact for fact in authoritative if fact.document_id == new_document_id]
    challenger = from_new[-1] if from_new else authoritative[-1]
    in_scope = [fact for fact in authoritative if fact.scope.comparable_to(challenger.scope)]
    return in_scope, challenger


def _incumbent(in_scope: list[StoredFact], challenger: StoredFact) -> StoredFact:
    """The standing value the challenger disagrees with."""
    return next(
        (
            fact
            for fact in reversed(in_scope)
            if canonical_json(fact.value) != canonical_json(challenger.value)
        ),
        in_scope[0],
    )

"""A verbatim quote is not evidence for a value. These are the ways it can fail to be.

Every candidate below cites real characters at real offsets, so every one of them passes
`validate_citation`. What each gets wrong is the step after: whether the quote actually
says what the value claims. Left unchecked, all of them reach the register as grounded
facts with a citation a reviewer can click through to -- which is worse than an obvious
error, because the citation makes them look checked.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from langgraph.types import Command

from doctask.domain import Block, EvidenceSpan, FactCandidate, FactScope
from doctask.graph.builder import build_graph
from doctask.graph.nodes import NodeDependencies
from doctask.llm.fake import FakeLLM
from doctask.repositories.memory import InMemoryRepository
from doctask.services.citations import validate_citation
from doctask.services.grounding import (
    PARSER_VERSION,
    check_qualifiers,
    check_value,
    extend_to_qualifiers,
    span_for,
)
from doctask.services.hashing import register_content_hash, sha256_text
from doctask.services.rules import parse_ruleset

CORPUS = Path(__file__).resolve().parent.parent / "data" / "sample_data"
PLAYBOOK = json.loads((CORPUS / "rules.json").read_text())


def _block(text: str, **kwargs) -> Block:
    return Block(
        document_id=kwargs.pop("document_id", uuid4()),
        index=kwargs.pop("index", 0),
        text=text,
        text_sha256=sha256_text(text),
        char_start=0,
        char_end=len(text),
        **kwargs,
    )


def _candidate(block: Block, key: str, value: dict, quote: str) -> FactCandidate:
    start = block.text.index(quote)
    return FactCandidate(
        key=key,
        value=value,
        block_id=block.id,
        quote=quote,
        quote_start=start,
        quote_end=start + len(quote),
    )


# ------------------------------------------------------- the value against its quote


def test_a_nearby_number_is_not_the_term() -> None:
    """A clause is full of numbers that are not the obligation.

    "Section 4.3", a "(3) year" survival period, the year in a date. Each is real text at
    real offsets, so a citation to any of them resolves and looks sound. Only the number
    that carries the unit the value counts in can ground the value.
    """
    block = _block(
        "Section 4.3 Payment is due within 30 calendar days of receipt. "
        "This clause survives termination for three (3) years."
    )
    quote = "Section 4.3 Payment is due within 30 calendar days of receipt."

    # The citation itself is impeccable: those characters really are at those offsets.
    assert validate_citation(block, _candidate(block, "payment_due_days", {"days": 4}, quote)).ok
    truthful = check_value("payment_due_days", {"days": 30, "anchor": "receipt"}, quote)
    assert truthful.ok, truthful.reason

    # 4 and 3 are both in the quote. Neither is counted in days.
    assert check_value("payment_due_days", {"days": 4, "anchor": "receipt"}, quote).ok is False
    assert check_value("payment_due_days", {"days": 3, "anchor": "receipt"}, quote).ok is False
    assert "not as a day count" in check_value("payment_due_days", {"days": 4}, quote).reason
    # And a number the quote does not state at all.
    assert check_value("payment_due_days", {"days": 45}, quote).ok is False


def test_a_quote_that_denies_the_term_does_not_ground_it() -> None:
    """"Payment is not due within 30 calendar days" contains 30, and means the opposite."""
    denied = "Payment is not due within 30 calendar days of receipt"
    result = check_value("payment_due_days", {"days": 30, "anchor": "receipt"}, denied)
    assert result.ok is False
    assert "denies" in result.reason

    # A cap is drafted as a denial and is not one. Rejecting these would refuse every
    # liability cap in the corpus, which is why the phrase list exists.
    cap = "aggregate liability will not exceed two hundred fifty thousand U.S. dollars (USD 250,000)"
    assert check_value("liability_cap", {"amount": "$250,000", "currency": "USD"}, cap).ok
    assert check_value("payment_due_days", {"days": 30}, "payment due no later than 30 days").ok


def test_a_boolean_follows_the_polarity_of_its_quote() -> None:
    assert check_value("auto_renewal", {"renews": True}, "This agreement renews annually.").ok
    assert (
        check_value("auto_renewal", {"renews": True}, "This agreement does not renew.").ok is False
    )
    assert check_value("auto_renewal", {"renews": False}, "This agreement does not renew.").ok


def test_a_date_is_matched_however_the_contract_writes_it() -> None:
    assert check_value("term_end_date", {"date": "2026-12-31"}, "expires on 2026-12-31").ok
    assert check_value("term_end_date", {"date": "2026-12-31"}, "expires on December 31, 2026").ok
    assert (
        check_value("term_end_date", {"date": "2026-12-31"}, "expires on January 3, 2026").ok
        is False
    )


def test_the_anchor_has_to_be_in_the_quote_not_merely_in_the_document() -> None:
    """"30 days" from what? A term without its anchor is not an obligation."""
    assert check_value("payment_due_days", {"days": 30, "anchor": "receipt"}, "30 calendar days")\
        .ok is False
    assert check_value(
        "payment_due_days", {"days": 30, "anchor": "receipt"}, "30 calendar days of receipt"
    ).ok


# --------------------------------------------------------- qualifiers and exceptions


def test_a_cap_quoted_without_its_exception_is_not_the_cap_that_was_agreed() -> None:
    """The exception is the part that matters, and it is the part a short quote drops.

    "Liability is capped at $250,000" is true of the sentence and false of the contract:
    the cap does not apply to the cases the other party most cares about. A register that
    holds the number without the carve-out is not conservative, it is wrong.
    """
    text = (
        "Liability is capped at $250,000, except in cases of gross negligence "
        "or wilful misconduct."
    )
    short = text.index("Liability"), text.index(",")
    result = check_qualifiers(text, *short)
    assert result.ok is False
    assert "gross negligence" in result.reason

    # Quoted whole, it grounds the value.
    assert check_qualifiers(text, 0, len(text)).ok
    # And the extractor widens to it on its own, so the refusal is the backstop.
    assert extend_to_qualifiers(text, *short) == (short[0], len(text))


def test_a_payment_term_quoted_without_its_condition_is_stricter_than_the_contract() -> None:
    text = "Payment is due within 30 calendar days of receipt unless disputed in good faith."
    short = 0, text.index(" unless")

    assert check_qualifiers(text, *short).ok is False
    assert "unless disputed in good faith" in check_qualifiers(text, *short).reason
    assert extend_to_qualifiers(text, *short) == (0, len(text))
    # Nothing to preserve, nothing to widen.
    plain = "Payment is due within 30 calendar days of receipt."
    assert extend_to_qualifiers(plain, 0, len(plain)) == (0, len(plain))
    assert check_qualifiers(plain, 0, len(plain)).ok


# ------------------------------------------------------------------- evidence spans


def test_an_evidence_span_is_pinned_to_the_bytes_not_to_the_database() -> None:
    block = _block("Payment is due within 30 calendar days of receipt.", index=7, page=3)
    block.extraction_method = "native_pdf"
    quote = "Payment is due within 30 calendar days of receipt."
    candidate = _candidate(block, "payment_due_days", {"days": 30}, quote)

    span = span_for(block, candidate, document_sha256="doc-hash", extractor_version="fake-v1")

    assert span.document_sha256 == "doc-hash"
    assert span.block_index == 7
    assert span.page == 3
    assert (span.char_start, span.char_end) == (0, len(quote))
    assert span.quote_sha256 == sha256_text(quote)
    assert span.parser_version == f"native_pdf/{PARSER_VERSION}"
    assert EvidenceSpan.from_dict(span.as_dict()) == span

    # Re-ingesting the same bytes produces new row ids and the same fingerprint.
    reimported = _block(block.text, index=7, page=3)
    reimported.extraction_method = "native_pdf"
    again = span_for(
        reimported,
        _candidate(reimported, "payment_due_days", {"days": 30}, quote),
        document_sha256="doc-hash",
        extractor_version="fake-v1",
    )
    assert reimported.id != block.id
    assert again.fingerprint(key="payment_due_days", value={"days": 30}) == span.fingerprint(
        key="payment_due_days", value={"days": 30}
    )


def test_the_same_characters_read_by_a_different_parser_are_different_evidence() -> None:
    """A page a vision model transcribed is not the page a human would read.

    The characters can match and the confidence cannot. Sharing a fingerprint across the
    two would let an OCR reading inherit a register hash that a native read earned.
    """
    quote = "Payment is due within 30 calendar days of receipt."
    native, ocr = _block(quote), _block(quote)
    native.extraction_method = "native_pdf"
    ocr.extraction_method = "gemma_vlm"

    def fingerprint(block: Block) -> str:
        candidate = _candidate(block, "payment_due_days", {"days": 30}, quote)
        span = span_for(block, candidate, document_sha256="d", extractor_version="fake-v1")
        return span.fingerprint(key="payment_due_days", value={"days": 30})

    assert fingerprint(native) != fingerprint(ocr)


def test_a_register_hash_refuses_a_database_id() -> None:
    """The rule that keeps register hashes meaning what they claim to mean.

    A row id changes when a collection is rebuilt from the same documents and stays the
    same when a fact is corrected in place, so a hash built on ids reports change where
    there was none and silence where there was.
    """
    fingerprint = sha256_text("evidence")
    assert register_content_hash(
        value={"days": 30}, evidence_fingerprints=[fingerprint], state="supported"
    )

    with pytest.raises(ValueError, match="not evidence"):
        register_content_hash(
            value={"days": 30},
            evidence_fingerprints=[str(uuid4())],
            state="supported",
        )


# ----------------------------------------------------------------- end to end, refused


UNQUALIFIED = """MASTER SERVICES AGREEMENT

Section 4.3 Payment is due within 30 calendar days of receipt unless disputed in good faith.
"""


class _Misreading(FakeLLM):
    """An extractor that quotes real text and draws the wrong conclusion from it."""

    def __init__(self, value: dict, quote: str) -> None:
        super().__init__()
        self.value = value
        self.quote = quote

    async def extract(self, block: Block, *, wider_context: bool = False):
        if self.quote not in block.text:
            return []
        start = block.text.index(self.quote)
        return [
            FactCandidate(
                key="payment_due_days",
                value=self.value,
                block_id=block.id,
                quote=self.quote,
                quote_start=start,
                quote_end=start + len(self.quote),
            )
        ]


class _CorruptedOffsets(FakeLLM):
    """Right quote text, offsets shifted: the classic replay of a stale extraction."""

    async def extract(self, block: Block, *, wider_context: bool = False):
        candidates = await super().extract(block, wider_context=wider_context)
        for candidate in candidates:
            candidate.quote_start += 3
            candidate.quote_end += 3
        return candidates


async def _run(model, text: str = UNQUALIFIED) -> tuple[dict, InMemoryRepository]:
    repository = InMemoryRepository()
    collection_id = await repository.create_collection("acme")
    await repository.put_ruleset(parse_ruleset(PLAYBOOK, collection_id))
    graph = build_graph(NodeDependencies(repository=repository, model=model))
    run_id = uuid4()
    config = {"configurable": {"thread_id": str(run_id)}}
    result = await graph.ainvoke(
        {
            "run_id": str(run_id),
            "collection_id": str(collection_id),
            "idempotency_key": "doc",
            "input_document": {
                "filename": "agreement.txt",
                "mime_type": "text/plain",
                "text": text,
            },
            "validation_attempt": 0,
            "status": "running",
        },
        config=config,
    )
    while "report" not in result:
        pending = [
            item
            for item in await repository.list_review_items(run_id)
            if item.state == "pending"
        ]
        result = await graph.ainvoke(
            Command(
                resume={
                    "actor_id": "reviewer-1",
                    "actor_role": "reviewer",
                    "decisions": {str(item.id): "approved" for item in pending},
                    "override": False,
                }
            ),
            config=config,
        )
    repository.collection_id = collection_id
    return result["report"], repository


def _register_verdicts(report: dict) -> list[str]:
    return [
        finding["system_verdict"]
        for finding in report["rules"]["findings"]
        if finding["target_kind"] == "register_item"
    ]


@pytest.mark.parametrize(
    ("model", "label"),
    [
        (
            _Misreading({"days": 4, "anchor": "receipt"}, "Section 4.3 Payment is due"),
            "a section number read as a payment term",
        ),
        (
            _Misreading(
                {"days": 30, "anchor": "acceptance"},
                "Payment is due within 30 calendar days of receipt unless disputed in good faith.",
            ),
            "an anchor the quote does not state",
        ),
        (
            _Misreading(
                {"days": 30, "anchor": "receipt"},
                "Payment is due within 30 calendar days of receipt",
            ),
            "a quote cut short of the condition that qualifies it",
        ),
        (_CorruptedOffsets(), "offsets that no longer point at the quote"),
    ],
)
async def test_an_ungrounded_value_can_neither_commit_nor_pass(model, label) -> None:
    """Nothing here reaches the register, and nothing here produces a PASS.

    Both halves matter. Refusing to commit but still reporting a passing rule would leave
    the run looking clean over evidence the system itself rejected, which is the exact
    shape of failure the explicit-verdict discipline exists to prevent.
    """
    report, repository = await _run(model)

    assert report["unsupported_count"] >= 1, label
    assert await repository.list_register(repository.collection_id) == [], label
    assert report["committed_keys"] == [], label
    assert "pass" not in _register_verdicts(report), label


async def test_the_same_document_read_correctly_does_commit() -> None:
    """The control. Without it the tests above are satisfied by a pipeline that refuses
    everything, which is not the property being claimed."""
    report, repository = await _run(FakeLLM())

    assert report["unsupported_count"] == 0
    assert report["committed_keys"] == ["::payment_due_days"]
    assert _register_verdicts(report) == ["pass"]

    facts = await repository.get_active_facts(repository.collection_id, ["payment_due_days"])
    assert len(facts) == 1
    # The condition survived into the quote, so the register's citation carries it.
    assert "unless disputed in good faith" in facts[0].quote
    assert facts[0].scope.conditions == ("unless disputed in good faith",)

    span = facts[0].evidence
    assert span is not None
    assert span.quote_sha256 == sha256_text(facts[0].quote)
    assert facts[0].fingerprint == span.fingerprint(
        key="payment_due_days", value=facts[0].value
    )
    # The register cites the evidence fingerprint, and its content hash is built from it.
    item = (await repository.list_register(repository.collection_id))[0]
    assert item.citation_fingerprints == [facts[0].fingerprint]
    assert item.content_hash == register_content_hash(
        value=item.value, evidence_fingerprints=item.citation_fingerprints, state=item.state
    )


async def test_a_fact_the_validator_refused_is_kept_but_never_cited() -> None:
    """Refused is not deleted. The proposal stays auditable and stays out of the register."""
    report, repository = await _run(
        _Misreading({"days": 4, "anchor": "receipt"}, "Section 4.3 Payment is due")
    )

    assert report["status"] in {"committed", "duplicate_noop"}
    assert report["affected_keys"] == []
    stored = await repository.get_active_facts(repository.collection_id, ["payment_due_days"])
    assert stored == []
    # The refusal itself is on the record, with the reason a reviewer needs.
    events = await repository.list_events(UUID(report["run_id"]))
    reasons = [event.reason for event in events if event.stage == "validate_citations"]
    assert any("failed deterministic validation" in reason for reason in reasons)


def test_scope_conditions_and_quote_qualifiers_agree() -> None:
    """Two readings of the same sentence that must not diverge.

    `FactScope.conditions` is what decides whether two terms are comparable;
    `check_qualifiers` is what decides whether the quote is honest. If the quote can pass
    while the sentence carries a condition the scope missed, an unconditional term and a
    conditional one become indistinguishable.
    """
    from doctask.services.scoping import scope_for

    text = "Payment is due within 30 calendar days of receipt unless disputed in good faith."
    scope = scope_for(
        doc_type="master_agreement",
        document_text=text,
        block_text=text,
        quote=text,
        agreement_id=None,
        amends=False,
    )
    assert scope.conditions == ("unless disputed in good faith",)
    assert check_qualifiers(text, 0, len(text)).ok
    assert FactScope().conditions == ()

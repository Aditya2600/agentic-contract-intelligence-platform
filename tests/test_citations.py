from uuid import uuid4

from doctask.domain import Block, FactCandidate
from doctask.services.citations import validate_citation


def test_verbatim_citation_passes() -> None:
    text = "Payment is due within 30 calendar days of receipt."
    block = Block(
        document_id=uuid4(),
        index=0,
        text=text,
        text_sha256="x",
        char_start=0,
        char_end=len(text),
    )
    quote = "Payment is due within 30 calendar days"
    candidate = FactCandidate(
        key="payment_due_days",
        value={"days": 30},
        block_id=block.id,
        quote=quote,
        quote_start=0,
        quote_end=len(quote),
        fingerprint="f",
    )
    assert validate_citation(block, candidate).ok


def test_non_verbatim_citation_fails() -> None:
    text = "Payment is due within 30 calendar days."
    block = Block(
        document_id=uuid4(),
        index=0,
        text=text,
        text_sha256="x",
        char_start=0,
        char_end=len(text),
    )
    candidate = FactCandidate(
        key="payment_due_days",
        value={"days": 45},
        block_id=block.id,
        quote="Payment is due within 45 calendar days",
        quote_start=0,
        quote_end=39,
        fingerprint="f",
    )
    result = validate_citation(block, candidate)
    assert not result.ok
    assert "verbatim" in result.reason

from uuid import uuid4

import pytest

from doctask.domain import Block, Rule
from doctask.llm.base import RuleExcerpt
from doctask.llm.fake import FakeLLM
from doctask.services.citations import validate_citation
from doctask.services.hashing import sha256_text


@pytest.mark.asyncio
async def test_fake_extractor_emits_grounded_payment_term() -> None:
    text = "Payment is due within 30 calendar days of receipt."
    block = Block(
        document_id=uuid4(),
        index=0,
        text=text,
        text_sha256=sha256_text(text),
        char_start=0,
        char_end=len(text),
    )
    candidates = await FakeLLM().extract(block)
    assert len(candidates) == 1
    assert candidates[0].value["days"] == 30
    assert validate_citation(block, candidates[0]).ok


# The offline model reads real contract prose in `scripts/run_demo.py`, which is how
# these cases were found. Each one silently produced a wrong answer, not an error.


@pytest.mark.asyncio
async def test_a_contract_is_typed_by_its_title_not_by_what_it_mentions() -> None:
    """Every real document in the pack names the others. Matching those words anywhere
    in the body typed the MSA, the invoice and an operational notice as amendments."""
    msa = (
        "MASTER SERVICES AGREEMENT\nMSA-2026-014\n\n"
        "This Agreement may be modified only by a signed amendment to the Agreement."
    )
    assert (await FakeLLM().classify(msa))[0] == "master_agreement"

    notice = (
        "ASTERPEAK CLOUD SERVICES PVT. LTD.\n"
        "Customer Operations Notice\n"
        "Reference: OPS-NOTICE-2026-0528\n"
        "Date: 28 May 2026\n\n"
        "To: Northstar Retail Technologies Pvt. Ltd.\n"
        "Agreement reference: MSA-2026-014\n\n"
        "Subject: Scheduled maintenance window - 31 May 2026\n\n"
        "Dear Northstar Operations Team,\n\n"
        "AsterPeak will perform scheduled maintenance on 31 May 2026.\n\n"
        "This notice is operational only. It does not amend the Master Services "
        "Agreement, Amendment No. 1, pricing, payment terms, or liability limits."
    )
    # No type marker in the title block, so it goes to a human rather than being
    # guessed at from the sentence that disclaims amending anything.
    doc_type, confidence, _ = await FakeLLM().classify(notice)
    assert (doc_type, confidence < 0.70) == ("unknown", True)


@pytest.mark.asyncio
async def test_a_clause_is_judged_on_the_number_that_carries_the_rule_s_unit() -> None:
    """A clause is full of numbers that are not the term: section numbers, dates, a
    survival period. First-number-wins read "Section 4.3" as a three-day payment term
    and reported a violation against a compliant contract."""
    rule = Rule(
        code="PAY-01",
        text="Payment terms must allow at least 30 calendar days before undisputed "
        "amounts are due.",
        severity="major",
        scope="source",
        id=uuid4(),
    )
    excerpt = RuleExcerpt(
        index=1,
        label="4. Payment Terms",
        text=(
            "4.3 Undisputed invoice amounts are due thirty (30) calendar days after "
            "Customer's receipt of a correct invoice. Confidentiality obligations "
            "survive termination for three (3) years."
        ),
        block_id=uuid4(),
    )
    verdict = await FakeLLM().evaluate_rule(rule, target_label="the MSA", excerpts=[excerpt])
    assert verdict.verdict == "pass"
    assert "30 satisfies" in verdict.rationale


@pytest.mark.asyncio
async def test_a_wrapped_clause_keeps_the_cap_attached_to_the_liability_it_caps() -> None:
    """A PDF breaks a line where the column ends, and "U.S." is not a sentence end."""
    rule = Rule(
        code="LIA-01",
        text="The general liability cap must not exceed USD 500,000 without separate "
        "legal approval.",
        severity="major",
        scope="source",
        id=uuid4(),
    )
    excerpt = RuleExcerpt(
        index=1,
        label="10. Limitation of Liability",
        text=(
            "10.1 Except for Excluded Claims, each party's aggregate liability arising "
            "out of or relating to this Agreement \nwill not exceed two hundred fifty "
            "thousand U.S. dollars (USD 250,000)."
        ),
        block_id=uuid4(),
    )
    verdict = await FakeLLM().evaluate_rule(rule, target_label="the MSA", excerpts=[excerpt])
    assert verdict.verdict == "pass"
    assert "250000 satisfies" in verdict.rationale


@pytest.mark.asyncio
async def test_a_rule_is_not_judged_against_a_clause_that_only_shares_a_unit() -> None:
    """An invoice payment line shares "calendar days" with a termination-notice rule and
    nothing else. Judging the rule against it reported a breach the invoice never made."""
    rule = Rule(
        code="TERM-01",
        text="Convenience termination notice must be at least 60 calendar days.",
        severity="minor",
        scope="source",
        id=uuid4(),
    )
    excerpt = RuleExcerpt(
        index=1,
        label="Invoice",
        text="Payment Terms: NET 10 - payment due within ten (10) calendar days of invoice date.",
        block_id=uuid4(),
    )
    verdict = await FakeLLM().evaluate_rule(rule, target_label="the invoice", excerpts=[excerpt])
    assert verdict.verdict == "insufficient_evidence"

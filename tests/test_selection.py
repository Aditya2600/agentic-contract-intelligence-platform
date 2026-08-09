"""Bounded, deterministic evidence selection.

The bound is what keeps cost proportional to the rules rather than to the corpus, and
determinism is what lets a finding be re-derived during an audit.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from doctask.domain import Block, Rule
from doctask.services.hashing import sha256_text
from doctask.services.selection import select_excerpts

DOCUMENT = uuid4()

PAYMENT = "Payment is due within 30 calendar days of receipt of a valid invoice."
LIABILITY = "Aggregate liability is capped at $250,000."
NOTICE = "Either party may terminate on 60 days' written notice."
NOISE = "The parties acknowledge that this agreement is governed by the laws of Delaware."


def _blocks(*texts: str) -> list[Block]:
    return [
        Block(
            document_id=DOCUMENT,
            index=index,
            text=text,
            text_sha256=sha256_text(text),
            char_start=0,
            char_end=len(text),
        )
        for index, text in enumerate(texts)
    ]


def _rule(text: str) -> Rule:
    return Rule(code="R", text=text, severity="major", scope="source")


def test_the_relevant_block_is_selected_over_noise() -> None:
    blocks = _blocks(NOISE, NOISE, PAYMENT, NOISE)
    rule = _rule("Payment terms must be at least 30 calendar days after receipt.")

    excerpts = select_excerpts(rule, blocks, max_blocks=1, max_chars=10_000)

    assert [excerpt.text for excerpt in excerpts] == [PAYMENT]
    assert excerpts[0].block_id == blocks[2].id


def test_selection_is_bounded_by_block_count_and_characters() -> None:
    blocks = _blocks(*([PAYMENT] * 20))
    rule = _rule("Payment terms must be at least 30 calendar days after receipt.")

    assert len(select_excerpts(rule, blocks, max_blocks=3, max_chars=10_000)) == 3
    # Two blocks fit in the character budget, the third does not.
    tight = select_excerpts(rule, blocks, max_blocks=20, max_chars=2 * len(PAYMENT) + 5)
    assert len(tight) == 2


def test_excerpts_keep_document_order_and_contiguous_indices() -> None:
    blocks = _blocks(NOISE, LIABILITY, NOISE, PAYMENT)
    rule = _rule("Payment terms of at least 30 days and a liability cap are required.")

    excerpts = select_excerpts(rule, blocks, max_blocks=2, max_chars=10_000)

    # Ranked by relevance, then re-sorted so the model reads them as the document reads.
    assert [excerpt.text for excerpt in excerpts] == [LIABILITY, PAYMENT]
    assert [excerpt.index for excerpt in excerpts] == [0, 1]


def test_selection_is_deterministic() -> None:
    blocks = _blocks(NOISE, PAYMENT, LIABILITY, NOTICE, NOISE)
    rule = _rule("Payment terms must be at least 30 calendar days after receipt.")

    first = select_excerpts(rule, blocks, max_blocks=3, max_chars=10_000)
    again = select_excerpts(rule, blocks, max_blocks=3, max_chars=10_000)

    assert [e.block_id for e in first] == [e.block_id for e in again]


def test_a_rule_about_an_uncovered_subject_still_sees_something() -> None:
    """It has to be able to reach `insufficient_evidence`, which needs evidence to read."""
    blocks = _blocks(NOISE, NOISE)
    rule = _rule("Data must be deleted within 14 days of termination.")

    excerpts = select_excerpts(rule, blocks, max_blocks=2, max_chars=10_000)

    assert len(excerpts) == 2


def test_a_block_larger_than_the_whole_budget_is_skipped_not_truncated() -> None:
    """Truncating a block would leave a quote that is verbatim in nothing."""
    blocks = _blocks("x" * 500 + PAYMENT, LIABILITY)
    rule = _rule("Payment terms must be at least 30 calendar days after receipt.")

    excerpts = select_excerpts(rule, blocks, max_blocks=5, max_chars=100)

    assert [excerpt.text for excerpt in excerpts] == [LIABILITY]


@pytest.mark.parametrize(("blocks_limit", "chars_limit"), [(0, 100), (5, 0), (-1, 100)])
def test_nonsense_bounds_are_rejected(blocks_limit, chars_limit) -> None:
    with pytest.raises(ValueError, match="positive"):
        select_excerpts(
            _rule("anything"), _blocks(PAYMENT), max_blocks=blocks_limit, max_chars=chars_limit
        )

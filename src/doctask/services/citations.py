from __future__ import annotations

from doctask.domain import Block, FactCandidate
from doctask.services.grounding import Grounding


def validate_citation(block: Block, candidate: FactCandidate) -> Grounding:
    """Whether the quote is real: those characters, at those offsets, in that block.

    Says nothing about whether the quote supports the value -- that is `check_value`.
    """
    if candidate.block_id != block.id:
        return Grounding(False, "candidate cites a different block")
    if candidate.quote_start < 0 or candidate.quote_end > len(block.text):
        return Grounding(False, "quote offsets are outside the source block")
    if candidate.quote_end <= candidate.quote_start:
        return Grounding(False, "quote offsets are empty or reversed")
    actual = block.text[candidate.quote_start : candidate.quote_end]
    if actual != candidate.quote:
        return Grounding(False, "quote is not verbatim at the submitted offsets")
    if not candidate.quote.strip():
        return Grounding(False, "quote is empty")
    return Grounding(True, "verbatim quote and offsets validated")

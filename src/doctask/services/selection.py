"""Bounded, deterministic selection of the evidence one rule is evaluated against.

Sending a whole contract to the model for every rule costs money proportional to the
corpus and, past the context window, silently truncates - which is the failure mode that
turns a violation into a `pass`. So each rule gets a bounded, scored slice instead, and
the bound is explicit configuration rather than whatever the server happens to accept.

Selection is term overlap, not a model call: the choice of evidence must be reproducible
from the same inputs, or a finding cannot be re-derived during an audit.
"""

from __future__ import annotations

import re

from doctask.domain import Block, Rule
from doctask.llm.base import RuleExcerpt

_WORD = re.compile(r"[a-z][a-z0-9-]{2,}")

# Words that appear in almost any rule and would score every block equally.
_STOPWORDS = frozenset(
    (  # noqa: SIM905 - a wrapped string reads as a word list; 40 quoted items does not
        "the and for any all must not may shall with within than from that this "
        "which are was were has have had its their there then when where while "
        "each other more less least most such upon into out per been being "
        "also only very much many some can could would should will does did done "
        "required requires require exceed exceeds"
    ).split()
)


def _terms(text: str) -> set[str]:
    return {word for word in _WORD.findall(text.lower()) if word not in _STOPWORDS}


def select_excerpts(
    rule: Rule,
    blocks: list[Block],
    *,
    max_blocks: int,
    max_chars: int,
) -> list[RuleExcerpt]:
    """Return at most `max_blocks` excerpts, within `max_chars` total, in document order.

    Blocks are ranked by how many of the rule's distinctive terms they contain, ties
    broken by document order so the same inputs always produce the same evidence. Blocks
    that share no term with the rule are still eligible once the scoring blocks run out:
    a rule about a subject the document never names has to be able to reach
    `insufficient_evidence`, and it can only do that by seeing something.
    """
    if max_blocks <= 0 or max_chars <= 0:
        raise ValueError("rule context bounds must be positive")

    # A quarantined block is never rule context. Its text is the payload, and a rule
    # prompt is a second door into the same model -- withholding it from extraction and
    # then handing it over here would be the same failure with an extra step. A rule left
    # with nothing to read reaches `insufficient_evidence`, which is the honest answer.
    blocks = [block for block in blocks if not block.injection_flag]

    wanted = _terms(rule.text)
    scored = sorted(
        ((len(wanted & _terms(block.text)), -index, block) for index, block in enumerate(blocks)),
        key=lambda row: (row[0], row[1]),
        reverse=True,
    )

    chosen: list[Block] = []
    budget = max_chars
    for _, _, block in scored:
        if len(chosen) >= max_blocks:
            break
        if len(block.text) > budget:
            continue
        chosen.append(block)
        budget -= len(block.text)

    chosen.sort(key=lambda block: block.index)
    return [
        RuleExcerpt(index=position, label=f"block {block.index}", text=block.text,
                    block_id=block.id)
        for position, block in enumerate(chosen)
    ]

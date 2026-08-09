from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

from doctask.domain import Block, DocType, FactCandidate, Rule

Verdict = Literal["violation", "pass", "insufficient_evidence"]


@dataclass(frozen=True, slots=True)
class RuleExcerpt:
    """One numbered piece of evidence offered to the model for a single rule.

    The model cites an excerpt by `index`, never by UUID: an index it can copy correctly
    and the caller can check, where a mangled UUID would silently ground nothing.
    `block_id` is the source block behind the excerpt, or None when the excerpt is a
    register row rather than document text.
    """

    index: int
    label: str
    text: str
    block_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class RuleVerdict:
    """A model's opinion about one rule. `quote` and `excerpt_index` are claims, not a
    citation: the caller still has to confirm that the named excerpt exists and that the
    quote is verbatim inside it before a violation may be recorded."""

    verdict: Verdict
    rationale: str
    quote: str | None = None
    excerpt_index: int | None = None


class ModelGateway(Protocol):
    async def classify(self, text: str) -> tuple[DocType, float, str]: ...

    async def extract(self, block: Block, *, wider_context: bool = False) -> list[FactCandidate]: ...

    async def evaluate_rule(
        self,
        rule: Rule,
        *,
        target_label: str,
        excerpts: list[RuleExcerpt],
        statement: str | None = None,
    ) -> RuleVerdict:
        """Judge `rule` against `statement` if one is given, otherwise against the
        excerpts themselves, and cite an excerpt either way.

        The deliverable stage judges a derived register value but has to cite the source
        text behind it, so the thing under judgement and the evidence for it are separate
        arguments. Quoting the derived value back would prove the rendering, not the term.
        """
        ...

    async def read_page(self, image_png: bytes, *, page: int) -> str:
        """Transcribe a rendered page image, for pages no native extractor could read.

        Returns the empty string when nothing legible is on the page. It must not
        paraphrase or summarise: the output becomes block text that facts are quoted
        from, and a quote that is not in the source is rejected downstream anyway.
        """
        ...

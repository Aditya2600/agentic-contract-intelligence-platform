"""Whether a quote actually says what the fact extracted from it claims.

`validate_citation` proves the quote is real: those characters are at those offsets in
that block. It cannot tell whether the quote *supports the value*, and that gap is where
the dangerous failures live. Every one of these passes a verbatim-quote check:

  - the value reads a number the quote does not carry -- a nearby section number, a
    survival period, the year -- and the citation still resolves to real text;
  - the quote negates the very term the value asserts ("payment is **not** due within 30
    calendar days") and the value records 30 days as the obligation;
  - the quote is cut short of the exception that guts it ("capped at $250,000" from a
    sentence ending "except in cases of gross negligence"), so the register holds a cap
    that does not apply where it matters most;
  - the quote drops the anchor, and "30 days" stops meaning anything in particular.

So this module re-reads the quote and refuses the value it cannot find there. Everything
here is deterministic regex over stored text: a check that asks a model whether the model
was right is not a check.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from doctask.domain import Block, EvidenceSpan, FactCandidate
from doctask.services.hashing import sha256_text

# Bumped whenever a change here would make the same bytes produce a different span. It is
# part of every evidence fingerprint, so a re-parse under new rules cannot silently reuse
# a register hash that was built under the old ones.
PARSER_VERSION = "1"


@dataclass(frozen=True, slots=True)
class Grounding:
    ok: bool
    reason: str


# Negations that do not negate: contract drafting states a cap as "shall not exceed" and a
# deadline as "not later than". Reading those as denials would reject every liability cap
# in the corpus, so they are removed before the quote is searched for a real denial.
_CAP_PHRASE = re.compile(
    r"\b(?:not\s+(?:to\s+)?exceed|no(?:t)?\s+(?:more|less|later|earlier|greater|fewer)\s+than|"
    r"not\s+to\s+be\s+exceeded)\b",
    re.IGNORECASE,
)
_NEGATION = re.compile(r"\b(?:not|never|nor|neither|without|no)\b", re.IGNORECASE)

# A qualifier makes the term hold only sometimes; an exception carves a hole in it. Either
# one changes what the number means, so a quote that omits it is not evidence for the
# value -- it is evidence for a stronger claim than the contract makes.
_QUALIFIER = re.compile(
    r"\b(?:unless|except(?:\s+(?:for|where|that|in|as))?|provided\s+that|subject\s+to|"
    r"other\s+than|save\s+(?:for|that)|only\s+(?:if|when|where)|contingent\s+upon)\b",
    re.IGNORECASE,
)
# What a period is counted from. "30 days" on its own is not an obligation.
_ANCHOR_CUE = re.compile(
    r"\b(?:of|after|from|following|upon|prior\s+to)\s+(?:the\s+)?(?:[\w']+\s+){0,3}"
    r"(?:receipt|invoice|delivery|acceptance|notice|date|termination|expiry|expiration)\b",
    re.IGNORECASE,
)

_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+(?=[\"“(A-Z0-9])")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_MONTHS = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)

# Fields whose name says what unit the number carries. Anything else is compared as a
# bare number, which is weaker but still refuses a value the quote never states.
_DAY_FIELDS = frozenset({"days", "notice_days", "due_days"})
_DATE_FIELDS = frozenset({"date", "effective_from", "effective_to", "end_date"})
# Not evidence in itself: a currency code or an anchor word is a label on the number, and
# it is checked separately rather than searched for as a literal.
_CURRENCY_FIELDS = frozenset({"currency"})
_ANCHOR_FIELDS = frozenset({"anchor"})


def _sentence_bounds(text: str, start: int, end: int) -> tuple[int, int]:
    """The sentence containing `text[start:end]`, as offsets into `text`."""
    bounds = [0] + [match.end() for match in _SENTENCE_BREAK.finditer(text)] + [len(text)]
    lower = max((b for b in bounds if b <= start), default=0)
    upper = min((b for b in bounds if b >= end), default=len(text))
    return lower, upper


def extend_to_qualifiers(text: str, start: int, end: int) -> tuple[int, int]:
    """Grow a quote to the end of its sentence when the rest of it changes the meaning.

    An extractor is asked for the shortest quote that carries the evidence, and shortest
    is usually right. It stops being right the moment the remainder of the sentence says
    "unless disputed in good faith", "except in cases of gross negligence", or "after
    Customer's receipt of a correct invoice": the number alone then overstates what was
    agreed. Widening here is cheap; `check_qualifiers` is what refuses the ones that are
    not widened.
    """
    _, upper = _sentence_bounds(text, start, end)
    remainder = text[end:upper]
    if _QUALIFIER.search(remainder) or _ANCHOR_CUE.search(remainder):
        return start, upper
    return start, end


def _affirms(quote: str, position: int) -> bool:
    """Whether the quote asserts, rather than denies, whatever sits at `position`.

    A denial before the number is what matters: "payment is not due within 30 calendar
    days" carries the number 30 and means the opposite of a 30-day term.
    """
    masked = _CAP_PHRASE.sub(lambda match: " " * len(match.group(0)), quote)
    return not any(match.start() < position for match in _NEGATION.finditer(masked))


def _number_positions(quote: str, number: float) -> list[int]:
    """Where this number appears in the quote, written as a contract writes numbers."""
    rendered = f"{number:g}"
    whole = rendered.split(".")[0]
    grouped = f"{int(whole):,}" if whole.lstrip("-").isdigit() else whole
    positions: list[int] = []
    for form in {rendered, whole, grouped}:
        positions += [
            match.start()
            for match in re.finditer(rf"(?<![\d.,]){re.escape(form)}(?![\d])", quote)
        ]
    return sorted(set(positions))


def _has_day_unit(quote: str, position: int) -> bool:
    """A day count has to be counted in days. "Section 4.3" is not a three-day term."""
    return bool(re.search(r"^\s*\)?\s*(?:[a-z]+\s+){0,2}days?\b", quote[position:], re.IGNORECASE))


def _has_currency(quote: str) -> bool:
    return bool(re.search(r"(?:USD|US\$|\$|EUR|GBP|€|£|dollars?|euros?|pounds?)", quote, re.IGNORECASE))


def _date_in(quote: str, raw: str) -> bool:
    """The date as ISO, or written out the way a contract writes it."""
    if raw in quote:
        return True
    match = _ISO_DATE.fullmatch(raw)
    if match is None:
        return False
    year, month, day = raw.split("-")
    name = _MONTHS[int(month) - 1]
    written = re.compile(
        rf"\b{name}\s+0?{int(day)}(?:st|nd|rd|th)?,?\s+{year}\b", re.IGNORECASE
    )
    return bool(written.search(quote))


def check_value(key: str, value: dict[str, Any], quote: str) -> Grounding:
    """Whether every claim in `value` is readable in `quote`, affirmed rather than denied.

    Checked field by field, because a value is a conjunction: "30 days from receipt" is
    wrong if the quote says 30 days from acceptance, and wrong if it says 45 from receipt.
    An unrecognised field is compared as a plain string or number rather than skipped --
    skipping is how a field nobody thought about becomes the one that is never checked.
    """
    if not value:
        return Grounding(False, f"{key}: the extractor proposed an empty value")

    for field, raw in sorted(value.items()):
        if field in _CURRENCY_FIELDS:
            if not _has_currency(quote):
                return Grounding(False, f"{key}.{field}: the quote names no currency")
            continue

        if field in _ANCHOR_FIELDS:
            # The anchor is what the period runs from, and a term without one is not an
            # obligation anyone can miss. It has to be in the quote, not merely in the
            # document: see `check_qualifiers`.
            if not re.search(rf"\b{re.escape(str(raw))}\b", quote, re.IGNORECASE):
                return Grounding(
                    False, f"{key}.{field}: the quote does not say the term runs from {raw!r}"
                )
            continue

        if isinstance(raw, bool):
            # Checked before the numeric branch: in Python a bool is an int, and reading
            # True as the number 1 would send it looking for a "1" in the quote.
            if raw is not _affirms(quote, len(quote)):
                return Grounding(
                    False,
                    f"{key}.{field}: the quote {'denies' if raw else 'affirms'} what the "
                    f"value records as {raw}",
                )
            continue

        if isinstance(raw, int | float):
            positions = _number_positions(quote, float(raw))
            if not positions:
                return Grounding(False, f"{key}.{field}: the quote does not state {raw}")
            if field in _DAY_FIELDS:
                positions = [p for p in positions if _has_day_unit(quote, p + len(f"{raw:g}"))]
                if not positions:
                    return Grounding(
                        False, f"{key}.{field}: {raw} appears in the quote but not as a day count"
                    )
            if not any(_affirms(quote, p) for p in positions):
                return Grounding(False, f"{key}.{field}: the quote denies the {raw} it states")
            continue

        text = str(raw)
        if field in _DATE_FIELDS or _ISO_DATE.fullmatch(text):
            if not _date_in(quote, text):
                return Grounding(False, f"{key}.{field}: the quote does not state the date {text}")
            continue

        # Money and everything else. A money string carries its own digits, so it is
        # matched on those rather than on the exact rendering ("$250,000" vs "USD 250,000").
        digits = re.sub(r"[^\d.]", "", text)
        if digits and digits.strip("."):
            amount = float(digits)
            positions = _number_positions(quote, amount)
            if not positions:
                return Grounding(False, f"{key}.{field}: the quote does not state {text}")
            if not any(_affirms(quote, p) for p in positions):
                return Grounding(False, f"{key}.{field}: the quote denies the {text} it states")
            continue
        if text and text.lower() not in quote.lower():
            return Grounding(False, f"{key}.{field}: the quote does not say {text!r}")

    return Grounding(True, "every field of the value is stated and affirmed by the quote")


def check_qualifiers(block_text: str, start: int, end: int) -> Grounding:
    """Whether the quote carries the qualifiers and exceptions of the sentence it sits in.

    A cap quoted without "except in cases of gross negligence" is not the cap the parties
    agreed; a payment term quoted without "unless disputed in good faith" is stricter than
    the contract. Both read as clean, well-cited facts, and both are wrong in the
    direction that matters. Only applied to contractual facts: an invoice restating its
    own billing line has no clause around it to lose.
    """
    _, upper = _sentence_bounds(block_text, start, end)
    dropped = block_text[end:upper]
    qualifier = _QUALIFIER.search(dropped)
    if qualifier is not None:
        return Grounding(
            False,
            "the quote stops before a qualifier that changes the term: "
            f"{dropped[qualifier.start():].strip()!r}",
        )
    anchor = _ANCHOR_CUE.search(dropped)
    if anchor is not None:
        return Grounding(
            False,
            "the quote stops before the anchor the period runs from: "
            f"{dropped[anchor.start():].strip()!r}",
        )
    return Grounding(True, "the quote carries the whole operative clause")


def span_for(
    block: Block,
    candidate: FactCandidate,
    *,
    document_sha256: str,
    extractor_version: str,
) -> EvidenceSpan:
    """Mint the evidence span from the stored block, never from what the model said.

    The model proposes a quote and offsets; both are already checked against the block by
    this point. The span is then built from the block itself, so the hash of the quote is
    the hash of the characters that are actually there -- an extractor cannot hand over a
    quote hash of the text it wishes it had cited.
    """
    return EvidenceSpan(
        document_sha256=document_sha256,
        block_index=block.index,
        page=block.page,
        char_start=candidate.quote_start,
        char_end=candidate.quote_end,
        quote_sha256=sha256_text(block.text[candidate.quote_start : candidate.quote_end]),
        parser_version=f"{block.extraction_method}/{PARSER_VERSION}",
        extractor_version=extractor_version,
    )

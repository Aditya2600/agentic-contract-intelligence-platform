from __future__ import annotations

import re

from doctask.domain import Block, DocType, FactCandidate, Rule
from doctask.llm.base import RuleExcerpt, RuleVerdict
from doctask.services.grounding import extend_to_qualifiers

# Which obligation a rule is about, inferred from the words it uses. The rule text is
# configuration, so a new threshold or a new limit changes behaviour without a code edit.
_SUBJECT_WORDS = ("payment", "liability", "notice", "invoice")

_AT_LEAST = re.compile(r"at least\s+(?:USD\s*)?\$?(?P<n>[\d,]+(?:\.\d+)?)", re.IGNORECASE)
_AT_MOST = re.compile(
    r"(?:must not exceed|may not exceed|not exceed|no more than|at most)\s+"
    r"(?:USD\s*)?\$?(?P<n>[\d,]+(?:\.\d+)?)",
    re.IGNORECASE,
)
# Must start with a digit: a bare comma in rendered JSON is not a number.
_NUMBER = re.compile(r"\$?(?P<n>\d[\d,]*(?:\.\d+)?)")
# Split on a full stop only where a sentence really ends: after it, whitespace and then
# something that starts a new sentence. Splitting on every period cut "two hundred fifty
# thousand U.S. dollars (USD 250,000)" in half and lost the cap it was evidence for.
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+(?=[\"“(A-Z0-9])")
# How much of a document counts as its title block for classification.
_TITLE_LINES = 12

# Words too common to say anything about which clause a rule is aimed at.
_STOPWORDS = frozenset(
    {
        "must", "the", "and", "for", "any", "all", "that", "this", "than", "with",
        "without", "their", "its", "from", "into", "upon", "shall", "will", "may",
        "are", "was", "were", "been", "being", "have", "has", "had", "not", "nor",
        "but", "each", "other", "which", "when", "where", "whose", "separate",
    }
)
# Rule vocabulary a sentence has to share before it counts as being about that rule at
# all, when it does not name the subject outright. Two words is not enough: an invoice
# payment line shares "calendar" and "days" with a termination-notice rule and nothing
# else, and judging that rule against it reports a breach the invoice never committed.
_MIN_RULE_OVERLAP = 3
_WORD = re.compile(r"[a-z]{4,}")


def _content_words(text: str) -> set[str]:
    return {word for word in _WORD.findall(text.lower()) if word not in _STOPWORDS}


# A quantity with its unit attached. "(45) calendar days", "forty-five (45) days",
# "USD 250,000", "$18,500.00" -- and deliberately not "Section 4.3" or "(3) years".
_DAYS = re.compile(r"(?P<n>\d[\d,]*)\s*\)?\s*(?:[a-z]+\s+){0,2}days?\b", re.IGNORECASE)
_MONEY = re.compile(r"(?:USD|US\$|\$)\s*(?P<n>\d[\d,]*(?:\.\d+)?)", re.IGNORECASE)
# The register renders a derived value as JSON, where the unit is the field name.
_JSON_DAYS = re.compile(r"[\"']days[\"']\s*:\s*(?P<n>\d[\d,]*(?:\.\d+)?)", re.IGNORECASE)


def _unit(rule_text: str, threshold: re.Match[str] | None) -> str:
    """What the rule's threshold is counted in, read from the words right after it."""
    if threshold is None:
        return "number"
    trailing = rule_text[threshold.end() : threshold.end() + 30].lower()
    if "day" in trailing or "day" in rule_text.lower():
        return "days"
    if "usd" in rule_text.lower() or "$" in rule_text:
        return "money"
    return "number"


def _measure(text: str, unit: str) -> float | None:
    """The number in `text` that carries `unit`, or None if it states no such quantity."""
    patterns = {
        "days": (_JSON_DAYS, _DAYS),
        "money": (_MONEY,),
        "number": (_NUMBER,),
    }[unit]
    for pattern in patterns:
        match = pattern.search(text)
        if match is not None:
            return _number(match.group("n"))
    return None


def _number(raw: str) -> float:
    return float(raw.replace(",", ""))


def _sentences(text: str) -> list[str]:
    # Line wraps inside a block are joined first. A PDF breaks a clause wherever the
    # column ends, and treating that break as a sentence end separated "aggregate
    # liability ... will not exceed" from the "(USD 250,000)" it was capping.
    unwrapped = re.sub(r"\s*\n\s*", " ", text)
    return [sentence.strip() for sentence in _SENTENCE_BREAK.split(unwrapped) if sentence.strip()]


def _approx_tokens(text: str) -> int:
    """A real, deterministic token count for text nobody billed for.

    Not a tokenizer -- an offline stand-in has no vocabulary to tokenize against -- but
    it is a genuine function of what was actually read or written, at the ~4-characters-
    per-token rate real BPE tokenizers land near for English prose. That is the
    difference the cost report needs: a number that moves when the text does, not a
    constant that would make every event look identical regardless of what happened.
    """
    return max(1, len(text) // 4)


class FakeLLM:
    """Deterministic offline model used by tests and the local demo."""

    extractor_version = "fake-v1"
    # The price table's key for "no real model was called". Declared there at
    # $0/million tokens -- an explicit zero, not the silent one an unrecognised model
    # would get.
    model = "fake"

    def __init__(self, ocr_pages: dict[int, str] | None = None) -> None:
        # What the stand-in "vision model" sees on each rendered page, by page number.
        # Empty by default: offline, an unreadable page stays unreadable, which is the
        # behaviour the failure tests need.
        self.ocr_pages = ocr_pages or {}
        self.last_usage: dict[str, int] = {"tokens_in": 0, "tokens_out": 0}

    async def read_page(self, image_png: bytes, *, page: int) -> str:
        del image_png
        text = self.ocr_pages.get(page, "")
        self.last_usage = {"tokens_in": _approx_tokens(str(page)), "tokens_out": _approx_tokens(text)}
        return text

    async def classify(self, text: str) -> tuple[DocType, float, str]:
        """Type a document from what it calls itself, not from what it mentions.

        Only the opening of the document is read. A real contract names every other
        contract in the file -- an MSA anticipates amendments, an operational notice
        says it amends nothing, an invoice cites the agreement -- so matching those
        words anywhere in the body types everything as an amendment. The title block is
        the one place a document has to say what it is.
        """
        markers: list[tuple[DocType, tuple[str, ...], float, str]] = [
            ("amendment", ("amendment no", "amendment to", "amendment number"), 0.95,
             "title identifies an amendment to an existing agreement"),
            ("master_agreement", ("master services agreement", "master agreement"), 0.96,
             "title identifies a master services agreement"),
            ("invoice", ("invoice",), 0.94, "title identifies an invoice"),
            ("sow", ("statement of work",), 0.93, "title identifies a statement of work"),
            ("purchase_order", ("purchase order",), 0.93, "title identifies a purchase order"),
            ("policy", ("data processing addendum", "addendum", "policy"), 0.91,
             "title identifies an addendum or policy document"),
        ]
        # Earliest line wins, then marker order. A document's own title comes before the
        # documents it refers to, and the one line that carries both -- "AMENDMENT NO. 1
        # TO MASTER SERVICES AGREEMENT" -- is settled by the order above.
        result: tuple[DocType, float, str] = (
            "unknown",
            0.45,
            "no type marker in the document title block",
        )
        for line in text.splitlines()[:_TITLE_LINES]:
            lowered = line.lower()
            for doc_type, words, confidence, reason in markers:
                if any(word in lowered for word in words):
                    result = (doc_type, confidence, reason)
                    break
            else:
                continue
            break
        self.last_usage = {
            "tokens_in": _approx_tokens(text),
            "tokens_out": _approx_tokens(result[2]),
        }
        return result

    async def extract(self, block: Block, *, wider_context: bool = False) -> list[FactCandidate]:
        del wider_context
        candidates: list[FactCandidate] = []

        patterns = [
            (
                "payment_due_days",
                re.compile(r"payment (?:is )?due within (?P<days>\d+) calendar days", re.IGNORECASE),
                lambda m: {"days": int(m.group("days")), "anchor": "receipt"},
            ),
            (
                "liability_cap",
                re.compile(r"liability (?:is|shall be) capped at (?P<amount>\$[\d,]+)", re.IGNORECASE),
                lambda m: {"amount": m.group("amount"), "currency": "USD"},
            ),
            (
                "notice_days",
                re.compile(r"(?P<days>\d+) days(?:'|’) written notice", re.IGNORECASE),
                lambda m: {"days": int(m.group("days"))},
            ),
            (
                "invoice_amount_due",
                re.compile(r"amount due\s*:\s*(?P<amount>\$[\d,]+(?:\.\d{2})?)", re.IGNORECASE),
                lambda m: {"amount": m.group("amount"), "currency": "USD"},
            ),
            # Contract drafting spells the number and then repeats it in digits:
            # "due thirty (30) calendar days", "on sixty (60) days' prior written
            # notice", "will not exceed ... (USD 250,000)". The digits in brackets are
            # the operative figure, so that is what is read.
            (
                "payment_due_days",
                re.compile(
                    r"due (?:within\s+)?(?:[a-z-]+\s+)?\((?P<days>\d+)\)\s+calendar days",
                    re.IGNORECASE,
                ),
                lambda m: {"days": int(m.group("days")), "anchor": "receipt"},
            ),
            (
                "notice_days",
                re.compile(
                    r"\((?P<days>\d+)\)\s+days(?:'|’)\s+(?:prior\s+)?written notice",
                    re.IGNORECASE,
                ),
                lambda m: {"days": int(m.group("days"))},
            ),
            (
                "liability_cap",
                re.compile(
                    r"not exceed[^)]{0,160}\(USD\s*(?P<amount>[\d,]+(?:\.\d{2})?)\)", re.IGNORECASE
                ),
                lambda m: {"amount": f"${m.group('amount')}", "currency": "USD"},
            ),
            (
                "invoice_amount_due",
                re.compile(
                    r"total due\s*:\s*USD\s*(?P<amount>[\d,]+(?:\.\d{2})?)", re.IGNORECASE
                ),
                lambda m: {"amount": f"${m.group('amount')}", "currency": "USD"},
            ),
        ]

        for key, pattern, value_builder in patterns:
            for match in pattern.finditer(block.text):
                value = value_builder(match)
                # The shortest quote carrying the number is not always the shortest quote
                # carrying the obligation: "due thirty (30) calendar days" leaves out what
                # the thirty days run from, and "capped at $250,000" leaves out the
                # exception that guts the cap. Widen to the end of the clause when the
                # rest of it changes the meaning; `check_qualifiers` refuses the ones an
                # extractor fails to widen.
                start, end = extend_to_qualifiers(block.text, match.start(), match.end())
                candidates.append(
                    FactCandidate(
                        key=key,
                        value=value,
                        block_id=block.id,
                        quote=block.text[start:end],
                        quote_start=start,
                        quote_end=end,
                    )
                )
        self.last_usage = {
            "tokens_in": _approx_tokens(block.text),
            "tokens_out": sum(_approx_tokens(c.quote) for c in candidates) or 1,
        }
        return candidates

    async def evaluate_rule(
        self,
        rule: Rule,
        *,
        target_label: str,
        excerpts: list[RuleExcerpt],
        statement: str | None = None,
    ) -> RuleVerdict:
        verdict = await self._evaluate_rule(
            rule, target_label=target_label, excerpts=excerpts, statement=statement
        )
        prompt_text = rule.text + (statement or "") + "".join(e.text for e in excerpts)
        self.last_usage = {
            "tokens_in": _approx_tokens(prompt_text),
            "tokens_out": _approx_tokens(verdict.rationale),
        }
        return verdict

    async def _evaluate_rule(
        self,
        rule: Rule,
        *,
        target_label: str,
        excerpts: list[RuleExcerpt],
        statement: str | None = None,
    ) -> RuleVerdict:
        """Compare one numeric threshold in the rule against the target.

        Offline stand-in for a reasoning model, but the honesty rules are the real ones:
        no subject match, no threshold, or no number means `insufficient_evidence`, never
        an invented pass. Every verdict names the excerpt it read, so the deterministic
        grounding check gets the same claim to verify that a real model would give it.
        """
        lowered = rule.text.lower()
        subject = next((word for word in _SUBJECT_WORDS if word in lowered), None)
        if subject is None:
            return RuleVerdict(
                "insufficient_evidence",
                f"{rule.code}: no obligation subject recognised in the rule text",
            )

        at_least = _AT_LEAST.search(rule.text)
        at_most = _AT_MOST.search(rule.text)
        if at_least is None and at_most is None:
            return RuleVerdict(
                "insufficient_evidence",
                f"{rule.code}: rule states no numeric threshold this model can check",
            )

        # What is being judged: the derived value when the caller supplies one, otherwise
        # the excerpts themselves. Either way the citation comes from an excerpt, because
        # a derived value is not evidence of anything.
        #
        # Sentences are ranked by how much of the rule's own vocabulary they use, not
        # taken in document order. A real contract mentions its subjects repeatedly --
        # the MSA says "written notice" in a payment-dispute clause pages before the
        # termination clause the notice rule is actually about -- and first-match would
        # judge the wrong sentence and call it a violation.
        wanted = _content_words(rule.text)
        scored = [
            (len(wanted & _content_words(sentence)), subject in sentence.lower(), excerpt, sentence)
            for excerpt in excerpts
            for sentence in _sentences(excerpt.text)
        ]
        # A sentence that names the subject outright beats one that merely shares
        # vocabulary with the rule, however much of it: the invoice's payment clause
        # overlaps a termination-notice rule on "calendar days" alone, and judging the
        # notice rule against it would report a termination breach the invoice never
        # committed. Vocabulary overlap is the fallback, not the first choice.
        # Subject-bearing sentences are tried first and vocabulary matches after, in one
        # list: an invoice's payment clause overlaps a termination-notice rule on
        # "calendar days" alone, and judging the notice rule against it would report a
        # termination breach the invoice never committed. But a clause often states the
        # term without repeating the subject word ("Undisputed invoice amounts are due
        # thirty (30) calendar days"), so the overlap matches stay as the fallback.
        ranked = sorted(scored, key=lambda row: (-int(row[1]), -row[0]))
        relevant = [
            (excerpt, sentence)
            for score, has_subject, excerpt, sentence in ranked
            if has_subject or score >= _MIN_RULE_OVERLAP
        ]
        if not relevant:
            return RuleVerdict(
                "insufficient_evidence",
                f"{rule.code}: {target_label} says nothing about {subject}",
            )

        # Compare like with like. A clause is full of numbers that are not the term --
        # section numbers, dates, a "(3) years" survival period -- so the number read
        # out of a sentence has to carry the same unit the rule's own threshold does.
        # First-number-wins read "Section 4.3" as a three-day payment term.
        unit = _unit(rule.text, at_least or at_most)
        judged = _measure(statement, unit) if statement else None
        for excerpt, sentence in relevant:
            found = judged if judged is not None else _measure(sentence, unit)
            if found is None:
                continue
            actual = found
            if at_least is not None:
                threshold = _number(at_least.group("n"))
                violated = actual < threshold
                comparison = f"{actual:g} is below the required minimum {threshold:g}"
            else:
                assert at_most is not None
                threshold = _number(at_most.group("n"))
                violated = actual > threshold
                comparison = f"{actual:g} exceeds the permitted maximum {threshold:g}"
            verdict = "violation" if violated else "pass"
            rationale = (
                comparison
                if violated
                else f"{actual:g} satisfies the {threshold:g} threshold"
            )
            return RuleVerdict(verdict, f"{rule.code}: {rationale}", sentence, excerpt.index)

        return RuleVerdict(
            "insufficient_evidence",
            f"{rule.code}: {target_label} mentions {subject} but states no comparable number",
        )

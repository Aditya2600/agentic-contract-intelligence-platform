"""What a fact is a fact about, read deterministically from the document around it.

The extractor answers "what number does this clause state". This module answers the
question that decides whether two such numbers are in conflict at all: whose agreement,
whose promise, under what condition, from when. Getting it wrong in one direction invents
conflicts between documents that were never talking about the same thing; getting it wrong
in the other hides a real contradiction. So it is read from the text, never inferred from
ingest order, and never asked of a model.
"""

from __future__ import annotations

import re

from doctask.domain import FactScope

# "entered into between Acme Buyer LLC and Northstar Vendor Inc."
_PARTIES = re.compile(
    r"\bbetween\s+(?P<a>[A-Z][^,.;]{2,60}?)\s+and\s+(?P<b>[A-Z][^,.;]{2,60}?)"
    r"\s*(?=[,.;(]|$)",
    re.MULTILINE,
)
_EFFECTIVE = re.compile(
    r"\b(?:effective(?:\s+(?:as\s+of|from|date))?|dated)\s*[:.]?\s*"
    r"(?P<date>\d{4}-\d{2}-\d{2}|[A-Z][a-z]+\s+\d{1,2},?\s+\d{4})",
    re.IGNORECASE,
)
_EXPIRES = re.compile(
    r"\b(?:until|through|expires?(?:\s+on)?|terminates?\s+on)\s*[:.]?\s*"
    r"(?P<date>\d{4}-\d{2}-\d{2}|[A-Z][a-z]+\s+\d{1,2},?\s+\d{4})",
    re.IGNORECASE,
)
_CLAUSE = re.compile(r"\b(?:section|clause|article)\s+(?P<n>\d+(?:\.\d+)*)", re.IGNORECASE)
# A leading clause number on its own line: '"4.3 Undisputed invoice amounts are due...'
_LEADING_CLAUSE = re.compile(r'^["\'\s]*(?P<n>\d+\.\d+(?:\.\d+)*)\s+[A-Z]')
# Qualifiers that make a term hold only sometimes. Kept to a short, unambiguous list:
# a false condition splits one obligation into two that never compare, which hides a
# real conflict, so this errs towards finding none.
_CONDITION = re.compile(
    r"\b(?P<c>(?:if|unless|provided that|except where|only when)\s+[^,.;]{3,60})",
    re.IGNORECASE,
)

_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+(?=[\"“(A-Z0-9])")

# Document types that execute an agreement rather than state its terms.
_OPERATIONAL_TYPES = {"invoice", "purchase_order"}


def obligation_scope_for(doc_type: str, *, amends: bool) -> str:
    """Whether this document's terms are contract terms or operational ones.

    An invoice stating NET 10 under an agreement that says NET 30 is not proposing a
    contract change; it is billing on terms that do not match the contract. Filing it as a
    competing contract term produced a conflict on every invoice a vendor ever sent.

    `amends` is the documented way out: an operational document that carries explicit
    amendment or supersession language is claiming to change the agreement, and a claim
    like that is judged as a contract term -- by a human, at the conflict gate.
    """
    if doc_type in _OPERATIONAL_TYPES and not amends:
        return "operational"
    return "contractual"


def _sentence_around(text: str, quote: str) -> str:
    index = text.find(quote)
    if index == -1:
        return text
    unwrapped = re.sub(r"\s*\n\s*", " ", text)
    index = unwrapped.find(quote)
    if index == -1:
        return unwrapped
    starts = [0] + [match.end() for match in _SENTENCE_BREAK.finditer(unwrapped)]
    ends = [match.start() for match in _SENTENCE_BREAK.finditer(unwrapped)] + [len(unwrapped)]
    for start, end in zip(starts, ends, strict=True):
        if start <= index < end:
            return unwrapped[start:end].strip()
    return unwrapped


def parties_in(text: str) -> tuple[str, ...]:
    """The named parties, sorted so the same two in either order compare equal."""
    match = _PARTIES.search(text)
    if match is None:
        return ()
    return tuple(sorted(name.strip().rstrip(".") for name in (match["a"], match["b"])))


def scope_for(
    *,
    doc_type: str,
    document_text: str,
    block_text: str,
    quote: str,
    agreement_id: str | None,
    amends: bool,
) -> FactScope:
    """Build the scope of one fact from its own sentence and its document.

    Clause and conditions come from the sentence carrying the quote, because they qualify
    that term and no other. Parties and dates come from the document, because they qualify
    everything in it.
    """
    sentence = _sentence_around(block_text, quote)
    clause_match = _CLAUSE.search(sentence) or _LEADING_CLAUSE.match(sentence)
    effective = _EFFECTIVE.search(document_text)
    expires = _EXPIRES.search(document_text)
    return FactScope(
        agreement_id=agreement_id,
        clause=clause_match.group("n") if clause_match else None,
        parties=parties_in(document_text),
        effective_from=effective.group("date") if effective else None,
        effective_to=expires.group("date") if expires else None,
        conditions=tuple(
            sorted({match.group("c").strip().lower() for match in _CONDITION.finditer(sentence)})
        ),
        obligation_scope=obligation_scope_for(doc_type, amends=amends),
    )

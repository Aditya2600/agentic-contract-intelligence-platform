from __future__ import annotations

import re

# Explicit supersession language only. "This is the latest document" or a newer
# ingest timestamp is not supersession; a human decides precedence, this module
# only reports that the source says so and returns the sentence as evidence.
SUPERSESSION_PATTERNS = [
    # Past tense included on purpose: real amendments are written "is deleted in its
    # entirety and replaced with the following", not "replaces". Matching only the
    # present tense read that as an ordinary contradiction and kept the old term.
    re.compile(r"\breplac(?:e|es|ed|ing)\b", re.IGNORECASE),
    re.compile(r"\bsupersed(?:e|es|ed|ing)\b", re.IGNORECASE),
    re.compile(r"\bamend(?:s|ed|ing)?\b", re.IGNORECASE),
    re.compile(r"\bdeleted in its entirety\b", re.IGNORECASE),
    re.compile(r"\bis revised to\b|\brevised to\b", re.IGNORECASE),
    re.compile(r"\bin lieu of\b", re.IGNORECASE),
]

_SENTENCE = re.compile(r"[^.\n]*[.\n]?")
# How far back to look for the sentence that introduced a quoted replacement clause.
_LEAD_IN_CHARS = 300


def _claims_supersession(sentence: str) -> bool:
    return any(pattern.search(sentence) for pattern in SUPERSESSION_PATTERNS)


def supersession_evidence(text: str) -> str | None:
    """Return the first sentence containing explicit supersession language, else None."""
    for sentence in _SENTENCE.findall(text):
        stripped = sentence.strip()
        if stripped and _claims_supersession(stripped):
            return stripped
    return None


def supersession_evidence_near(text: str, quote: str) -> str | None:
    """Supersession language in the sentence that carries `quote`.

    A document-level sentence is the right evidence for the term it names and the
    wrong evidence for every other term, so a key-specific sentence wins when the
    source has one.
    """
    index = text.find(quote)
    if index == -1:
        return None
    start = max(text.rfind(".", 0, index), text.rfind("\n", 0, index)) + 1
    ends = [end for end in (text.find(".", index), text.find("\n", index)) if end != -1]
    sentence = text[start : min(ends) + 1 if ends else len(text)].strip()
    if _claims_supersession(sentence):
        return sentence

    # The replacement clause is usually the sentence *after* the one that claims the
    # replacement: "Section 4.3 is deleted in its entirety and replaced with the
    # following: '4.3 ... forty-five (45) calendar days ...'". The new term is the
    # quote; the claim is the line above it, so a key-specific lookup has to see both.
    # Walk back by line, not by sentence: a clause numbered "4.3" puts a full stop in
    # the middle of its own first word, so sentence-start lands inside the quotation.
    line_start = text.rfind("\n", 0, index) + 1
    preceding = [
        line.strip()
        for line in text[max(line_start - _LEAD_IN_CHARS, 0) : line_start].splitlines()
        if line.strip()
    ]
    if preceding and _claims_supersession(preceding[-1]):
        return f"{preceding[-1]} {sentence}".strip()
    return None

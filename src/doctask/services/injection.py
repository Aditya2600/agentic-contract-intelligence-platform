"""Reading a document is not obeying it, and this module is how that stays true.

Two things are deliberately separate here.

The **security boundary** is architectural and does not live in this file: document text
never acquires the capability to approve anything. A resume payload is refused unless it
carries a reviewer credential stamped by `doctask.auth`; the model is handed block text
and nothing else -- no repository, no connection, no token, no tool -- and application
code alone decides what to do with what comes back. That boundary holds whether or not a
single pattern below ever matches.

The **detector** is telemetry: it says "a human should look at this paragraph first". It
is a list of strings, so it is incomplete by construction, and treating a clean scan as
permission would make the whole system exactly as strong as this file's regexes. Nothing
downstream reads a negative result as a licence -- there is no path where "no pattern
matched" grants anything that "a pattern matched" would have withheld.

Scanning is per block, not per document. A single hostile paragraph in a 60-page contract
must not cost the other 59 pages: quarantining the document would hand an attacker a
denial of service for the price of one sentence.
"""

from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass, field

# Characters with no visible rendering. Their presence in a contract is not itself an
# attack, but it is the standard way one is hidden from the human who approves it: the
# reviewer reads "Payment is due in 30 days" and the model reads that plus an instruction
# spelled through zero-width joiners. Both the obfuscation and what it hides are reported.
_INVISIBLE = re.compile(
    r"[­​-‏‪-‮⁠-⁤⁪-⁯﻿᠎]"
)
# Tags Unicode intends for language tagging, routinely used to smuggle ASCII invisibly.
_TAG_CHARS = re.compile(r"[\U000e0000-\U000e007f]")

# Markup a renderer hides and a model reads: HTML comments, and attributes that carry
# text no reader ever sees.
_HIDDEN_MARKUP = re.compile(
    r"<!--.*?-->|<[^>]*\b(?:alt|title|aria-label|data-[\w-]+)\s*=\s*[\"'][^\"']*[\"'][^>]*>",
    re.IGNORECASE | re.DOTALL,
)
# Markdown's own hiding places: a link title, and an image's alt text.
_MARKDOWN_HIDDEN = re.compile(r"!\[[^\]]*\]\([^)]*\)|\[[^\]]*\]\([^)\s]*\s+\"[^\"]*\"\)")

_IMPERATIVES: list[tuple[str, re.Pattern[str]]] = [
    ("override_instructions", re.compile(
        r"\b(?:ignore|disregard|forget|override)\s+(?:all\s+|any\s+|the\s+)?"
        r"(?:previous|prior|earlier|above|preceding|system)\s+"
        r"(?:instruction|instructions|prompt|prompts|rules?|context)\b",
        re.IGNORECASE)),
    ("role_impersonation", re.compile(
        r"(?:^|\n)\s*(?:system|assistant|agent|developer)\s*:", re.IGNORECASE)),
    ("approval_demand", re.compile(
        r"\b(?:approve|accept|sign\s+off\s+on|auto[- ]?approve|commit)\s+"
        r"(?:this|the|all|every)\b",
        re.IGNORECASE)),
    ("concealment", re.compile(
        r"\b(?:do\s+not|don't|never)\s+(?:tell|show|report|mention|flag|escalate)\b",
        re.IGNORECASE)),
    ("secret_exfiltration", re.compile(
        r"\b(?:reveal|print|output|repeat|send|leak)\s+(?:the\s+|your\s+)?"
        r"(?:system\s+prompt|api[\s_-]?key|secret|credential|token|password)\b",
        re.IGNORECASE)),
    ("tool_invocation", re.compile(
        r"\b(?:call|invoke|execute|run)\s+(?:the\s+)?"
        r"(?:tool|function|command|sql|query|shell)\b",
        re.IGNORECASE)),
    ("instruction_to_agent", re.compile(
        r"\b(?:you\s+(?:must|should|shall|will)|your\s+task\s+is|new\s+instructions?)\b",
        re.IGNORECASE)),
]


@dataclass(frozen=True, slots=True)
class Scan:
    """What a block looked like on inspection. `signals` is evidence, not a verdict."""

    suspicious: bool
    signals: tuple[str, ...] = ()
    # The text the detector actually matched against, after unhiding. Kept so a reviewer
    # can see what the model would have read rather than what the page renders.
    normalised: str = ""
    quotes: tuple[str, ...] = field(default=())

    def describe(self) -> str:
        return ", ".join(self.signals) if self.signals else "no known pattern"


def normalise(text: str) -> str:
    """Everything a model reads, made visible.

    An injection only has to survive the path from bytes to tokens; it does not have to
    survive being displayed. So the detector reads the same text the model will: invisible
    characters removed rather than left to break `\\bignore\\b` in the middle, HTML
    entities decoded, compatibility forms folded, hidden markup unwrapped into plain text.
    Matching the rendered string instead is how `i&#8203;gnore previous instructions`
    passes a scan and reaches the model intact.
    """
    without_tags = _TAG_CHARS.sub("", text)
    visible = _INVISIBLE.sub("", without_tags)
    decoded = html.unescape(visible)
    folded = unicodedata.normalize("NFKC", decoded)
    # Hidden markup is not deleted -- deleting it would hide the payload from the
    # detector too. The delimiters go and the text inside stays.
    unwrapped = _HIDDEN_MARKUP.sub(lambda m: re.sub(r"[<>!\[\]()\"']", " ", m.group(0)), folded)
    return _MARKDOWN_HIDDEN.sub(lambda m: re.sub(r"[!\[\]()\"']", " ", m.group(0)), unwrapped)


def scan(text: str, *, extraction_method: str = "txt") -> Scan:
    """Inspect one block. Suspicion is reported; it is never a permission decision.

    `extraction_method` matters because text a vision model transcribed off a rendered
    page carries an instruction no human reading the PDF would have seen -- the pixels say
    one thing to a person and another to an OCR pass. It is not suspicious on its own;
    it is what makes an imperative found in that text worth quarantining rather than
    shrugging at.
    """
    cleaned = normalise(text)
    signals = [name for name, pattern in _IMPERATIVES if pattern.search(cleaned)]
    quotes = [
        pattern.search(cleaned).group(0).strip()
        for name, pattern in _IMPERATIVES
        if name in signals and pattern.search(cleaned)
    ]
    hidden = []
    if _INVISIBLE.search(text) or _TAG_CHARS.search(text):
        hidden.append("invisible_characters")
    if _HIDDEN_MARKUP.search(text) or _MARKDOWN_HIDDEN.search(text):
        hidden.append("hidden_markup")

    # Concealment alone is not an attack: a contract may legitimately contain an HTML
    # comment. Concealment *carrying* an imperative is, and so is an imperative on its
    # own. Text a vision model read is held to the same bar as any other text -- the
    # method is recorded as context for the reviewer, not as an extra trigger.
    if signals and extraction_method != "txt":
        hidden.append(f"read_by_{extraction_method}")
    all_signals = tuple(signals + hidden) if signals else tuple(hidden)
    return Scan(
        suspicious=bool(signals),
        signals=all_signals,
        normalised=cleaned,
        quotes=tuple(quotes),
    )


def has_injection_pattern(text: str) -> bool:
    """Whole-text convenience for callers that only need the boolean."""
    return scan(text).suspicious

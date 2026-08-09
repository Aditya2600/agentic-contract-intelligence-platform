"""How one document says it stands to another, read from what the document says.

Ingest order does not establish that an amendment amends anything, and a filename
establishes less. A relation is a claim a document makes in a sentence, so every relation
found here carries that sentence and the block it came from -- the same standard the fact
extractor is held to, for the same reason: a claim nobody can re-read is not evidence.

Detection is deterministic and regex-based on purpose. Which agreement a term belongs to
decides whether two numbers are in conflict, and that decision has to be reproducible from
the stored text during an audit, not re-asked of a model that may answer differently.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import UUID

from doctask.domain import Block, DocumentRelation
from doctask.services.supersession import SUPERSESSION_PATTERNS

# "Agreement No. MSA-2024-001", "Contract Reference: ACME/2024/17", "MSA No: X-9".
# The label is required: a bare token that looks like an identifier is as likely to be an
# invoice number, a section reference or a part code.
#
# Every label token ends on a word boundary. Without that, `no\.?` matched the "no" inside
# "notices" and "NORTHSTAR", so "Contract notices" read as agreement "TICES" and named
# every register row after it. An identifier this wrong is worse than none: it is a real
# agreement scope as far as the rest of the system is concerned.
_AGREEMENT_REF = re.compile(
    r"\b(?:master\s+services\s+agreement|agreement|contract|msa|sow)\s*"
    r"(?:no\b\.?|nos\b\.?|number\b|#|ref\b\.?|reference\b)\s*[:.]?\s*"
    r"(?P<ref>[A-Za-z0-9][A-Za-z0-9./_-]{2,})",
    re.IGNORECASE,
)

# Where a sentence really ends: a full stop, then whitespace, then something that starts
# a new sentence. Breaking on every period cut the evidence to pieces exactly where these
# documents put their periods -- "Amendment No. 1", "Agreement No. MSA-2024-001",
# "Section 4.3" -- and a relation quoted as "This Amendment amends Agreement No." names
# nothing. A digit does not start a sentence here, which is what keeps "No. 1" whole.
#
# The abbreviations are the ones these documents actually end a "sentence" with, and
# "Agreement No. MSA-2024-001" is the case that matters: the identifier lands after the
# period, so a naive break drops it out of the quote that is supposed to name it.
_ABBREVIATION = (
    r"(?<!(?i:\bno\.))(?<!(?i:\bnos\.))(?<!(?i:\binc\.))"
    r"(?<!(?i:\bltd\.))(?<!(?i:\bco\.))(?<!(?i:\bsec\.))"
)
_SENTENCE_BREAK = re.compile(_ABBREVIATION + r"(?<=[.!?])\s+(?=[\"“(A-Z])|\n+")

# The word "amendment" is not a claim to amend anything. A reference header
# ("MSA-2026-014 / Amendment No. 1") only cites one, and an invoice saying it "is not
# signed as an amendment to the MSA" is denying the relation in as many words. Both used
# to register as `amends`, which made the invoice's billing terms contractual and put
# NET 10 in front of a human as a rival to the negotiated term. So an amending claim needs
# an amending verb, or "amendment to/of", or the noun beside supersession language -- and
# a denial anywhere near the word cancels it.
_AMENDS_CLAIM = re.compile(
    r"\bamend(?:s|ed|ing)\b|\bamendments?\s+(?:to|of)\b", re.IGNORECASE
)
_AMENDMENT_NOUN = re.compile(r"\bamendments?\b", re.IGNORECASE)
_DENIED = re.compile(r"\b(?:not|nor|neither|never)\b[^.]{0,40}?\bamend", re.IGNORECASE)

_RELATION_PATTERNS: dict[str, re.Pattern[str]] = {
    # An amendment changes the terms of a named agreement.
    "amends": _AMENDS_CLAIM,
    # An operational instrument issued under an agreement it does not change.
    "governed_by": re.compile(
        r"\b(?:issued under|pursuant to|governed by|subject to the terms of|"
        r"under the terms of)\b",
        re.IGNORECASE,
    ),
    # A bare mention: enough to scope a term to an agreement, not to change one.
    "references": re.compile(
        r"\b(?:with reference to|in reference to|re:|relating to|"
        r"under (?:the )?(?:master services )?agreement)\b",
        re.IGNORECASE,
    ),
}


@dataclass(frozen=True, slots=True)
class DetectedRelation:
    kind: str
    target_ref: str
    quote: str
    block_id: UUID | None
    quote_start: int
    quote_end: int


def agreement_ref(text: str) -> str | None:
    """The agreement identifier this text declares or names, normalised to upper case."""
    match = _AGREEMENT_REF.search(text)
    return match.group("ref").upper().rstrip(".,;:") if match else None


def _sentences_with_offsets(text: str) -> list[tuple[str, int, int]]:
    """Sentences with offsets into `text`, so evidence can be cited back into a block."""
    found: list[tuple[str, int, int]] = []
    cursor = 0
    bounds = [match.start() for match in _SENTENCE_BREAK.finditer(text)] + [len(text)]
    for end in bounds:
        raw = text[cursor:end]
        stripped = raw.strip()
        if stripped:
            start = cursor + raw.index(stripped[0])
            found.append((stripped, start, start + len(stripped)))
        match = _SENTENCE_BREAK.search(text, end)
        cursor = match.end() if match and match.start() == end else end
    return found


def detect_relations(blocks: list[Block]) -> list[DetectedRelation]:
    """Every relation any sentence in the document claims, in document order.

    A sentence may claim more than one -- "This amendment replaces the payment provision
    in Master Services Agreement No. MSA-2024-001" both amends and supersedes -- and both
    are recorded. Collapsing them to one would decide, here, a question about legal effect
    that only a human is allowed to decide.

    The identifier is read from the claiming sentence first and from the document as a
    whole second, so a document that names its parent agreement once in a header still
    scopes a relation stated three paragraphs later.
    """
    document_ref = agreement_ref("\n".join(block.text for block in blocks))
    relations: list[DetectedRelation] = []
    for block in blocks:
        for sentence, start, end in _sentences_with_offsets(block.text):
            kinds = [kind for kind, p in _RELATION_PATTERNS.items() if p.search(sentence)]
            if any(pattern.search(sentence) for pattern in SUPERSESSION_PATTERNS):
                kinds.append("supersedes")
                # "This Amendment replaces the payment provision": the noun names what
                # the document is and the verb says what it does to the agreement.
                if "amends" not in kinds and _AMENDMENT_NOUN.search(sentence):
                    kinds.append("amends")
            if "amends" in kinds and _DENIED.search(sentence):
                kinds.remove("amends")
            if not kinds:
                continue
            target = agreement_ref(sentence) or document_ref or ""
            for kind in kinds:
                relations.append(
                    DetectedRelation(
                        kind=kind,
                        target_ref=target,
                        quote=sentence,
                        block_id=block.id,
                        quote_start=start,
                        quote_end=end,
                    )
                )
    return relations


def self_declared_ref(blocks: list[Block], *, doc_type: str) -> str | None:
    """The agreement a document *is*, as opposed to one it points at.

    Only an agreement-shaped document can declare its own identity. An invoice quoting
    "Agreement No. MSA-2024-001" is naming someone else's agreement, and treating that as
    the invoice's own identity would file the invoice's billing terms as contract terms.
    """
    if doc_type not in {"master_agreement", "sow"}:
        return None
    return agreement_ref("\n".join(block.text for block in blocks))


def to_relations(
    detected: list[DetectedRelation],
    *,
    document_id: UUID,
    resolved: dict[str, UUID],
) -> list[DocumentRelation]:
    """Persistable relations, with the target resolved where the collection holds it.

    An unresolved target is kept rather than dropped: an amendment to an agreement nobody
    uploaded is the case a human most needs to see, not the one to discard quietly.
    """
    return [
        DocumentRelation(
            document_id=document_id,
            kind=relation.kind,
            target_ref=relation.target_ref,
            evidence_quote=relation.quote,
            block_id=relation.block_id,
            quote_start=relation.quote_start,
            quote_end=relation.quote_end,
            target_document_id=resolved.get(relation.target_ref),
        )
        for relation in detected
    ]

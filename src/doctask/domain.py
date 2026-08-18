from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from doctask.services.hashing import canonical_json, sha256_text

DocType = Literal[
    "master_agreement",
    "amendment",
    "sow",
    "invoice",
    "purchase_order",
    "policy",
    "unknown",
]
ReviewState = Literal["pending", "approved", "rejected"]

# The vendor-obligation ontology: every register key the system knows how to hold, and
# what each one means. Adding a key here is the only change needed to track a new
# obligation -- no node logic knows these names. A playbook may only aim a rule at a key
# on this list, because a rule pointed at a key that never exists is never evaluated and
# reports nothing, which reads exactly like a clean result.
OBLIGATION_KEYS: dict[str, str] = {
    "payment_due_days": "number of days after the anchor event by which payment is due",
    "liability_cap": "monetary cap on liability",
    "notice_days": "days of written notice required to terminate",
    "invoice_amount_due": "total amount due on an invoice",
    "term_end_date": "date the agreement or SOW ends",
    "auto_renewal": "whether the agreement renews automatically",
}


@dataclass(slots=True)
class DocumentInput:
    filename: str
    mime_type: str
    text: str


@dataclass(slots=True)
class Document:
    collection_id: UUID
    filename: str
    mime_type: str
    text: str
    sha256: str
    id: UUID = field(default_factory=uuid4)
    doc_type: DocType = "unknown"
    doc_type_confidence: float | None = None
    quarantined: bool = False
    # The agreement this document declares itself to be, when it names one. Only
    # agreement-shaped documents get one; see `services.relations.self_declared_ref`.
    agreement_ref: str | None = None


@dataclass(slots=True)
class Block:
    document_id: UUID
    index: int
    text: str
    text_sha256: str
    char_start: int
    char_end: int
    id: UUID = field(default_factory=uuid4)
    injection_flag: bool = False
    # Where this text came from, kept so a citation can be audited rather than trusted:
    # `page` locates it in the original file, `extraction_method` says whether a human
    # would see the same characters (native_pdf/docx/txt) or whether a vision model read
    # them off a rendered image (gemma_vlm).
    page: int | None = None
    extraction_method: str = "txt"


# How one document says it stands to another. Every relation carries the sentence that
# claims it, because "this amendment amends that MSA" is a legal claim made by a
# document, not something the system may infer from ingest order or filename.
RELATION_KINDS = ("amends", "governed_by", "supersedes", "references")

# What kind of promise a fact records. A master agreement states the obligation; an
# invoice or purchase order executes it. An invoice saying NET 10 where the contract says
# NET 30 is an operational term that disagrees with the contract -- which is a billing
# problem, not two contradictory contract terms. Comparing across this line manufactures
# a conflict out of two statements that were never about the same thing.
OBLIGATION_SCOPES = ("contractual", "operational")


@dataclass(frozen=True, slots=True)
class FactScope:
    """What a fact is a fact *about*: which agreement, which clause, whose promise.

    Two numbers under the same register key are only in conflict when they answer the
    same question. Before this existed, `payment_due_days` was one global question and
    every document that mentioned payment was arguing about it: two unrelated MSAs in one
    collection, an invoice restating its own billing terms, a targeted amendment to a
    clause nobody else touched.
    """

    # The agreement this term belongs to, as the document identifies it. None means the
    # document declared none, which is the pre-existing single-agreement behaviour.
    agreement_id: str | None = None
    clause: str | None = None
    # Sorted, so two documents naming the same parties in either order compare equal.
    parties: tuple[str, ...] = ()
    effective_from: str | None = None
    effective_to: str | None = None
    # Qualifiers the term is only true under: "unless disputed in good faith", "if paid
    # by ACH". A conditional term and an unconditional one are different promises.
    conditions: tuple[str, ...] = ()
    obligation_scope: str = "contractual"

    def comparable_to(self, other: FactScope) -> bool:
        """Whether two facts are close enough to the same question to be in conflict.

        Deliberately excludes `clause`: an amendment's clause 1 replaces the MSA's clause
        4.3, and requiring them to match would make every amendment incomparable with the
        term it amends. Deliberately excludes the effective dates for the same reason --
        a replacement term always carries a later date than the term it replaces. Both are
        recorded and reported; neither decides comparability.

        An unknown `agreement_id` or an unnamed party list on either side compares: a
        document that declares neither is the ordinary single-agreement case, and refusing
        to compare there would turn every real conflict into silence. Absent `conditions`
        is not unknown -- it means the term is unconditional, which is a real difference
        from one that only holds "unless disputed" -- so those are matched exactly.
        """
        same_agreement = (
            self.agreement_id is None
            or other.agreement_id is None
            or self.agreement_id == other.agreement_id
        )
        same_parties = not self.parties or not other.parties or self.parties == other.parties
        return (
            same_agreement
            and same_parties
            and self.obligation_scope == other.obligation_scope
            and self.conditions == other.conditions
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "agreement_id": self.agreement_id,
            "clause": self.clause,
            "parties": list(self.parties),
            "effective_from": self.effective_from,
            "effective_to": self.effective_to,
            "conditions": list(self.conditions),
            "obligation_scope": self.obligation_scope,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> FactScope:
        value = value or {}
        return cls(
            agreement_id=value.get("agreement_id"),
            clause=value.get("clause"),
            parties=tuple(value.get("parties") or ()),
            effective_from=value.get("effective_from"),
            effective_to=value.get("effective_to"),
            conditions=tuple(value.get("conditions") or ()),
            obligation_scope=value.get("obligation_scope") or "contractual",
        )

    def describe(self) -> str:
        """Short human label for a rationale line. Only the parts that were found."""
        parts = [f"scope={self.obligation_scope}"]
        if self.agreement_id:
            parts.append(f"agreement={self.agreement_id}")
        if self.clause:
            parts.append(f"clause={self.clause}")
        if self.parties:
            parts.append(f"parties={'/'.join(self.parties)}")
        if self.effective_from:
            parts.append(f"effective={self.effective_from}")
        if self.conditions:
            parts.append(f"conditions={'; '.join(self.conditions)}")
        return ", ".join(parts)


@dataclass(slots=True)
class DocumentRelation:
    """One document's claim about how it stands to another, with the sentence saying so.

    `target_document_id` is None when the collection does not hold the document named --
    the claim is still recorded, because an amendment to an agreement nobody uploaded is
    exactly the case a human has to be told about rather than one to drop.
    """

    document_id: UUID
    kind: str  # one of RELATION_KINDS
    target_ref: str  # the agreement as this document names it
    evidence_quote: str
    block_id: UUID | None = None
    quote_start: int = 0
    quote_end: int = 0
    target_document_id: UUID | None = None
    id: UUID = field(default_factory=uuid4)


@dataclass(frozen=True, slots=True)
class EvidenceSpan:
    """Exactly where a fact came from, in coordinates that outlive the database.

    Every field here is a property of the bytes: which document (by content hash), which
    block of it (by position, not by row id), which characters, what those characters
    hash to, and which parser produced them. Nothing in it can be renumbered by a
    reimport, and nothing in it is a primary key -- a register hash built on row ids
    changes when the rows are rebuilt and stays the same when the evidence is replaced,
    which is backwards in both directions.

    Frozen because a span is a record of a reading, not a working value. Re-reading the
    document produces a new span; editing one in place would leave a fingerprint standing
    for evidence nobody ever saw.
    """

    document_sha256: str
    block_index: int
    char_start: int
    char_end: int
    quote_sha256: str
    # Which code turned bytes into this text: "native_pdf/1", "gemma_vlm/1", "docx/1".
    # A page a vision model transcribed is not the same evidence as one read natively,
    # even when the characters match, so the two never share a fingerprint.
    parser_version: str
    extractor_version: str
    page: int | None = None

    def fingerprint(self, *, key: str, value: dict[str, Any]) -> str:
        """The stable identity of "this claim, from this evidence".

        What a register item cites and what its content hash is built from. Two facts
        share a fingerprint only when the same parser read the same characters of the
        same document and the same extractor drew the same conclusion.
        """
        # Built from `as_dict()` rather than a hand-listed subset, so a field added to the
        # span is covered by the fingerprint the moment it exists. Listing them here left
        # `page` out: two spans differing only in which page they were read from hashed
        # identically, which is a claim about the evidence that was not true.
        return sha256_text(canonical_json({**self.as_dict(), "key": key, "value": value}))

    def as_dict(self) -> dict[str, Any]:
        return {
            "document_sha256": self.document_sha256,
            "block_index": self.block_index,
            "char_start": self.char_start,
            "char_end": self.char_end,
            "quote_sha256": self.quote_sha256,
            "parser_version": self.parser_version,
            "extractor_version": self.extractor_version,
            "page": self.page,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> EvidenceSpan | None:
        if not value:
            return None
        return cls(
            document_sha256=value["document_sha256"],
            block_index=value["block_index"],
            char_start=value["char_start"],
            char_end=value["char_end"],
            quote_sha256=value["quote_sha256"],
            parser_version=value["parser_version"],
            extractor_version=value["extractor_version"],
            page=value.get("page"),
        )


@dataclass(slots=True)
class FactCandidate:
    key: str
    value: dict[str, Any]
    block_id: UUID
    quote: str
    quote_start: int
    quote_end: int
    # Minted by `validate_citations`, from the stored block rather than from the model.
    # Empty until then: an extractor that could name its own fingerprint could name one
    # for evidence it never read.
    fingerprint: str = ""
    evidence: EvidenceSpan | None = None
    supported: bool = False
    reason: str | None = None
    scope: FactScope = field(default_factory=FactScope)
    # Filled in by the repository once the fact is persisted. Conflicts and
    # register citations reference facts by id, so a proposal without an id is
    # one that never reached the database.
    id: UUID | None = None


@dataclass(slots=True)
class StoredFact:
    """A grounded fact plus the document context needed to reason about precedence."""

    id: UUID
    key: str
    value: dict[str, Any]
    fingerprint: str
    quote: str
    document_id: UUID
    # Present when the caller needs to cite the source block, e.g. a rule finding
    # against a register item. Derivation itself only needs the fact identity.
    block_id: UUID | None = None
    doc_type: DocType = "unknown"
    supersedes_id: UUID | None = None
    scope: FactScope = field(default_factory=FactScope)
    evidence: EvidenceSpan | None = None


@dataclass(slots=True)
class Conflict:
    collection_id: UUID
    key: str
    fact_a_id: UUID
    fact_b_id: UUID
    kind: str  # contradiction | supersession_candidate
    rationale: str
    id: UUID = field(default_factory=uuid4)
    state: str = "open"
    detected_run: UUID | None = None


@dataclass(slots=True)
class Rule:
    code: str
    text: str
    severity: str  # blocker | major | minor | info
    scope: str  # source | deliverable | both
    # Register keys this rule judges in the deliverable stage. Empty means every affected
    # key, which is the right default for a rule stated about the contract as a whole.
    target_keys: list[str] = field(default_factory=list)
    id: UUID | None = None


@dataclass(slots=True)
class Ruleset:
    collection_id: UUID
    name: str
    version: int
    source_text: str
    rules: list[Rule] = field(default_factory=list)
    id: UUID | None = None


@dataclass(slots=True)
class FindingCitation:
    block_id: UUID
    quote: str
    quote_start: int
    quote_end: int


# What a human did with a finding. `dismissed` is a recorded disagreement with the system,
# not an erasure of it: the verdict, its rationale and its citations stay exactly as
# written, and the finding is re-evaluated on the next run rather than treated as settled.
REVIEW_DECISIONS = ("pending", "upheld", "dismissed")


@dataclass(slots=True)
class Finding:
    """One rule evaluated against one target, and separately, what a human did about it.

    A `pass` row is written as explicitly as a violation, so an empty findings list can
    only mean the stage never ran.

    `system_verdict` is what the evaluation concluded. Nothing writes it after the
    evaluation that produced it -- not a reviewer, not a later run, not a commit. A human
    who disagrees records that disagreement in `review_decision`, beside the verdict
    rather than over it. Collapsing the two into one mutable field is how a dismissed
    violation becomes indistinguishable from a rule that passed, and there is no way to
    tell afterwards which one the record is describing.
    """

    run_id: UUID
    rule_id: UUID
    rule_code: str
    target_kind: str  # document | register_item | register
    target_id: UUID
    system_verdict: str  # violation | pass | insufficient_evidence
    rationale: str
    # Human-readable name of the target: a filename for a document, a register key for a
    # register_item. `target_id` is a derived UUID and reads as nothing on its own.
    target_key: str = ""
    severity: str = "info"
    citations: list[FindingCitation] = field(default_factory=list)
    review_decision: str = "pending"
    decided_by: str | None = None
    # Set when a human dismissed an adverse verdict. The next run over this document must
    # re-evaluate rather than reuse: a dismissal is a judgement about one run's evidence,
    # and letting the source-rule cache serve it back forever turns it into policy.
    recheck_required: bool = False
    id: UUID | None = None

    @property
    def adverse(self) -> bool:
        return self.system_verdict != "pass"


@dataclass(slots=True)
class ReviewItem:
    run_id: UUID
    kind: str
    target_key: str
    payload: dict[str, Any]
    id: UUID = field(default_factory=uuid4)
    state: ReviewState = "pending"
    decided_by: str | None = None
    comment: str | None = None


@dataclass(slots=True)
class StageRecord:
    """One unit of work this run performed, and what came out of it.

    The event log says what happened in order and reads well; it is prose, and prose
    cannot answer "did this exact stage already complete". The ledger can, because it is
    keyed on the work rather than on the sequence.
    """

    run_id: UUID
    stage: str
    input_hash: str
    output_hash: str | None = None
    status: str = "completed"
    attempts: int = 1


@dataclass(slots=True)
class RunEvent:
    run_id: UUID
    stage: str
    decision: str
    reason: str
    next_node: str
    error_class: str | None = None
    duration_ms: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    # The three fields the cost/latency report reads that nothing used to write: which
    # model answered this event (for pricing and for telling a real call apart from an
    # offline one), whether it was served from a cache instead of computed, and which
    # external service (currently only "ocr") it called, if any.
    model: str | None = None
    cache_hit: bool = False
    external_service: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


# Separates the agreement from the obligation key in a register key's text form. Agreement
# identifiers are matched as `[A-Za-z0-9./_-]+` (see `services.relations`), so a colon pair
# cannot occur inside one and the split is unambiguous.
REGISTER_KEY_SEPARATOR = "::"


@dataclass(frozen=True, slots=True)
class RegisterKey:
    """Which agreement's `payment_due_days` this is.

    The register was keyed by obligation alone, one row per key for the whole collection.
    Two agreements in one collection therefore shared a row, and the later upload
    overwrote the earlier agreement's term -- silently, because two facts in different
    agreements are not a conflict and nothing was escalated. The agreement is part of the
    identity of a register row, not metadata hanging off it.

    `agreement_id` is `""` when no agreement was named, which is the ordinary
    single-agreement collection. It is a bucket, not a wildcard: it never matches a named
    agreement, and a value only lands there when the collection names no agreement at all.
    """

    agreement_id: str
    key: str

    @property
    def text(self) -> str:
        """The flat form. Review items, findings and graph state address keys as text."""
        return f"{self.agreement_id}{REGISTER_KEY_SEPARATOR}{self.key}"

    @classmethod
    def parse(cls, text: str) -> RegisterKey:
        agreement_id, separator, key = text.partition(REGISTER_KEY_SEPARATOR)
        # A bare key predates agreement scoping; read it into the unnamed bucket rather
        # than inventing an agreement called "payment_due_days".
        return cls("", text) if not separator else cls(agreement_id, key)

    def label(self) -> str:
        return f"{self.key} under {self.agreement_id}" if self.agreement_id else self.key


@dataclass(slots=True)
class RegisterItem:
    collection_id: UUID
    key: str
    value: dict[str, Any]
    state: str
    content_hash: str
    version: int = 1
    id: UUID | None = None
    citation_fact_ids: list[UUID] = field(default_factory=list)
    citation_fingerprints: list[str] = field(default_factory=list)
    # The agreement this row belongs to; "" when the collection names none.
    agreement_id: str = ""

    @property
    def register_key(self) -> RegisterKey:
        return RegisterKey(self.agreement_id, self.key)


@dataclass(slots=True)
class StaleProposal:
    """An approved proposal that was derived from a register item which has since moved.

    Committing it would silently overwrite whatever landed in between, so it is
    refused and reported instead: the document has to be re-run against the register
    as it now stands.
    """

    key: str
    expected_version: int | None
    expected_hash: str | None
    actual_version: int | None
    actual_hash: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "expected_version": self.expected_version,
            "expected_hash": self.expected_hash,
            "actual_version": self.actual_version,
            "actual_hash": self.actual_hash,
        }


@dataclass(slots=True)
class CommitResult:
    committed: list[RegisterItem] = field(default_factory=list)
    stale: list[StaleProposal] = field(default_factory=list)


RunStatus = Literal[
    "running", "awaiting_review", "committing", "committed", "blocked", "failed", "cancelled"
]


@dataclass(slots=True)
class RunSummary:
    """What a poller needs to know about a run without touching its checkpoint or
    lease: whether it is still moving, parked for a human, or done."""

    run_id: UUID
    collection_id: UUID
    status: RunStatus
    started_at: datetime
    ended_at: datetime | None = None
    # What put this run here -- api, mcp, watcher -- and the document it was started
    # over. `trigger_document_id` is None until the run's ingest stage has stored the
    # document, and stays None for a run that never got that far.
    trigger: str = "api"
    trigger_document_id: UUID | None = None
    # The ruleset this run was actually pinned to at `pin_ruleset`, once the graph has
    # reported it back -- not the collection's currently active ruleset, which may have
    # moved on since. None means either no playbook was pinned, or this run predates the
    # column; both report as unknown rather than guessing the active one.
    ruleset_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class WatchedCollection:
    """A collection that has named a directory for documents to arrive in.

    `watch_path` is a property of the collection, not of the watcher process: which
    directories get polled is a question the database answers, so a watcher restart,
    a second replica and a fresh container all poll the same set without being told.
    """

    id: UUID
    name: str
    watch_path: str


@dataclass(slots=True)
class CollectionSummary:
    """What the collection index/detail views need: identity plus counts derived
    from the other tables, not stored anywhere as its own row."""

    id: UUID
    name: str
    slug: str
    document_count: int
    register_row_count: int
    open_run_count: int
    last_activity_at: datetime | None
    watch_path: str | None = None


@dataclass(slots=True)
class DocumentSummary:
    """One row of a collection's document list -- identity and format, not the
    full text/blocks `get_document` returns for a single document."""

    id: UUID
    collection_id: UUID
    filename: str
    doc_type: DocType
    sha256: str
    ingested_at: datetime | None
    # Distinct block page numbers seen, or 1 for formats that carry no page anchors
    # (txt/docx) rather than an unset value implying the document has no length.
    pages: int = 1


@dataclass(slots=True)
class RegisterChange:
    """One register row's value before and after a commit, and which run caused it.

    This is `change_log` read back: the same evidence a live run's report proves
    unaffected rows preserved byte-for-byte, addressable later by `run_id` alone so a
    caller who only kept the run id can still ask "what did this actually change".
    """

    run_id: UUID
    register_item_id: UUID
    key: str
    old_value: dict[str, Any] | None
    new_value: dict[str, Any]
    old_hash: str | None
    new_hash: str
    changed_at: datetime

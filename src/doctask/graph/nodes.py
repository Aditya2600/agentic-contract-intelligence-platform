from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from time import perf_counter
from typing import Any, Literal
from uuid import UUID

from langgraph.types import Command, interrupt

from doctask.auth import reviewer_from_payload
from doctask.config import settings
from doctask.domain import (
    Block,
    Conflict,
    Document,
    DocumentRelation,
    FactCandidate,
    FactScope,
    Finding,
    RegisterKey,
    ReviewItem,
    Rule,
    RunEvent,
    StoredFact,
)
from doctask.graph.state import GraphState
from doctask.llm.base import ModelGateway, RuleExcerpt
from doctask.repositories.base import Repository
from doctask.services.citations import validate_citation
from doctask.services.cost_report import build_run_cost_report
from doctask.services.derivation import Derivation, derive_key
from doctask.services.grounding import check_qualifiers, check_value, span_for
from doctask.services.hashing import (
    candidate_basis_hash,
    canonical_json,
    register_content_hash,
    sha256_text,
    stage_input_hash,
)
from doctask.services.ids import target_uuid
from doctask.services.injection import Scan
from doctask.services.injection import scan as scan_block
from doctask.services.relations import detect_relations, self_declared_ref, to_relations
from doctask.services.rules import (
    SOURCE_RULE_EVALUATOR_VERSION,
    ground_verdict,
    rules_for,
    ruleset_hash,
    source_rule_cache_key,
)
from doctask.services.scoping import scope_for
from doctask.services.selection import select_excerpts
from doctask.services.supersession import supersession_evidence, supersession_evidence_near

# How an uncertain read is written to the event log, and the only way a later run can
# read it back. Kept as one constant because `rederive` parses what `ingest` wrote.
UNCERTAIN_PREFIX = "extraction was not certain: "


@dataclass(slots=True)
class NodeDependencies:
    repository: Repository
    model: ModelGateway


@dataclass(slots=True)
class RuleTarget:
    """One thing a rule can be evaluated against: a document, or a single register key.

    `excerpts` is per-rule rather than a fixed blob, because the evidence a rule needs is
    a function of the rule. `applies` is what lets a playbook aim a rule at named keys.
    """

    kind: str  # document | register_item
    id: UUID
    key: str
    label: str
    excerpts: Callable[[Rule], list[RuleExcerpt]]
    # What the rule judges, when that is not the excerpts themselves. A derived register
    # value is judged but never cited; the excerpts are what may be cited.
    statement: str | None = None
    applies: Callable[[Rule], bool] = lambda rule: True


async def _event(
    deps: NodeDependencies,
    state: GraphState,
    *,
    stage: str,
    decision: str,
    reason: str,
    next_node: str,
    started: float,
    error_class: str | None = None,
    usage: dict[str, int] | None = None,
    input_hash: str | None = None,
    output_hash: str | None = None,
    model: str | None = None,
    cache_hit: bool = False,
    external_service: str | None = None,
) -> None:
    """Record what happened, and -- for a stage that wrote something -- that it happened.

    The event log is prose in sequence and reads well; it cannot answer "did this exact
    stage already complete", because it is keyed on order rather than on the work. The
    ledger is keyed on `(run, stage, input_hash)` and can. Every node that writes domain
    state passes both hashes here, so the two records are produced together rather than
    by a second call site that can be forgotten.
    """
    usage = usage or {}
    if input_hash is not None:
        await deps.repository.record_stage(
            UUID(state["run_id"]),
            stage,
            input_hash=input_hash,
            output_hash=output_hash,
        )
    await deps.repository.add_event(
        RunEvent(
            run_id=UUID(state["run_id"]),
            stage=stage,
            decision=decision,
            reason=reason,
            next_node=next_node,
            error_class=error_class,
            duration_ms=int((perf_counter() - started) * 1000),
            tokens_in=usage.get("tokens_in", 0),
            tokens_out=usage.get("tokens_out", 0),
            model=model,
            cache_hit=cache_hit,
            external_service=external_service,
        )
    )


def _model_usage(deps: NodeDependencies) -> dict[str, int]:
    """Token counts from the last model call; FakeLLM reports its own approximation."""
    return getattr(deps.model, "last_usage", {}) or {}


def _model_name(deps: NodeDependencies) -> str:
    """Which model answered, for the cost report's pricing lookup and model breakdown.

    `.model` is the identifier a price table is keyed on (the real model id for the
    gateway, `"fake"` for the offline stand-in). `extractor_version` is a fallback for
    any future `ModelGateway` implementation that names itself differently.
    """
    return getattr(deps.model, "model", None) or getattr(
        deps.model, "extractor_version", "unknown"
    )


def _candidate_to_dict(candidate: FactCandidate) -> dict[str, Any]:
    result = asdict(candidate)
    result["block_id"] = str(candidate.block_id)
    result["id"] = str(candidate.id) if candidate.id else None
    # Tuples do not survive the checkpoint's JSON round trip; `FactScope.from_dict`
    # reads either shape back, so the scope is written as plain lists here.
    result["scope"] = candidate.scope.as_dict()
    return result


def _candidate_from_dict(value: dict[str, Any]) -> FactCandidate:
    return FactCandidate(
        key=value["key"],
        value=value["value"],
        block_id=UUID(value["block_id"]),
        quote=value["quote"],
        quote_start=value["quote_start"],
        quote_end=value["quote_end"],
        fingerprint=value["fingerprint"],
        supported=value.get("supported", False),
        reason=value.get("reason"),
        scope=FactScope.from_dict(value.get("scope")),
        id=UUID(value["id"]) if value.get("id") else None,
    )


def _derivation_to_dict(derivation: Derivation) -> dict[str, Any]:
    return {
        "key": derivation.key,
        "agreement_id": derivation.agreement_id,
        "scoped_key": RegisterKey(derivation.agreement_id, derivation.key).text,
        "value": derivation.value,
        "state": derivation.state,
        "citation_fact_ids": [str(fact_id) for fact_id in derivation.citation_fact_ids],
        "citation_fingerprints": derivation.citation_fingerprints,
        "reason": derivation.reason,
        "conflict": (
            {
                "kind": derivation.conflict.kind,
                "rationale": derivation.conflict.rationale,
                "fact_a_id": str(derivation.conflict.fact_a_id),
                "fact_b_id": str(derivation.conflict.fact_b_id),
            }
            if derivation.conflict
            else None
        ),
    }


def _finding_to_dict(finding: Finding) -> dict[str, Any]:
    return {
        "id": str(finding.id) if finding.id else None,
        "rule_code": finding.rule_code,
        "system_verdict": finding.system_verdict,
        "review_decision": finding.review_decision,
        "decided_by": finding.decided_by,
        "recheck_required": finding.recheck_required,
        "rationale": finding.rationale,
        "severity": finding.severity,
        "target_kind": finding.target_kind,
        "target_key": finding.target_key,
        "citations": [
            {
                "block_id": str(citation.block_id),
                "quote": citation.quote,
                "quote_start": citation.quote_start,
                "quote_end": citation.quote_end,
            }
            for citation in finding.citations
        ],
    }


def _register_excerpts(
    row: dict[str, Any],
    facts: dict[UUID, StoredFact],
    blocks: dict[UUID, Block],
) -> list[RuleExcerpt]:
    """Evidence for one candidate register key: the source blocks it was derived from.

    The proposed value itself is deliberately not an excerpt. It is what the rule judges,
    not evidence for anything, and offering it as a quotable excerpt invites a citation
    that proves the rendering rather than the contract. It travels as `statement` instead.
    """
    excerpts: list[RuleExcerpt] = []
    for fact_id in row["citation_fact_ids"]:
        fact = facts.get(UUID(str(fact_id)))
        block = blocks.get(fact.block_id) if fact and fact.block_id else None
        # Belt and braces: extraction never reads a quarantined block, so no stored fact
        # should cite one. If one ever does, it still does not become rule context.
        if block is None or block.injection_flag:
            continue
        excerpts.append(
            RuleExcerpt(
                index=len(excerpts),
                label=f"source block {block.index} behind {row['key']}",
                text=block.text,
                block_id=block.id,
            )
        )
    return excerpts


def _register_before(items: dict[str, Any]) -> dict[str, Any]:
    """The stored items a run derives against, flattened into the checkpoint.

    Carries the version and content hash so an approved proposal can be checked at
    commit against what it was actually derived from.
    """
    return {
        key: {
            "value": item.value,
            "state": item.state,
            "content_hash": item.content_hash,
            "version": item.version,
            "citation_fact_ids": [str(fact_id) for fact_id in item.citation_fact_ids],
            "citation_fingerprints": item.citation_fingerprints,
        }
        for key, item in items.items()
    }


def _register_statement(row: dict[str, Any]) -> str:
    scoped = RegisterKey(row.get("agreement_id", ""), row["key"])
    return f"{scoped.label()} = {canonical_json(row['value'])} (state: {row['state']})"


def _resolve_agreement(named: tuple[str, ...], agreement_id: str | None) -> tuple[str, tuple[str, ...]]:
    """Which register row a fact belongs to, and what makes that a guess.

    Returns the agreement bucket and, when the bucket could not be resolved, the
    agreements it might have been. A document that names its agreement resolves to it. A
    document that names none resolves to the collection's only agreement when there is
    exactly one -- an SOW under the single MSA in the folder belongs to that MSA, and
    filing it separately would split the register on a distinction nobody drew. With no
    agreements named anywhere the bucket is "", which is every single-agreement collection
    this system has ever run. With more than one, the answer is unknown, and saying so is
    the whole point: see the `ambiguous_scope` branch in `derive_key`.
    """
    if agreement_id:
        return agreement_id, ()
    if len(named) == 1:
        return named[0], ()
    return "", named


def _after_source_rules(state: GraphState) -> str:
    """Where the run goes once the source stage has produced its verdicts.

    The source stage now runs for every ingested document, so it sits on three different
    paths and has to hand each one back to where it was going. A re-derive already knows
    its keys; a duplicate has no new facts to diff and only needs its findings routed; an
    ordinary upload goes on to work out which register keys it touched.
    """
    if state.get("rederive_from_run"):
        return "detect_conflicts"
    if state.get("duplicate_document"):
        return "route_source_findings"
    return "diff_against_register"


def _scan_fields(block: Block) -> dict[str, Any]:
    """Re-derive a stored block's signals from its stored text.

    `document_blocks` keeps the boolean, not the evidence behind it. Re-scanning is
    deterministic over the same characters, so a re-derive shows a reviewer the same
    signals the original run did rather than an unexplained flag.
    """
    result = scan_block(block.text, extraction_method=block.extraction_method)
    return {
        "signals": list(result.signals),
        "quotes": list(result.quotes),
        "normalised": result.normalised,
    }


def _decision_binding(state: GraphState) -> dict[str, Any]:
    """What a decision made right now would be a decision about.

    Stamped into every review item so an approval is not a free-floating "yes" but a yes
    to one register, at these versions, judged by one playbook. `verify_review_binding`
    recomputes all of it before the commit; anything that moved in between means the
    human agreed to something that is no longer what would be written.
    """
    return {
        "basis_hash": state.get("candidate_basis_hash"),
        "ruleset_hash": state.get("ruleset_hash"),
        "item_versions": {
            row["scoped_key"]: row.get("version")
            for row in state.get("candidate_register", [])
        },
    }


def _finding_decisions(items: list[ReviewItem]) -> dict[UUID, str]:
    """Map decided review items back onto the findings they were about.

    `approved` on a finding means "this problem is real" -- the finding is upheld.
    `rejected` means the reviewer judged it not to apply, which is a dismissal recorded
    against the verdict, never a deletion of it.
    """
    return {
        UUID(str(item.payload["finding_id"])): (
            "upheld" if item.state == "approved" else "dismissed"
        )
        for item in items
        if item.kind == "finding" and item.payload.get("finding_id")
    }


def _unresolved_source_findings(state: GraphState) -> list[dict[str, Any]]:
    """Source verdicts a human still owes a decision on. A pass needs none."""
    return [
        finding
        for finding in state.get("findings", [])
        if finding["target_kind"] == "document" and finding["system_verdict"] != "pass"
    ]


def _rules_summary(state: GraphState) -> dict[str, Any]:
    """Explicit counts against an explicit denominator.

    `clean` is the only field allowed to mean "no findings", and it is the conjunction of
    every condition that has to hold for that claim to be true: every rule that was
    supposed to run produced a verdict, none failed, and none of those verdicts was
    adverse. A count of zero on its own proves nothing - a stage that never ran, a
    collection with no playbook, and a clean corpus all report zero violations.
    """
    findings = state.get("findings", [])
    counts = Counter(finding["system_verdict"] for finding in findings)
    expected = state.get("rules_expected", 0)
    completed = state.get("rules_completed", 0)
    failed = state.get("rules_failed", 0)
    complete = failed == 0 and completed == expected and completed == len(findings)
    # Everything above judges the rules. This judges the evidence they were judged on: a
    # page a vision model transcribed, a rotated page, a table flattened into rows. Every
    # verdict here may be sound and still have been reached over text that is not quite
    # what the file says, and a `clean` that cannot see that is the failure this whole
    # pipeline exists to avoid.
    extraction_warnings = state.get("extraction_warnings", [])
    # Who signed off at Gate 2. A run reaches its report with this unset only when the
    # deliverable stage never faced a human, and "nothing was wrong" with nobody's name
    # on it is a claim the system is making about itself.
    reviewed_by = state.get("deliverable_review_by")
    dismissed = [
        finding
        for finding in findings
        if finding.get("review_decision") == "dismissed" and finding["system_verdict"] != "pass"
    ]
    summary = {
        "rules_expected": expected,
        "rules_completed": completed,
        "rules_failed": failed,
        "evaluation_complete": complete,
        "reviewed_by": reviewed_by,
        # A dismissed verdict is kept, reported, and re-earned next run. It is not a pass,
        # and a run carrying one is not clean.
        "dismissed": [
            {
                "rule_code": finding["rule_code"],
                "target_key": finding["target_key"],
                "system_verdict": finding["system_verdict"],
                "decided_by": finding.get("decided_by"),
            }
            for finding in dismissed
        ],
        "extraction_warnings": extraction_warnings,
        "extraction_certain": not extraction_warnings,
        "pass": counts["pass"],
        "violation": counts["violation"],
        "insufficient_evidence": counts["insufficient_evidence"],
        # `expected > 0` is deliberate and goes beyond "no errors, no violations": a
        # collection with no playbook checked nothing, and unchecked is not clean.
        "clean": (
            complete
            and expected > 0
            and not extraction_warnings
            and counts["violation"] == 0
            and counts["insufficient_evidence"] == 0
            # A clean result is a human's claim, not the pipeline's. Gate 2 opens on
            # every deliverable evaluation precisely so this can never be true of a run
            # nobody looked at.
            and reviewed_by is not None
            and state.get("deliverable_confirmed", False)
        ),
        "findings": [
            {
                "rule_code": finding["rule_code"],
                "system_verdict": finding["system_verdict"],
                "review_decision": finding["review_decision"],
                "decided_by": finding["decided_by"],
                "severity": finding["severity"],
                "target_kind": finding["target_kind"],
                "target_key": finding["target_key"],
                "rationale": finding["rationale"],
                "citations": finding["citations"],
            }
            for finding in findings
        ],
    }
    assert not summary["clean"] or len(findings) == expected == summary["pass"], (
        "clean may only be claimed when every expected rule produced a pass"
    )
    assert not summary["clean"] or not extraction_warnings, (
        "clean may only be claimed over evidence the extractor was sure of"
    )
    assert not summary["clean"] or reviewed_by, (
        "clean may only be claimed once a named human has confirmed the deliverable"
    )
    return summary


def make_nodes(deps: NodeDependencies) -> dict[str, Any]:
    async def _prior_extraction_warnings(source_run: UUID) -> list[str]:
        """What the run that actually read this document was unsure of.

        The event log is where that doubt is durable; the run state of a finished run is
        not something a new run can read. Re-emitting the warnings verbatim keeps the
        retry's report saying the same thing about the same bytes.
        """
        return [
            warning
            for event in await deps.repository.list_events(source_run)
            if event.error_class == "extraction" and event.reason.startswith(UNCERTAIN_PREFIX)
            for warning in event.reason.removeprefix(UNCERTAIN_PREFIX).split("; ")
        ]

    async def _record_type(state: GraphState, doc_type: str, confidence: float | None) -> None:
        """Persist the decided type. What this document supersedes is settled later, in
        `link_documents`, where the sentences that make the claim have been parsed into
        blocks and the agreement it names can be resolved to an actual document."""
        await deps.repository.set_document_type(UUID(state["document_id"]), doc_type, confidence)

    async def _link_supersession(
        state: GraphState, relations: list[DocumentRelation]
    ) -> str | None:
        """Link the document this one replaces, preferring the one it actually names.

        The fallback picks the most recent agreement in the collection, which is only
        right when there is one agreement to pick. With two, it silently aimed every
        amendment at whichever MSA was uploaded last -- so an amendment to the January
        agreement rewrote a term in the June one. A named target wins over recency.
        """
        document_id = UUID(state["document_id"])
        evidence = supersession_evidence(state["input_document"]["text"])
        if evidence is None:
            return None
        target = next(
            (
                relation.target_document_id
                for relation in relations
                if relation.kind in {"amends", "supersedes"} and relation.target_document_id
            ),
            None,
        )
        if target is None:
            target = await deps.repository.find_supersession_target(
                UUID(state["collection_id"]), document_id
            )
        if target is None:
            return None
        await deps.repository.link_supersession(document_id, target)
        return evidence

    async def pin_ruleset(state: GraphState) -> Command[Literal["ingest", "rederive"]]:
        """Resolve the playbook once, at run start, and pin it for the whole run.

        Both rule stages read the pinned id rather than re-resolving `get_active_ruleset`,
        which they used to do independently - an upload landing between them would have
        judged one document against two different playbooks. The hash is what makes a
        finding explicable after the playbook is edited: `name v2` is a moving target.
        """
        started = perf_counter()
        ruleset = await deps.repository.get_active_ruleset(UUID(state["collection_id"]))
        digest = ruleset_hash(ruleset) if ruleset else None
        # A re-derive run has no upload to ingest: its document is already stored.
        next_node = "rederive" if state.get("rederive_from_run") else "ingest"
        await _event(
            deps,
            state,
            stage="pin_ruleset",
            decision="continue" if ruleset else "skip",
            reason=(
                f"pinned ruleset {ruleset.name} v{ruleset.version} "
                f"({len(ruleset.rules)} rules, sha256:{digest[:12]})"
                if ruleset and digest
                else "collection has no playbook; both rule stages will have nothing to run"
            ),
            next_node=next_node,
            started=started,
        )
        return Command(
            update={
                "ruleset_id": str(ruleset.id) if ruleset else None,
                "ruleset_hash": digest,
            },
            goto=next_node,
        )

    async def rederive(
        state: GraphState,
    ) -> Command[Literal["apply_source_rules", "snapshot_diff_report"]]:
        """Redo what an earlier run approved but could not commit.

        A refused proposal cannot be retried by its own run - `review_items` is unique
        per run and key, so the run cannot propose the same key twice - and re-uploading
        the document short-circuits on its SHA-256. So the remedy is a new run over the
        document already stored: no upload, no re-extraction, no model spend. It reuses
        the stored facts and blocks, reads the register as it now stands, and hands the
        derivation stage only the keys that never landed. Everything after this node is
        the ordinary path, which is what makes the retry a fresh human review rather than
        a replay of a decision made against a value that has since changed.
        """
        started = perf_counter()
        collection_id = UUID(state["collection_id"])
        source_run = UUID(state["rederive_from_run"])
        # This run re-reads nothing, so nothing here can re-discover that the document
        # was rotated, scanned or repaired. Without carrying the original read's doubt
        # forward, a retry of an uncertain document reports `extraction_certain: true`
        # and may call itself clean - which is exactly the silent-clean failure the
        # warnings exist to prevent, reintroduced by the one path that skips extraction.
        warnings = await _prior_extraction_warnings(source_run)
        approved = [
            item
            for item in await deps.repository.list_review_items(source_run)
            if item.state == "approved" and item.kind in {"register_update", "conflict"}
        ]
        stored = await deps.repository.get_register_items(
            collection_id, [item.target_key for item in approved]
        )
        # An approval landed if the stored item carries the hash it proposed. Anything
        # else was refused as stale, or overwritten since, and is this run's work.
        pending = [
            item
            for item in approved
            if item.target_key not in stored
            or stored[item.target_key].content_hash != item.payload["after"]["content_hash"]
        ]
        if not pending:
            await _event(
                deps,
                state,
                stage="rederive",
                decision="skip",
                reason=(
                    f"run {source_run} has no approved proposal left outstanding; "
                    f"{len(approved)} approval(s) are all in the register"
                ),
                next_node="snapshot_diff_report",
                started=started,
            )
            return Command(
                update={"extraction_warnings": warnings}, goto="snapshot_diff_report"
            )

        document = await deps.repository.get_document(
            UUID(pending[0].payload["document_id"])
        )
        if document is None:
            raise ValueError(f"run {source_run} proposed against a document that is gone")

        keys = sorted({item.target_key for item in pending})
        stored_blocks = sorted(
            (await deps.repository.get_blocks(document.id)).values(), key=lambda b: b.index
        )
        quarantined = [
            {
                "block_id": str(block.id),
                "index": block.index,
                "page": block.page,
                "extraction_method": block.extraction_method,
                **_scan_fields(block),
            }
            for block in stored_blocks
            if block.injection_flag
        ]
        # Both flags are deterministic functions of the stored text, so re-computing them
        # costs nothing and keeps this run judged exactly as the original was.
        evidence = (
            supersession_evidence(document.text) if document.doc_type == "amendment" else None
        )
        await _event(
            deps,
            state,
            stage="rederive",
            decision="continue",
            reason=(
                f"re-deriving {len(keys)} key(s) left outstanding by run {source_run} "
                f"({', '.join(keys)}) from stored facts on document {document.filename}"
            ),
            next_node="apply_source_rules",
            started=started,
        )
        return Command(
            update={
                "document_id": str(document.id),
                "document_sha256": document.sha256,
                "input_document": {
                    "filename": document.filename,
                    "mime_type": document.mime_type,
                    "text": document.text,
                },
                "document_type": document.doc_type,
                "supersession_evidence": evidence,
                # Re-read from the stored blocks rather than re-scanned over the whole
                # document: a re-derive skips `parse_blocks`, and rescanning the joined
                # text would flag the document instead of the paragraph and quarantine
                # evidence the original run happily used.
                "injection_flag": bool(quarantined),
                "quarantined_blocks": quarantined,
                "affected_keys": keys,
                "extraction_warnings": warnings,
                "register_before": _register_before(
                    {key: stored[key] for key in keys if key in stored}
                ),
            },
            goto="apply_source_rules",
        )

    async def ingest(
        state: GraphState,
    ) -> Command[Literal["short_circuit", "classify"]]:
        started = perf_counter()
        raw = state["input_document"]
        document = Document(
            collection_id=UUID(state["collection_id"]),
            filename=raw["filename"],
            mime_type=raw["mime_type"],
            text=raw["text"],
            sha256=sha256_text(raw["text"]),
        )
        input_hash = stage_input_hash("ingest", document.sha256)
        # Written *before* the document, and this is the whole reason the ledger has a
        # `started` status. A process killed between `put_document` and its checkpoint
        # leaves the row in the table with no record that this run put it there -- so the
        # retry sees its own write, calls it a duplicate, short-circuits, and never
        # extracts a single fact from a document that was never actually processed. The
        # `started` row is what lets the replay recognise its own earlier attempt.
        attempted = await deps.repository.stage_record(
            UUID(state["run_id"]), "ingest", input_hash
        )
        await deps.repository.record_stage(
            UUID(state["run_id"]), "ingest", input_hash=input_hash, status="started"
        )
        stored, duplicate = await deps.repository.put_document(document)
        if duplicate and attempted is not None:
            duplicate = False
        next_node = "short_circuit" if duplicate else "classify"
        warnings = state.get("extraction_warnings", [])
        if warnings:
            # In the event log rather than only in the report: the reason a run could not
            # claim a clean result has to be auditable after the fact, not inferred.
            await _event(
                deps,
                state,
                stage="ingest",
                decision="uncertain",
                reason=UNCERTAIN_PREFIX + "; ".join(warnings),
                next_node=next_node,
                started=started,
                error_class="extraction",
            )
        await _event(
            deps,
            state,
            stage="ingest",
            decision="skip" if duplicate else "continue",
            reason="collection already contains this SHA-256" if duplicate else "new document stored",
            next_node=next_node,
            started=started,
            input_hash=stage_input_hash("ingest", document.sha256),
            output_hash=stage_input_hash(str(stored.id), duplicate),
        )
        return Command(
            update={
                "document_id": str(stored.id),
                "document_sha256": stored.sha256,
                "duplicate_document": duplicate,
            },
            goto=next_node,
        )

    async def short_circuit(state: GraphState) -> Command[Literal["apply_source_rules"]]:
        """A duplicate skips extraction and derivation. It does not skip the playbook.

        The bytes are already extracted, so there is nothing for the model to read again
        and no register key that can move. But the playbook may have been edited since the
        first upload, and "we have seen this file before" is not an answer to "does it
        pass the rules we have now". The source stage runs; its cache is what stops the
        second upload paying for it when nothing that decides a verdict has changed.
        """
        started = perf_counter()
        await _event(
            deps,
            state,
            stage="short_circuit",
            decision="skip",
            reason=(
                "duplicate document; no extraction, derivation or register work, "
                "source rules still evaluated against the pinned playbook"
            ),
            next_node="apply_source_rules",
            started=started,
        )
        return Command(goto="apply_source_rules")

    async def classify(
        state: GraphState,
    ) -> Command[Literal["classify_review", "parse_blocks"]]:
        started = perf_counter()
        doc_type, confidence, reason = await deps.model.classify(state["input_document"]["text"])
        next_node = "parse_blocks" if confidence >= settings.classification_threshold else "classify_review"
        if next_node == "parse_blocks":
            await _record_type(state, doc_type, confidence)
        await _event(
            deps,
            state,
            stage="classify",
            decision="continue" if next_node == "parse_blocks" else "escalate",
            reason=reason,
            next_node=next_node,
            started=started,
            usage=_model_usage(deps),
            model=_model_name(deps),
        )
        return Command(
            update={"document_type": doc_type, "document_type_confidence": confidence},
            goto=next_node,
        )

    async def classify_review(
        state: GraphState,
    ) -> Command[Literal["parse_blocks"]]:
        started = perf_counter()
        allowed = [
            "master_agreement",
            "amendment",
            "sow",
            "invoice",
            "purchase_order",
            "policy",
            "unknown",
        ]
        answer = interrupt(
            {
                "kind": "document_classification",
                "document_id": state["document_id"],
                "proposed_type": state.get("document_type", "unknown"),
                "confidence": state.get("document_type_confidence", 0.0),
                "allowed": allowed,
                "required_shape": {
                    "document_type": "one of `allowed`",
                    "actor_id": "stamped from the reviewer's credential, not the caller",
                },
            }
        )
        # Same rule as the other gates: the type a document is filed under decides
        # whether it can supersede anything, so only an authenticated human sets it.
        actor_id = reviewer_from_payload(answer).actor_id
        selected = answer.get("document_type")
        if selected not in allowed:
            raise ValueError(f"document_type must be one of {allowed}, got {selected!r}")
        await _record_type(state, selected, state.get("document_type_confidence"))
        await _event(
            deps,
            state,
            stage="classify_review",
            decision="human_override",
            reason=f"{actor_id} selected document type {selected}",
            next_node="parse_blocks",
            started=started,
        )
        return Command(update={"document_type": selected}, goto="parse_blocks")

    async def parse_blocks(state: GraphState) -> Command[Literal["link_documents"]]:
        started = perf_counter()
        text = state["input_document"]["text"]
        # An upload already split the file and knows which page and extractor each piece
        # came from; a JSON body carries plain text and gets the same blank-line split.
        supplied = state["input_document"].get("blocks") or [
            {"text": paragraph} for paragraph in text.split("\n\n") if paragraph.strip()
        ]
        blocks: list[Block] = []
        scans: list[Scan] = []
        cursor = 0
        for index, raw_block in enumerate(supplied):
            paragraph = raw_block["text"].strip()
            start = text.find(paragraph, cursor)
            end = start + len(paragraph)
            cursor = end
            method = raw_block.get("extraction_method", "txt")
            # Scanned here, at the one boundary where bytes become a citable unit. Per
            # block, never per document: one hostile paragraph in a sixty-page contract
            # must not cost the other fifty-nine, or an attacker buys a denial of service
            # for the price of one sentence.
            scan = scan_block(paragraph, extraction_method=method)
            scans.append(scan)
            blocks.append(
                Block(
                    document_id=UUID(state["document_id"]),
                    index=index,
                    text=paragraph,
                    text_sha256=sha256_text(paragraph),
                    char_start=start,
                    char_end=end,
                    page=raw_block.get("page"),
                    extraction_method=method,
                    injection_flag=scan.suspicious,
                )
            )
        await deps.repository.put_blocks(blocks)
        methods = sorted({block.extraction_method for block in blocks})
        flagged = [block for block in blocks if block.injection_flag]
        await _event(
            deps,
            state,
            stage="parse_blocks",
            decision="continue",
            reason=(
                f"created {len(blocks)} stable text blocks via {', '.join(methods)}"
                + (f"; {len(flagged)} carry an injection signal" if flagged else "")
            ),
            next_node="link_documents",
            started=started,
            input_hash=stage_input_hash("parse_blocks", state["document_id"], sha256_text(text)),
            output_hash=stage_input_hash([block.text_sha256 for block in blocks]),
        )
        return Command(
            update={
                "block_ids": [str(b.id) for b in blocks],
                "quarantined_blocks": [
                    {
                        "block_id": str(block.id),
                        "index": block.index,
                        "page": block.page,
                        "extraction_method": block.extraction_method,
                        "signals": list(scan.signals),
                        # What the model would have read, not what the page renders. For
                        # a hidden payload those are different strings, and the rendered
                        # one is the reason nobody noticed.
                        "quotes": list(scan.quotes),
                        "normalised": scan.normalised,
                    }
                    for block, scan in zip(blocks, scans, strict=True)
                    if block.injection_flag
                ],
            },
            goto="link_documents",
        )

    async def link_documents(state: GraphState) -> Command[Literal["detect_injection"]]:
        """Record how this document says it stands to the others, and scope it.

        Which agreement a term belongs to is what decides whether it can be in conflict
        with another term at all. Read it from the document's own sentences and store the
        sentence: an amendment amends what it says it amends, not whatever was uploaded
        before it. A relation whose target is not in the collection is still recorded --
        an amendment to an agreement nobody uploaded is the case a human most needs.
        """
        started = perf_counter()
        document_id = UUID(state["document_id"])
        collection_id = UUID(state["collection_id"])
        blocks = sorted(
            (await deps.repository.get_blocks(document_id)).values(), key=lambda b: b.index
        )
        doc_type = state.get("document_type") or "unknown"

        self_ref = self_declared_ref(blocks, doc_type=doc_type)
        if self_ref:
            await deps.repository.set_agreement_ref(document_id, self_ref)

        detected = detect_relations(blocks)
        known = await deps.repository.documents_by_agreement_ref(collection_id)
        relations = to_relations(detected, document_id=document_id, resolved=known)
        await deps.repository.put_document_relations(relations)

        amends = any(relation.kind in {"amends", "supersedes"} for relation in relations)
        evidence = (
            await _link_supersession(state, relations) if doc_type == "amendment" else None
        )
        # The agreement this document's facts belong to: the one it *is* if it is an
        # agreement, otherwise the one it names. Preference order is by how strong the
        # claim is -- amending an agreement says more about which agreement you are
        # talking about than mentioning it does.
        agreement_id = self_ref
        if agreement_id is None:
            by_kind = {relation.kind: relation.target_ref for relation in reversed(relations)}
            agreement_id = next(
                (
                    by_kind[kind]
                    for kind in ("amends", "governed_by", "references", "supersedes")
                    if by_kind.get(kind)
                ),
                None,
            )

        unresolved = [
            relation for relation in relations if relation.target_document_id is None
        ]
        await _event(
            deps,
            state,
            stage="link_documents",
            decision="continue",
            reason=(
                f"{len(relations)} relation(s) claimed "
                f"({', '.join(sorted({r.kind for r in relations})) or 'none'}); "
                f"{len(unresolved)} name no document in this collection; "
                f"facts scoped to agreement {agreement_id or 'unnamed'}"
            ),
            next_node="detect_injection",
            started=started,
            input_hash=stage_input_hash("link_documents", state["document_id"]),
            output_hash=stage_input_hash(
                agreement_id, sorted((r.kind, r.target_ref) for r in relations)
            ),
        )
        return Command(
            update={
                "agreement_id": agreement_id,
                "amends_agreement": amends,
                "supersession_evidence": evidence,
                "relations": [
                    {
                        "kind": relation.kind,
                        "target_ref": relation.target_ref,
                        "target_document_id": (
                            str(relation.target_document_id)
                            if relation.target_document_id
                            else None
                        ),
                        "evidence_quote": relation.evidence_quote,
                        "block_id": str(relation.block_id) if relation.block_id else None,
                    }
                    for relation in relations
                ],
            },
            goto="detect_injection",
        )

    async def detect_injection(state: GraphState) -> Command[Literal["extract_facts"]]:
        """Account for what was quarantined. Never route on it, never clear anything.

        The scan already happened, per block, in `parse_blocks`. This node exists to make
        the result visible and to put it where `clean` can see it -- a document whose
        evidence was partly withheld cannot be reported as fully judged, whichever way the
        remaining rules came out.

        There is no branch here on a negative result. A clean scan buys the document
        nothing it would not have had anyway: every proposal still needs a human, the
        model still gets no credential, and the register still cannot be written by text.
        Detection is telemetry; the boundary is that document text has no capability.
        """
        started = perf_counter()
        quarantined = state.get("quarantined_blocks", [])
        # Quarantine is a hole in the evidence, and a rule judged over a hole is not a
        # clean result. Carried as an extraction warning because that is already the one
        # channel `_rules_summary` refuses to call clean over -- evidence the pipeline
        # could not read is the same problem whether a scanner or a rotated page caused it.
        warnings = state.get("extraction_warnings", []) + [
            f"block {entry['index']} withheld from extraction and rule context "
            f"({', '.join(entry['signals'])})"
            for entry in quarantined
        ]
        await _event(
            deps,
            state,
            stage="detect_injection",
            decision="quarantine" if quarantined else "continue",
            reason=(
                f"{len(quarantined)} of {len(state.get('block_ids', []))} block(s) "
                f"quarantined: {'; '.join(sorted({s for e in quarantined for s in e['signals']}))}"
                if quarantined
                else f"{len(state.get('block_ids', []))} block(s) matched no known "
                "injection pattern, which grants them nothing"
            ),
            next_node="extract_facts",
            started=started,
            error_class="policy" if quarantined else None,
        )
        return Command(
            update={
                "injection_flag": bool(quarantined),
                "extraction_warnings": warnings,
            },
            goto="extract_facts",
        )

    async def extract_facts(state: GraphState) -> Command[Literal["validate_citations"]]:
        started = perf_counter()
        block_map = await deps.repository.get_blocks(UUID(state["document_id"]))
        wider_context = state.get("validation_attempt", 0) > 0
        candidates: list[FactCandidate] = []
        tokens_in = tokens_out = 0
        document_text = state["input_document"]["text"]
        doc_type = state.get("document_type") or "unknown"
        withheld = 0
        for block_id in state.get("block_ids", []):
            block = block_map[UUID(block_id)]
            if block.injection_flag:
                # Not sent to the model at all. Extraction is the step that turns text
                # into something the register can hold, so a block that tried to give
                # instructions does not get to propose a value -- and skipping the call
                # is also the only way to be sure its text never reaches a prompt.
                withheld += 1
                continue
            extracted = await deps.model.extract(block, wider_context=wider_context)
            for candidate in extracted:
                # Scope is derived here, never asked of the model: which agreement a term
                # belongs to decides whether it conflicts with anything, and that has to
                # be reproducible from the stored text during an audit.
                candidate.scope = scope_for(
                    doc_type=doc_type,
                    document_text=document_text,
                    block_text=block.text,
                    quote=candidate.quote,
                    agreement_id=state.get("agreement_id"),
                    amends=state.get("amends_agreement", False),
                )
            candidates.extend(extracted)
            usage = _model_usage(deps)
            tokens_in += usage.get("tokens_in", 0)
            tokens_out += usage.get("tokens_out", 0)
        await _event(
            deps,
            state,
            stage="extract_facts",
            decision="continue",
            reason=(
                f"model proposed {len(candidates)} atomic facts from "
                f"{len(state.get('block_ids', [])) - withheld} block(s)"
                + (f"; {withheld} quarantined block(s) were never sent" if withheld else "")
            ),
            next_node="validate_citations",
            started=started,
            usage={"tokens_in": tokens_in, "tokens_out": tokens_out},
            model=_model_name(deps),
        )
        return Command(
            update={"fact_candidates": [_candidate_to_dict(c) for c in candidates]},
            goto="validate_citations",
        )

    async def validate_citations_node(
        state: GraphState,
    ) -> Command[Literal["retry_extract", "mark_unsupported", "apply_source_rules"]]:
        started = perf_counter()
        block_map = await deps.repository.get_blocks(UUID(state["document_id"]))
        candidates = [_candidate_from_dict(c) for c in state.get("fact_candidates", [])]
        valid: list[FactCandidate] = []
        invalid: list[FactCandidate] = []
        for candidate in candidates:
            block = block_map.get(candidate.block_id)
            if block is None:
                # Cited block is not part of this document, so it is not ours to quote.
                candidate.supported = False
                candidate.reason = "cited block does not belong to this document"
                invalid.append(candidate)
                continue
            checks = [validate_citation(block, candidate)]
            if checks[0].ok:
                # The quote is real. Now: does it say what the value claims? A verbatim
                # quote of the wrong sentence, of a nearby number, or of a clause that
                # denies the term all pass the offsets check and none of them ground
                # anything.
                checks.append(check_value(candidate.key, candidate.value, candidate.quote))
                if candidate.scope.obligation_scope == "contractual":
                    checks.append(
                        check_qualifiers(block.text, candidate.quote_start, candidate.quote_end)
                    )
            failed = next((check for check in checks if not check.ok), None)
            candidate.supported = failed is None
            candidate.reason = failed.reason if failed else checks[-1].reason
            if failed is None:
                # Minted here, from the stored block, and only for a fact that survived
                # every check. A candidate that never grounded has no evidence to name.
                candidate.evidence = span_for(
                    block,
                    candidate,
                    document_sha256=state["document_sha256"],
                    extractor_version=getattr(deps.model, "extractor_version", "unknown"),
                )
                candidate.fingerprint = candidate.evidence.fingerprint(
                    key=candidate.key, value=candidate.value
                )
            (valid if candidate.supported else invalid).append(candidate)

        attempt = state.get("validation_attempt", 0)
        if invalid and attempt < settings.max_validation_repairs:
            next_node = "retry_extract"
            decision = "retry"
            reason = f"{len(invalid)} citation candidates failed deterministic validation"
        elif invalid:
            next_node = "mark_unsupported"
            decision = "abstain"
            reason = f"{len(invalid)} candidates remain ungrounded after repair budget"
        else:
            # Straight into the source stage. It used to sit after derivation, which meant
            # a document that moved no register key never reached it at all.
            next_node = "apply_source_rules"
            decision = "continue"
            reason = "all proposed facts have verbatim source evidence"
            await deps.repository.put_facts(UUID(state["collection_id"]), valid)

        await _event(
            deps,
            state,
            stage="validate_citations",
            decision=decision,
            reason=reason,
            next_node=next_node,
            started=started,
            error_class="validation" if invalid else None,
            # The attempt is part of the identity: `retry_extract` re-enters extraction
            # with wider context on purpose, and that is different work under the same
            # stage name rather than a replay of the first try.
            input_hash=stage_input_hash(
                "validate_citations", state["document_id"], attempt
            ),
            output_hash=stage_input_hash(sorted(c.fingerprint for c in valid), len(invalid)),
        )
        return Command(
            update={"fact_candidates": [_candidate_to_dict(c) for c in valid + invalid]},
            goto=next_node,
        )

    async def retry_extract(
        state: GraphState,
    ) -> Command[Literal["extract_facts"]]:
        started = perf_counter()
        # Bumping the counter first makes extract_facts re-ask with wider context,
        # and caps the loop at settings.max_validation_repairs.
        attempt = state.get("validation_attempt", 0) + 1
        await _event(
            deps,
            state,
            stage="retry_extract",
            decision="retry",
            reason="bounded citation-repair attempt with wider context",
            next_node="extract_facts",
            started=started,
            error_class="validation",
        )
        return Command(update={"validation_attempt": attempt}, goto="extract_facts")

    async def mark_unsupported(
        state: GraphState,
    ) -> Command[Literal["apply_source_rules"]]:
        started = perf_counter()
        candidates = [_candidate_from_dict(c) for c in state.get("fact_candidates", [])]
        valid = [candidate for candidate in candidates if candidate.supported]
        invalid = [candidate for candidate in candidates if not candidate.supported]
        await deps.repository.put_facts(UUID(state["collection_id"]), valid)
        await _event(
            deps,
            state,
            stage="mark_unsupported",
            decision="abstain",
            reason=f"retained {len(invalid)} unsupported candidates for audit; none can commit",
            next_node="apply_source_rules",
            started=started,
            input_hash=stage_input_hash("mark_unsupported", state["document_id"]),
            output_hash=stage_input_hash(sorted(c.fingerprint for c in valid)),
        )
        return Command(update={"unsupported_count": len(invalid)}, goto="apply_source_rules")

    async def diff_against_register(
        state: GraphState,
    ) -> Command[Literal["route_source_findings", "detect_conflicts"]]:
        started = perf_counter()
        supported = [
            _candidate_from_dict(c)
            for c in state.get("fact_candidates", [])
            if c.get("supported")
        ]
        collection_id = UUID(state["collection_id"])
        # A register row is (agreement, obligation), so what this document touches is a
        # set of scoped keys, not bare ones: a term of Alpha's leaves Beta's row alone.
        named = tuple(sorted(await deps.repository.documents_by_agreement_ref(collection_id)))
        fact_keys = sorted(
            {
                RegisterKey(
                    _resolve_agreement(named, candidate.scope.agreement_id)[0], candidate.key
                ).text
                for candidate in supported
            }
        )
        # New scoped keys plus, through the reverse citation index, any register row
        # still grounded in a document this one supersedes.
        affected_keys = await deps.repository.affected_register_keys(
            collection_id, fact_keys, UUID(state["document_id"])
        )
        register_before = _register_before(
            await deps.repository.get_register_items(collection_id, affected_keys)
        )
        inherited = sorted(set(affected_keys) - set(fact_keys))
        # No affected key means no derivation and no proposal, but the source stage has
        # already run by this point and may be holding a violation. Route it rather than
        # dropping straight to the report.
        next_node = "detect_conflicts" if affected_keys else "route_source_findings"
        await _event(
            deps,
            state,
            stage="diff_against_register",
            decision="continue" if affected_keys else "skip",
            reason=(
                f"affected key set contains {len(affected_keys)} keys "
                f"({len(fact_keys)} from new facts, {len(inherited)} from citation invalidation); "
                f"{len(register_before)} existing items read"
            ),
            next_node=next_node,
            started=started,
        )
        return Command(
            update={"affected_keys": affected_keys, "register_before": register_before},
            goto=next_node,
        )

    async def detect_conflicts(state: GraphState) -> Command[Literal["assemble_proposals"]]:
        started = perf_counter()
        collection_id = UUID(state["collection_id"])
        document_id = UUID(state["document_id"])
        affected_keys = state.get("affected_keys", [])

        scoped_keys = [RegisterKey.parse(entry) for entry in affected_keys]
        named = tuple(sorted(await deps.repository.documents_by_agreement_ref(collection_id)))
        active = await deps.repository.get_active_facts(
            collection_id, sorted({scoped.key for scoped in scoped_keys})
        )
        # Partitioned by the row each fact belongs to, so Alpha's payment term and Beta's
        # are derived separately and never see each other. Comparability within a row is
        # still `FactScope.comparable_to`'s job: an invoice issued under Alpha lands in
        # Alpha's partition and is weighed there as operational evidence.
        by_row: dict[tuple[str, str], list[Any]] = {}
        for fact in active:
            bucket, _ = _resolve_agreement(named, fact.scope.agreement_id)
            by_row.setdefault((bucket, fact.key), []).append(fact)

        document_evidence = state.get("supersession_evidence")
        text = state["input_document"]["text"]
        derivations: list[Derivation] = []
        conflicts: list[Conflict] = []
        for scoped in scoped_keys:
            key_facts = by_row.get((scoped.agreement_id, scoped.key), [])
            evidence = None
            if document_evidence:
                # Prefer the sentence carrying this key's own quote; fall back to the
                # document-level claim so a term named elsewhere is still covered.
                from_new = [fact for fact in key_facts if fact.document_id == document_id]
                evidence = (
                    supersession_evidence_near(text, from_new[-1].quote) if from_new else None
                ) or document_evidence
            derivation = derive_key(
                scoped.key,
                key_facts,
                new_document_id=document_id,
                supersession_evidence=evidence,
                agreement_id=scoped.agreement_id,
                # Only the unnamed bucket can be ambiguous, and only when there was more
                # than one agreement it could have meant.
                ambiguous_among=named if not scoped.agreement_id else (),
            )
            derivations.append(derivation)
            if derivation.conflict is not None:
                conflicts.append(
                    Conflict(
                        collection_id=collection_id,
                        # Scoped: two agreements may each hold an open conflict on the
                        # same obligation, and a bare key cannot tell them apart.
                        key=scoped.text,
                        fact_a_id=derivation.conflict.fact_a_id,
                        fact_b_id=derivation.conflict.fact_b_id,
                        kind=derivation.conflict.kind,
                        rationale=derivation.conflict.rationale,
                        detected_run=UUID(state["run_id"]),
                    )
                )

        stored = await deps.repository.put_conflicts(conflicts)
        conflict_ids = {conflict.key: str(conflict.id) for conflict in stored}
        payloads = []
        for derivation in derivations:
            payload = _derivation_to_dict(derivation)
            if payload["conflict"] is not None:
                payload["conflict"]["id"] = conflict_ids[payload["scoped_key"]]
            payloads.append(payload)

        kinds = Counter(conflict.kind for conflict in stored)
        contradictions = kinds["contradiction"]
        await _event(
            deps,
            state,
            stage="detect_conflicts",
            decision="escalate" if stored else "continue",
            reason=(
                f"derived {len(derivations)} agreement-scoped keys; "
                f"{contradictions} contradiction(s), "
                f"{kinds['supersession_candidate']} supersession proposal(s) and "
                f"{kinds['ambiguous_scope']} unresolved agreement scope(s) "
                "opened for human decision"
            ),
            next_node="assemble_proposals",
            started=started,
            error_class="policy" if contradictions else None,
            input_hash=stage_input_hash(
                "detect_conflicts", state["document_id"], affected_keys
            ),
            output_hash=stage_input_hash(sorted(conflict_ids.values())),
        )
        return Command(
            update={"derivations": payloads, "conflict_ids": list(conflict_ids.values())},
            goto="assemble_proposals",
        )

    async def _evaluate_stage(
        state: GraphState,
        *,
        stage: str,
        next_node: str,
        targets: list[RuleTarget],
    ) -> Command[Any]:
        """Evaluate one rule stage and write an explicit row per (rule, target).

        Each pair gets its own bounded slice of evidence, so cost scales with the rules
        and the affected keys rather than with the size of the corpus. The stage also
        contributes its own denominator: how many pairs were supposed to run and how many
        verdicts came back. A count without that denominator is not a result.
        """
        started = perf_counter()
        run_id = UUID(state["run_id"])
        stage_scope = "source" if stage == "apply_source_rules" else "deliverable"
        # The pinned ruleset, not whatever is active now: an upload landing mid-run must
        # not judge one document against two playbooks.
        ruleset = (
            await deps.repository.get_ruleset(UUID(state["ruleset_id"]))
            if state.get("ruleset_id")
            else None
        )
        rules = rules_for(ruleset, stage_scope) if ruleset else []

        pairs = [(rule, target) for rule in rules for target in targets if target.applies(rule)]
        if not pairs:
            await _event(
                deps,
                state,
                stage=stage,
                decision="skip",
                reason=(
                    "no playbook pinned for this run"
                    if ruleset is None
                    else f"ruleset {ruleset.name} v{ruleset.version} has no {stage_scope} rule "
                    f"matching the {len(targets)} target(s) of this run"
                ),
                next_node=next_node,
                started=started,
            )
            # Nothing to expect and nothing completed. The report's `clean` flag needs
            # `rules_expected > 0`, so a collection with no playbook cannot read as clean.
            return Command(goto=next_node)

        findings: list[Finding] = []
        tokens_in = tokens_out = 0
        for rule, target in pairs:
            assert rule.id is not None  # assigned when the ruleset was stored
            excerpts = target.excerpts(rule)
            try:
                verdict = await deps.model.evaluate_rule(
                    rule,
                    target_label=target.label,
                    excerpts=excerpts,
                    statement=target.statement,
                )
            except Exception:
                # An outage is not evidence. `insufficient_evidence` is a judgement -
                # the model read the target and found nothing to weigh - so recording it
                # here would make an unreachable server indistinguishable from a silent
                # contract. Fail the run instead, writing nothing: the checkpoint holds,
                # so re-invoking resumes at this node and re-evaluates every pair.
                await _event(
                    deps,
                    state,
                    stage=stage,
                    decision="fail",
                    reason=(
                        f"{stage_scope} rule {rule.code} returned no verdict for "
                        f"{target.label}; {len(findings)} of {len(pairs)} evaluations had "
                        "completed, none recorded"
                    ),
                    next_node=stage,
                    started=started,
                    error_class="transient",
                    usage={"tokens_in": tokens_in, "tokens_out": tokens_out},
                    model=_model_name(deps),
                )
                raise
            usage = _model_usage(deps)
            tokens_in += usage.get("tokens_in", 0)
            tokens_out += usage.get("tokens_out", 0)
            decision, rationale, citations = ground_verdict(verdict, excerpts)
            findings.append(
                Finding(
                    run_id=run_id,
                    rule_id=rule.id,
                    rule_code=rule.code,
                    target_kind=target.kind,
                    target_id=target.id,
                    target_key=target.key,
                    system_verdict=decision,
                    rationale=rationale,
                    severity=rule.severity,
                    citations=citations,
                )
            )

        stored = await deps.repository.put_findings(findings)
        counts = Counter(finding.system_verdict for finding in stored)
        # Pairs that were expected but produced no persisted row. Zero on every path today
        # - the loop above re-raises rather than skipping one - so this catches a
        # repository that silently drops rows, not a model that misbehaves.
        missing = len(pairs) - len(stored)
        await _event(
            deps,
            state,
            stage=stage,
            decision="escalate" if counts["violation"] else "continue",
            reason=(
                f"{len(stored)} of {len(pairs)} {stage_scope} evaluations "
                f"({len(rules)} rule(s) x {len(targets)} target(s)): "
                f"{counts['violation']} violation, {counts['pass']} pass, "
                f"{counts['insufficient_evidence']} insufficient evidence"
            ),
            next_node=next_node,
            started=started,
            error_class="policy" if counts["violation"] else None,
            usage={"tokens_in": tokens_in, "tokens_out": tokens_out},
            model=_model_name(deps),
            input_hash=stage_input_hash(
                stage,
                state.get("ruleset_hash"),
                sorted(f"{target.kind}:{target.key}" for target in targets),
            ),
            output_hash=stage_input_hash(
                sorted(f"{f.rule_code}:{f.target_key}:{f.system_verdict}" for f in stored)
            ),
        )
        return Command(
            update={
                "findings": state.get("findings", []) + [_finding_to_dict(f) for f in stored],
                "rules_expected": state.get("rules_expected", 0) + len(pairs),
                "rules_completed": state.get("rules_completed", 0) + len(stored),
                "rules_failed": state.get("rules_failed", 0) + missing,
            },
            goto=next_node,
        )

    async def apply_source_rules(
        state: GraphState,
    ) -> Command[
        Literal["diff_against_register", "detect_conflicts", "route_source_findings"]
    ]:
        """Judge the document itself, for every document that reached this far.

        This used to sit after derivation, so two documents never got here: one that
        changed no obligation key, and a re-upload, which stopped at the duplicate
        short-circuit. Both read as "no findings" in the report, which is the same shape
        as "the playbook found nothing wrong". The stage is now on every path.

        Running it twice over the same bytes is waste, not correctness, so it is a cache
        rather than a skip: an exact match on (document, pinned playbook, evaluator)
        reuses the verdicts that match ran; anything else re-evaluates and records its own.
        """
        started = perf_counter()
        run_id = UUID(state["run_id"])
        document_id = UUID(state["document_id"])
        next_node = _after_source_rules(state)
        cache_key = source_rule_cache_key(
            collection_id=UUID(state["collection_id"]),
            document_sha256=state["document_sha256"],
            ruleset_hash=state.get("ruleset_hash"),
            evaluator_version=SOURCE_RULE_EVALUATOR_VERSION,
        )

        cached_run = await deps.repository.get_source_rule_cache(cache_key)
        # `cached_run == run_id` is this node replaying after a crash, not a hit: the rows
        # it would copy are its own, and put_findings already makes the replay idempotent.
        if cached_run is not None and cached_run != run_id:
            reused = [
                finding
                for finding in await deps.repository.list_findings(cached_run)
                if finding.target_kind == "document"
            ]
            stored = await deps.repository.put_findings(
                [
                    # `proposed` again: a decision a human made on the earlier run was
                    # about that run's proposals, and does not carry to this one.
                    replace(
                        finding,
                        id=None,
                        run_id=run_id,
                        review_decision="pending",
                        decided_by=None,
                        citations=list(finding.citations),
                    )
                    for finding in reused
                ]
            )
            await _event(
                deps,
                state,
                stage="apply_source_rules",
                decision="reuse",
                reason=(
                    f"reused {len(stored)} source verdict(s) from run {cached_run}: "
                    f"same document, same pinned playbook, evaluator "
                    f"{SOURCE_RULE_EVALUATOR_VERSION}"
                ),
                next_node=next_node,
                started=started,
                cache_hit=True,
            )
            return Command(
                update={
                    "findings": state.get("findings", [])
                    + [_finding_to_dict(finding) for finding in stored],
                    "rules_expected": state.get("rules_expected", 0) + len(stored),
                    "rules_completed": state.get("rules_completed", 0) + len(stored),
                },
                goto=next_node,
            )

        blocks = sorted(
            (await deps.repository.get_blocks(document_id)).values(), key=lambda b: b.index
        )
        filename = state["input_document"]["filename"]
        command = await _evaluate_stage(
            state,
            stage="apply_source_rules",
            next_node=next_node,
            # One target: the document. Each rule still gets its own bounded slice of it.
            targets=[
                RuleTarget(
                    kind="document",
                    id=document_id,
                    key=filename,
                    label=f"source document {filename}",
                    excerpts=lambda rule: select_excerpts(
                        rule,
                        blocks,
                        max_blocks=settings.rule_context_blocks,
                        max_chars=settings.rule_context_chars,
                    ),
                )
            ],
        )
        # Only a stage that actually evaluated something is worth pointing a cache key at.
        # A skip (no playbook, or no source rule matching) recorded no verdict to reuse.
        if command.update:
            await deps.repository.put_source_rule_cache(
                cache_key,
                collection_id=UUID(state["collection_id"]),
                run_id=run_id,
                document_sha256=state["document_sha256"],
                ruleset_hash=state.get("ruleset_hash") or "",
                evaluator_version=SOURCE_RULE_EVALUATOR_VERSION,
            )
        return command

    async def route_source_findings(
        state: GraphState,
    ) -> Command[Literal["assemble_proposals", "snapshot_diff_report"]]:
        """Hand source findings to a human on the paths that derive nothing.

        A duplicate upload and a document that moves no register key both used to end at
        the report. Now that the source stage runs for them, a violation they turned up
        needs the same gate every other violation gets - otherwise the finding is
        recorded and nobody is ever asked about it, which is a bypass with extra steps.
        """
        started = perf_counter()
        pending = _unresolved_source_findings(state)
        next_node = "assemble_proposals" if pending else "snapshot_diff_report"
        await _event(
            deps,
            state,
            stage="route_source_findings",
            decision="escalate" if pending else "skip",
            reason=(
                f"{len(pending)} source finding(s) need a decision although this run "
                "derives no register key"
                if pending
                else "no register key affected and no adverse source verdict to decide"
            ),
            next_node=next_node,
            started=started,
            error_class="policy" if pending else None,
        )
        return Command(goto=next_node)

    async def assemble_proposals(state: GraphState) -> Command[Literal["await_review"]]:
        started = perf_counter()
        register_before = state.get("register_before", {})
        review_items: list[ReviewItem] = []
        unchanged: list[str] = []

        for derivation in state.get("derivations", []):
            key = derivation["scoped_key"]
            if derivation["state"] == "ambiguous":
                # A question, not a proposal. It carries no `after`, so neither commit
                # path can write it: an unresolved agreement must not put a contractual
                # value into a row, and approving the question is not approving a value.
                # What a human resolves it with is a re-upload naming the agreement.
                review_items.append(
                    ReviewItem(
                        run_id=UUID(state["run_id"]),
                        kind="scope_question",
                        target_key=key,
                        payload={
                            "document_id": state["document_id"],
                            "conflict": derivation["conflict"],
                            "reason": derivation["reason"],
                            "citation_fact_ids": derivation["citation_fact_ids"],
                            "force_review": True,
                        },
                    )
                )
                continue
            if derivation["value"] is None:
                continue
            before = register_before.get(key)
            after_hash = register_content_hash(
                value=derivation["value"],
                evidence_fingerprints=derivation["citation_fingerprints"],
                state=derivation["state"],
            )
            if before is not None and before["content_hash"] == after_hash:
                # Re-deriving produced the stored item byte for byte: no proposal,
                # no version bump, nothing for a human to decide.
                unchanged.append(key)
                continue

            review_items.append(
                ReviewItem(
                    run_id=UUID(state["run_id"]),
                    kind="conflict" if derivation["conflict"] else "register_update",
                    target_key=key,
                    payload={
                        "before": before,
                        "document_id": state["document_id"],
                        "after": {
                            "value": derivation["value"],
                            "state": derivation["state"],
                            "citation_fact_ids": derivation["citation_fact_ids"],
                            "citation_fingerprints": derivation["citation_fingerprints"],
                            "content_hash": after_hash,
                        },
                        "conflict": derivation["conflict"],
                        "reason": derivation["reason"],
                        # Always true. Detection is telemetry, so a clean scan must not buy a
                        # proposal a softer path than a flagged one -- there is no route
                        # in this graph where a human is optional, and making that
                        # conditional on a regex would make the regex the boundary.
                        "force_review": True,
                        "injection_flag": state.get("injection_flag", False),
                    },
                )
            )

        # One item per quarantined block. Withholding it from extraction keeps it out of
        # the register; this is what keeps it from also being invisible. A reviewer sees
        # the signals, the matched phrase, and the text as the model would have read it --
        # which for a hidden payload is not the text the page renders, and that difference
        # is the whole reason nobody noticed.
        for entry in state.get("quarantined_blocks", []):
            review_items.append(
                ReviewItem(
                    run_id=UUID(state["run_id"]),
                    kind="injection_review",
                    target_key=f"block {entry['index']}",
                    payload={
                        "document_id": state["document_id"],
                        "block_id": entry["block_id"],
                        "page": entry["page"],
                        "extraction_method": entry["extraction_method"],
                        "signals": entry["signals"],
                        "quotes": entry["quotes"],
                        "as_the_model_would_read_it": entry["normalised"],
                        "effect": (
                            "excluded from extraction, from rule context, and from every "
                            "register update; the rest of the document was processed"
                        ),
                        "force_review": True,
                    },
                )
            )

        # Source-stage findings only; the deliverable stage has not run yet. A pass needs
        # no decision, a violation or an unresolved rule does.
        for finding in state.get("findings", []):
            if finding["system_verdict"] == "pass" or finding["target_kind"] != "document":
                continue
            review_items.append(
                ReviewItem(
                    run_id=UUID(state["run_id"]),
                    kind="finding",
                    target_key=finding["rule_code"],
                    payload={
                        "finding_id": finding["id"],
                        "system_verdict": finding["system_verdict"],
                        "rationale": finding["rationale"],
                        "severity": finding["severity"],
                        "target_kind": finding["target_kind"],
                        "target_key": finding["target_key"],
                        "citations": finding["citations"],
                        # Always true. Detection is telemetry, so a clean scan must not buy a
                        # proposal a softer path than a flagged one -- there is no route
                        # in this graph where a human is optional, and making that
                        # conditional on a regex would make the regex the boundary.
                        "force_review": True,
                        "injection_flag": state.get("injection_flag", False),
                    },
                )
            )

        await deps.repository.add_review_items(review_items)
        by_kind = Counter(item.kind for item in review_items)
        await _event(
            deps,
            state,
            stage="assemble_proposals",
            decision="continue" if review_items else "skip",
            reason=(
                f"created {len(review_items)} independent review items "
                f"({by_kind['conflict']} conflict, {by_kind['finding']} finding, "
                f"{by_kind['scope_question']} unresolved agreement scope, "
                f"{by_kind['injection_review']} quarantined block, "
                f"{by_kind['register_update']} update); "
                f"{len(unchanged)} affected keys re-derived unchanged"
            ),
            next_node="await_review",
            started=started,
            input_hash=stage_input_hash(
                "assemble_proposals",
                [d["scoped_key"] for d in state.get("derivations", [])],
                [f["id"] for f in state.get("findings", [])],
            ),
            output_hash=stage_input_hash(sorted(str(item.id) for item in review_items)),
        )
        return Command(
            update={
                "review_item_ids": [str(item.id) for item in review_items],
                "unchanged_keys": unchanged,
            },
            goto="await_review",
        )

    async def await_review(state: GraphState) -> Command[Literal["build_candidate_register"]]:
        # No side effect before interrupt: the node restarts from the beginning on resume.
        decision_payload = interrupt(
            {
                "kind": "item_level_review",
                "run_id": state["run_id"],
                "review_item_ids": state.get("review_item_ids", []),
                "required_shape": {
                    "decisions": {"review_item_uuid": "approved|rejected"},
                    "actor_id": "stamped from the reviewer's credential, not the caller",
                },
            }
        )
        started = perf_counter()
        # Refuses a payload that did not come from an authenticated reviewer, so an
        # item cannot leave `pending` on the word of whoever reached the graph.
        actor_id = reviewer_from_payload(decision_payload).actor_id
        decisions = {UUID(key): value for key, value in decision_payload["decisions"].items()}
        decided = await deps.repository.decide_review_items(
            UUID(state["run_id"]), actor_id, decisions
        )
        approved = [str(item.id) for item in decided if item.state == "approved"]
        rejected = [str(item.id) for item in decided if item.state == "rejected"]
        rechecked, findings = await _record_finding_decisions(state, actor_id, decided)
        await _event(
            deps,
            state,
            stage="await_review",
            decision="human_decision",
            reason=(
                f"approved {len(approved)} and rejected {len(rejected)} items"
                + (
                    f"; {len(rechecked)} dismissed source finding(s) flagged for recheck "
                    f"({', '.join(rechecked)})"
                    if rechecked
                    else ""
                )
            ),
            next_node="build_candidate_register",
            started=started,
            input_hash=stage_input_hash("await_review", sorted(str(k) for k in decisions)),
            output_hash=stage_input_hash(sorted(approved), sorted(rejected), actor_id),
        )
        return Command(
            update={
                "actor_id": actor_id,
                "approved_review_item_ids": approved,
                "rejected_review_item_ids": rejected,
                "findings": findings,
            },
            goto="build_candidate_register",
        )

    async def build_candidate_register(
        state: GraphState,
    ) -> Command[Literal["apply_deliverable_rules"]]:
        """Assemble the register as it will stand if this run commits.

        Stored items overlaid with the proposals a human just approved. A rejected
        proposal is simply absent, so the deliverable stage judges the deliverable
        rather than the request. Nothing is written here; the commit is still ahead.
        """
        started = perf_counter()
        rows, applied = await _candidate_rows(state)
        basis = candidate_basis_hash(rows, ruleset_hash=state.get("ruleset_hash"))
        await _event(
            deps,
            state,
            stage="build_candidate_register",
            decision="continue",
            reason=(
                f"candidate register holds {len(rows)} keys "
                f"({applied} of them from this run's approvals); "
                f"basis sha256:{basis[:12]}"
            ),
            next_node="apply_deliverable_rules",
            started=started,
        )
        return Command(
            update={"candidate_register": rows, "candidate_basis_hash": basis},
            goto="apply_deliverable_rules",
        )

    async def _record_finding_decisions(
        state: GraphState, actor_id: str, decided: list[ReviewItem]
    ) -> tuple[list[str], list[dict[str, Any]]]:
        """Write the human decision beside each finding's verdict, and never over it.

        Recorded here rather than at commit, because a run that never commits -- blocked,
        refused as stale, abandoned -- still made decisions, and losing them is losing the
        audit trail of exactly the runs that most need one.

        Returns the rule codes whose dismissal flags a recheck. A dismissed source finding
        also drops this document's source-rule cache entry: the cache exists so the same
        bytes are not re-judged for free, and leaving a dismissal in it would serve one
        reviewer's call back to every future upload as if it were the playbook's answer.
        """
        snapshot = state.get("findings", [])
        decisions = _finding_decisions(decided)
        if not decisions:
            return [], snapshot
        findings = await deps.repository.record_finding_decisions(
            UUID(state["run_id"]), actor_id, decisions
        )
        # The report reads the run's own snapshot, so a decision that only reached storage
        # would be invisible in the very document that is supposed to disclose it.
        recorded = {
            str(finding.id): {
                "review_decision": finding.review_decision,
                "decided_by": finding.decided_by,
                "recheck_required": finding.recheck_required,
            }
            for finding in findings
            if finding.id in decisions
        }
        snapshot = [{**row, **recorded.get(row["id"], {})} for row in snapshot]
        rechecked = [
            finding.rule_code
            for finding in findings
            if finding.recheck_required and finding.id in decisions
        ]
        if rechecked and state.get("document_sha256"):
            await deps.repository.drop_source_rule_cache(
                source_rule_cache_key(
                    collection_id=UUID(state["collection_id"]),
                    document_sha256=state["document_sha256"],
                    ruleset_hash=state.get("ruleset_hash"),
                    evaluator_version=SOURCE_RULE_EVALUATOR_VERSION,
                )
            )
        return sorted(set(rechecked)), snapshot

    async def _candidate_rows(state: GraphState) -> tuple[list[dict[str, Any]], int]:
        """The register as it would stand if this run committed, plus how much is this run.

        Read fresh from storage every time it is called. That is the point: the same
        function produces what the deliverable rules judged and, later, what is actually
        there when the commit is about to happen. If the two disagree, someone else
        committed in between and the decisions a human made were about a register that no
        longer exists.
        """
        collection_id = UUID(state["collection_id"])
        candidate = {
            item.register_key.text: {
                "key": item.key,
                "agreement_id": item.agreement_id,
                "scoped_key": item.register_key.text,
                "value": item.value,
                "state": item.state,
                "version": item.version,
                "citation_fact_ids": [str(fact_id) for fact_id in item.citation_fact_ids],
            }
            for item in await deps.repository.list_register(collection_id)
        }
        applied = 0
        for item in await deps.repository.list_review_items(UUID(state["run_id"])):
            if item.state != "approved" or item.kind not in {"register_update", "conflict"}:
                continue
            after = item.payload["after"]
            scoped = RegisterKey.parse(item.target_key)
            stored = candidate.get(scoped.text)
            candidate[scoped.text] = {
                "key": scoped.key,
                "agreement_id": scoped.agreement_id,
                "scoped_key": scoped.text,
                "value": after["value"],
                "state": after.get("state", "supported"),
                # The version this proposal would produce. Binding on it is what makes a
                # concurrent commit visible: the row this run is about to write is not the
                # row it was judged against.
                "version": (stored["version"] + 1) if stored else 1,
                "citation_fact_ids": after.get("citation_fact_ids", []),
            }
            applied += 1
        return [candidate[key] for key in sorted(candidate)], applied

    async def apply_deliverable_rules(state: GraphState) -> Command[Literal["assemble_findings"]]:
        """Evaluate the candidate register one agreement-scoped key at a time.

        Judging the whole register as a single blob made every verdict share one row, so a
        violation could not name which obligation it was about, and the evidence for one
        key was diluted by every other key in the prompt. Keying by obligation alone had
        the same problem one level up: "payment_due_days violates NET-30" did not say
        whose contract, and with two agreements in a collection that is the only part
        anyone needs. A target is now one agreement's copy of one key.

        A rule that names `keys` in the playbook is evaluated only against those keys, in
        each agreement that holds them. A rule that names none is about the register as a
        whole, and gets an aggregate verdict of its own on top -- see `_aggregate_wide`.
        """
        collection_id = UUID(state["collection_id"])
        rows = {row["scoped_key"]: row for row in state.get("candidate_register", [])}
        # Only rows this run touched. Re-judging state nobody changed would reopen settled
        # questions on every upload -- and, now that rows are scoped, would drag every
        # other agreement in the collection back through the playbook on each upload.
        affected = [scoped for scoped in state.get("affected_keys", []) if scoped in rows]
        facts = await deps.repository.get_active_facts(
            collection_id, sorted({RegisterKey.parse(scoped).key for scoped in affected})
        )
        cited = {fact.id: fact for fact in facts}
        blocks = await deps.repository.get_blocks_by_ids(
            [fact.block_id for fact in facts if fact.block_id is not None]
        )

        def target_for(scoped_text: str) -> RuleTarget:
            scoped = RegisterKey.parse(scoped_text)
            return RuleTarget(
                kind="register_item",
                # A candidate register item has no row yet, so its id is derived from the
                # collection and the scoped key. Replay-safe: one finding per rule per
                # agreement per key per run.
                id=target_uuid("register_item", f"{state['collection_id']}:{scoped_text}"),
                key=scoped_text,
                label=f"proposed register value for {scoped.label()}",
                excerpts=lambda rule, s=scoped_text: _register_excerpts(rows[s], cited, blocks),
                statement=_register_statement(rows[scoped_text]),
                # The playbook names obligations, not agreements: NET-30 is a rule about
                # payment terms wherever they appear.
                applies=lambda rule, k=scoped.key: not rule.target_keys or k in rule.target_keys,
            )

        command = await _evaluate_stage(
            state,
            stage="apply_deliverable_rules",
            next_node="assemble_findings",
            targets=[target_for(scoped) for scoped in affected],
        )
        return await _aggregate_wide(state, command)

    async def _aggregate_wide(state: GraphState, command: Command[Any]) -> Command[Any]:
        """One explicit collection-level verdict per rule that is not about a named key.

        A rule with no `target_keys` is a statement about the register as a whole, and
        per-agreement rows alone leave "does the collection satisfy this rule" as
        something a reader has to work out by scanning. The aggregate is derived from
        those rows rather than re-asked of the model -- an aggregate that could disagree
        with its own parts is worse than none, and it would cost a second pass to get it.

        Rules that name keys get no aggregate row: their answer is already per-key, and
        an "aggregate" over one obligation is the same verdict written twice.
        """
        if not command.update or not state.get("ruleset_id"):
            return command
        ruleset = await deps.repository.get_ruleset(UUID(state["ruleset_id"]))
        if ruleset is None:
            return command
        wide = [rule for rule in rules_for(ruleset, "deliverable") if not rule.target_keys]
        if not wide:
            return command

        collection_id = state["collection_id"]
        rows = [
            finding
            for finding in command.update["findings"]
            if finding["target_kind"] == "register_item"
        ]
        aggregates: list[Finding] = []
        for rule in wide:
            assert rule.id is not None
            judged = [finding for finding in rows if finding["rule_code"] == rule.code]
            if not judged:
                # The rule ran against nothing this time. An aggregate here would be a
                # pass nobody earned, which is the silent-clean shape all over again.
                continue
            verdicts = {finding["system_verdict"] for finding in judged}
            verdict = next(
                (v for v in ("violation", "insufficient_evidence") if v in verdicts), "pass"
            )
            adverse = sorted(
                finding["target_key"] for finding in judged if finding["system_verdict"] != "pass"
            )
            aggregates.append(
                Finding(
                    run_id=UUID(state["run_id"]),
                    rule_id=rule.id,
                    rule_code=rule.code,
                    target_kind="register",
                    target_id=target_uuid("register", f"{collection_id}:{rule.code}"),
                    target_key="collection",
                    system_verdict=verdict,
                    rationale=(
                        f"aggregate of {len(judged)} agreement-scoped evaluation(s): "
                        + (
                            f"adverse on {', '.join(adverse)}"
                            if adverse
                            else "every scoped register item passed"
                        )
                    ),
                    severity=rule.severity,
                )
            )

        stored = await deps.repository.put_findings(aggregates)
        update = dict(command.update)
        update["findings"] = update["findings"] + [_finding_to_dict(f) for f in stored]
        update["rules_expected"] = update["rules_expected"] + len(aggregates)
        update["rules_completed"] = update["rules_completed"] + len(stored)
        return Command(update=update, goto=command.goto)

    async def assemble_findings(
        state: GraphState,
    ) -> Command[Literal["await_finding_review", "enforce_blockers"]]:
        """Gate 2 opens for every deliverable evaluation, adverse or not.

        It used to open only when something was wrong, so the runs nobody looked at were
        exactly the runs that reported themselves clean. "No adverse findings" is a claim
        about the deliverable, and a claim that reaches a report with no human attached to
        it is the same silent pass this pipeline exists to refuse. When there is nothing
        adverse, the gate asks for one thing instead: a confirmation, signed, bound to the
        register and the playbook it is a confirmation of.
        """
        started = perf_counter()
        binding = _decision_binding(state)
        # `register_item` only. A collection-wide rule's aggregate row is derived from
        # these same verdicts, so escalating it too would ask a human the same question
        # twice under two names.
        adverse = [
            finding
            for finding in state.get("findings", [])
            if finding["target_kind"] == "register_item" and finding["system_verdict"] != "pass"
        ]
        review_items = [
            ReviewItem(
                run_id=UUID(state["run_id"]),
                kind="finding",
                target_key=finding["rule_code"],
                payload={
                    "finding_id": finding["id"],
                    "system_verdict": finding["system_verdict"],
                    "rationale": finding["rationale"],
                    "severity": finding["severity"],
                    "target_kind": finding["target_kind"],
                    # What the rule judged: the register key here, a filename at the
                    # source stage. The source items already carry it; a reviewer
                    # reading a deliverable item needs it just as much.
                    "target_key": finding["target_key"],
                    "citations": finding["citations"],
                    # Always true. Detection is telemetry, so a clean scan must not buy a
                        # proposal a softer path than a flagged one -- there is no route
                        # in this graph where a human is optional, and making that
                        # conditional on a regex would make the regex the boundary.
                        "force_review": True,
                        "injection_flag": state.get("injection_flag", False),
                    **binding,
                },
            )
            for finding in adverse
        ]
        judged = state.get("findings", [])
        if not judged:
            # No playbook, so no claim: `rules_expected` is zero, `clean` is already
            # unreachable, and there is nothing for a human to put their name to.
            await _event(
                deps,
                state,
                stage="assemble_findings",
                decision="skip",
                reason="no rule was evaluated in this run; there is no result to confirm",
                next_node="enforce_blockers",
                started=started,
            )
            return Command(goto="enforce_blockers")
        if not adverse:
            review_items.append(
                ReviewItem(
                    run_id=UUID(state["run_id"]),
                    kind="deliverable_confirmation",
                    target_key="no adverse findings",
                    payload={
                        # Everything the confirmation is a confirmation of, so approving it
                        # is a statement about a specific register judged by a specific
                        # playbook rather than a click on an empty screen.
                        "evaluations": [
                            {
                                "rule_code": finding["rule_code"],
                                "target_key": finding["target_key"],
                                "system_verdict": finding["system_verdict"],
                                "rationale": finding["rationale"],
                            }
                            for finding in judged
                        ],
                        "rules_expected": state.get("rules_expected", 0),
                        "rules_completed": state.get("rules_completed", 0),
                        "extraction_warnings": state.get("extraction_warnings", []),
                        # Always true. Detection is telemetry, so a clean scan must not buy a
                        # proposal a softer path than a flagged one -- there is no route
                        # in this graph where a human is optional, and making that
                        # conditional on a regex would make the regex the boundary.
                        "force_review": True,
                        "injection_flag": state.get("injection_flag", False),
                        **binding,
                    },
                )
            )
        await deps.repository.add_review_items(review_items)
        blockers = sum(1 for finding in adverse if finding["severity"] == "blocker")
        await _event(
            deps,
            state,
            stage="assemble_findings",
            decision="escalate" if adverse else "confirm",
            reason=(
                f"{len(adverse)} deliverable finding(s) need a decision ({blockers} blocker)"
                if adverse
                else f"{len(judged)} deliverable evaluation(s), none adverse; "
                "a human confirms the result rather than the run assuming it"
            ),
            next_node="await_finding_review",
            started=started,
            error_class="policy" if adverse else None,
            input_hash=stage_input_hash(
                "assemble_findings", state.get("candidate_basis_hash"),
                [f["id"] for f in judged],
            ),
            output_hash=stage_input_hash(sorted(str(item.id) for item in review_items)),
        )
        return Command(
            update={"finding_review_item_ids": [str(item.id) for item in review_items]},
            goto="await_finding_review",
        )

    async def await_finding_review(
        state: GraphState,
    ) -> Command[Literal["enforce_blockers", "snapshot_diff_report"]]:
        # No side effect before interrupt: the node restarts from the beginning on resume.
        decision_payload = interrupt(
            {
                "kind": "deliverable_finding_review",
                "run_id": state["run_id"],
                "review_item_ids": state.get("finding_review_item_ids", []),
                "required_shape": {
                    "decisions": {"review_item_uuid": "approved|rejected"},
                    "actor_id": "stamped from the reviewer's credential, not the caller",
                },
            }
        )
        started = perf_counter()
        actor_id = reviewer_from_payload(decision_payload).actor_id
        decisions = {UUID(key): value for key, value in decision_payload["decisions"].items()}
        pending = [
            item
            for item in await deps.repository.list_review_items(UUID(state["run_id"]))
            if str(item.id) in set(state.get("finding_review_item_ids", []))
        ]
        undecided = [str(item.id) for item in pending if item.id not in decisions]
        if undecided:
            # The gate is not advisory. Leaving an item out of the payload would let a
            # caller walk past a finding -- or past the confirmation itself -- by simply
            # not mentioning it, which is a bypass that leaves no trace anywhere.
            raise ValueError(
                f"every deliverable review item needs a decision; {len(undecided)} left "
                f"undecided: {', '.join(sorted(undecided))}"
            )
        decided = await deps.repository.decide_review_items(
            UUID(state["run_id"]), actor_id, decisions
        )
        approved = [str(item.id) for item in decided if item.state == "approved"]
        rejected = [str(item.id) for item in decided if item.state == "rejected"]
        rechecked, findings = await _record_finding_decisions(state, actor_id, decided)
        confirmation = next(
            (item for item in decided if item.kind == "deliverable_confirmation"), None
        )
        confirmed = confirmation is None or confirmation.state == "approved"
        update = {
            "actor_id": actor_id,
            "approved_review_item_ids": state.get("approved_review_item_ids", []) + approved,
            "rejected_review_item_ids": state.get("rejected_review_item_ids", []) + rejected,
            "findings": findings,
            # Who signed off on the deliverable. `_rules_summary` will not call a run
            # clean without it.
            "deliverable_review_by": actor_id,
            "deliverable_confirmed": confirmed,
        }
        if not confirmed:
            # Declining to confirm a no-adverse-findings result is a statement that the
            # evaluation itself is not trusted. Committing anyway would treat "I do not
            # accept this" as consent, so the run stops here with nothing written.
            await deps.repository.block_run(
                UUID(state["run_id"]), "deliverable confirmation was refused"
            )
            await _event(
                deps,
                state,
                stage="await_finding_review",
                decision="refuse",
                reason=(
                    f"{actor_id} declined to confirm the deliverable; nothing committed "
                    "and the run may not report a clean result"
                ),
                next_node="snapshot_diff_report",
                started=started,
                error_class="policy",
            )
            return Command(update=update, goto="snapshot_diff_report")
        await _event(
            deps,
            state,
            stage="await_finding_review",
            decision="human_decision",
            reason=(
                f"upheld {len(approved)} and dismissed {len(rejected)} deliverable items"
                + (f"; {len(rechecked)} flagged for recheck" if rechecked else "")
            ),
            next_node="enforce_blockers",
            started=started,
            input_hash=stage_input_hash(
                "await_finding_review", sorted(str(k) for k in decisions)
            ),
            output_hash=stage_input_hash(sorted(approved), sorted(rejected), actor_id),
        )
        return Command(update=update, goto="enforce_blockers")

    async def verify_review_binding(
        state: GraphState,
    ) -> Command[Literal["commit_approved", "snapshot_diff_report"]]:
        """Re-check that what the human agreed to is still what would be written.

        Every decision was stamped with the basis it was made against: the register rows,
        their versions, and the pinned playbook. Between the gate and the commit another
        run can land, and then the approval that is about to be applied was given over a
        register that no longer exists -- the reviewer read 30 days and the row now says
        45, or a rule they were judged under has been edited.

        The per-key staleness check at commit catches a key this run is itself writing.
        It cannot catch the rest: a deliverable verdict is a statement about the whole
        candidate register, so a row this run never touches moving underneath it still
        invalidates the verdict. Refusing here costs one re-run; not refusing writes a
        change nobody approved.
        """
        started = perf_counter()
        rows, _ = await _candidate_rows(state)
        active = await deps.repository.get_active_ruleset(UUID(state["collection_id"]))
        live_ruleset = ruleset_hash(active) if active else None
        expected = state.get("candidate_basis_hash")
        actual = candidate_basis_hash(rows, ruleset_hash=state.get("ruleset_hash"))

        drift: list[dict[str, Any]] = []
        if expected is not None and actual != expected:
            before = {row["scoped_key"]: row.get("version") for row in
                      state.get("candidate_register", [])}
            after = {row["scoped_key"]: row.get("version") for row in rows}
            drift += [
                {
                    "kind": "register_moved",
                    "key": key,
                    "reviewed_version": before.get(key),
                    "actual_version": after.get(key),
                }
                for key in sorted(set(before) | set(after))
                if before.get(key) != after.get(key)
            ] or [{"kind": "register_moved", "key": "(values changed)",
                   "reviewed_version": None, "actual_version": None}]
        if live_ruleset != state.get("ruleset_hash"):
            drift.append(
                {
                    "kind": "ruleset_changed",
                    "key": "(playbook)",
                    "reviewed_version": state.get("ruleset_hash"),
                    "actual_version": live_ruleset,
                }
            )

        if not drift:
            await _event(
                deps,
                state,
                stage="verify_review_binding",
                decision="continue",
                reason=(
                    f"decisions still bind: basis sha256:{actual[:12]}, "
                    f"playbook sha256:{(live_ruleset or 'none')[:12]}"
                ),
                next_node="commit_approved",
                started=started,
            )
            return Command(goto="commit_approved")

        await deps.repository.block_run(
            UUID(state["run_id"]), "review decisions no longer bind the current state"
        )
        await _event(
            deps,
            state,
            stage="verify_review_binding",
            decision="refuse",
            reason=(
                f"{len(drift)} change(s) since the human decided "
                f"({', '.join(entry['key'] for entry in drift)}); nothing committed, "
                "re-run against the register as it now stands"
            ),
            next_node="snapshot_diff_report",
            started=started,
            error_class="data",
        )
        return Command(update={"decisions_stale": drift}, goto="snapshot_diff_report")

    async def enforce_blockers(
        state: GraphState,
    ) -> Command[Literal["verify_review_binding", "snapshot_diff_report"]]:
        """A blocker a human upheld stops the commit.

        Approving a finding means "this problem is real", so it cannot also mean "commit
        anyway". Rejecting one means the human judged it not to apply, and the run
        continues. Getting past an upheld blocker takes a second, explicit act: remediate
        and re-run, or override with a reason that goes in the event log.
        """
        upheld = [
            item
            for item in await deps.repository.list_review_items(UUID(state["run_id"]))
            if item.kind == "finding"
            and item.state == "approved"
            and item.payload.get("severity") == "blocker"
        ]
        if not upheld:
            started = perf_counter()
            await _event(
                deps,
                state,
                stage="enforce_blockers",
                decision="continue",
                reason="no blocker finding was upheld",
                next_node="verify_review_binding",
                started=started,
            )
            return Command(goto="verify_review_binding")

        codes = sorted({item.target_key for item in upheld})
        # No side effect before interrupt: the node restarts from the beginning on resume.
        answer = interrupt(
            {
                "kind": "blocker_override",
                "run_id": state["run_id"],
                "blocked_by": codes,
                "findings": [
                    {
                        "rule_code": item.target_key,
                        "system_verdict": item.payload["system_verdict"],
                        "rationale": item.payload["rationale"],
                        "upheld_by": item.decided_by,
                    }
                    for item in upheld
                ],
                "required_shape": {
                    "override": "true to commit anyway, false to leave the run blocked",
                    "reason": "required when override is true; recorded in the event log",
                    "actor_id": "stamped from the reviewer's credential, not the caller",
                },
            }
        )
        started = perf_counter()
        # Checked before the answer is read at all: committing past an upheld blocker is
        # the single most consequential act in the system, so an unauthenticated answer
        # is not a "no override", it is an error.
        actor_id = reviewer_from_payload(answer).actor_id
        override = bool(answer.get("override"))
        reason = str(answer.get("reason") or "").strip()

        if override and not reason:
            # Refuse rather than downgrade to "no override": a stated intent to commit
            # past a blocker must leave a written justification or it did not happen.
            raise ValueError("overriding a blocker needs a reason")

        if not override:
            await deps.repository.block_run(
                UUID(state["run_id"]), f"blocker findings upheld: {', '.join(codes)}"
            )
            await _event(
                deps,
                state,
                stage="enforce_blockers",
                decision="block",
                reason=(
                    f"commit blocked: {len(upheld)} blocker finding(s) upheld "
                    f"({', '.join(codes)}); nothing written, remediate and re-run or override"
                ),
                next_node="snapshot_diff_report",
                started=started,
                error_class="policy",
            )
            return Command(
                update={"blocked_by": codes, "commit_blocked": True},
                goto="snapshot_diff_report",
            )

        await _event(
            deps,
            state,
            stage="enforce_blockers",
            decision="human_override",
            reason=f"{actor_id} overrode {', '.join(codes)}: {reason}",
            next_node="verify_review_binding",
            started=started,
            error_class="policy",
        )
        return Command(
            update={
                "blocked_by": codes,
                "commit_blocked": False,
                "override_reason": reason,
                "override_by": actor_id,
            },
            goto="verify_review_binding",
        )

    async def commit_approved(state: GraphState) -> Command[Literal["snapshot_diff_report"]]:
        """Write what a human approved, once.

        Idempotent on `(run_id, candidate_basis_hash)`: the repository ledgers the commit
        in the same transaction as the register writes, so a replay of this node -- a
        SIGKILL between the commit and LangGraph's checkpoint is the ordinary way it
        happens -- returns what was written instead of versioning every row a second time.
        """
        started = perf_counter()
        basis = state.get("candidate_basis_hash") or candidate_basis_hash(
            state.get("candidate_register", []), ruleset_hash=state.get("ruleset_hash")
        )
        result = await deps.repository.commit_approved(
            UUID(state["collection_id"]), UUID(state["run_id"]), basis_hash=basis
        )
        stale = result.stale
        reason = f"committed {len(result.committed)} approved register updates"
        if stale:
            # Refused, not applied: the register moved between derivation and commit, so
            # the human approved a change to a value that is no longer there. Re-run the
            # document to re-derive against what is stored now.
            reason += (
                f"; refused {len(stale)} stale proposal(s) "
                f"({', '.join(item.key for item in stale)}) whose register item changed "
                "under this run"
            )
        await _event(
            deps,
            state,
            stage="commit_approved",
            decision="partial_commit" if stale else "commit",
            reason=reason,
            next_node="snapshot_diff_report",
            started=started,
            error_class="data" if stale else None,
            # No ledger write here on purpose. This stage's row is written inside the
            # same transaction as the register, by the repository, which is the only
            # placement that makes "committed" and "recorded as committed" inseparable.
            # A second write from out here would count one execution twice.
        )
        return Command(
            update={
                "committed_keys": [item.register_key.text for item in result.committed],
                "stale_keys": [item.as_dict() for item in stale],
            },
            goto="snapshot_diff_report",
        )

    async def snapshot_diff_report(state: GraphState) -> dict[str, Any]:
        started = perf_counter()
        collection_id = UUID(state["collection_id"])
        register = await deps.repository.list_register(collection_id)
        events = await deps.repository.list_events(UUID(state["run_id"]))
        stage_ledger = await deps.repository.list_stages(UUID(state["run_id"]))
        open_conflicts = await deps.repository.list_conflicts(collection_id, state="open")
        register_before = state.get("register_before", {})
        hashes_after = {item.register_key.text: item.content_hash for item in register}
        # The register as a reader has to read it: one agreement at a time. A flat list of
        # `payment_due_days` rows with no agreement beside them is unreadable the moment a
        # collection holds two contracts, which is the case this whole change is about.
        by_agreement: dict[str, list[dict[str, Any]]] = {}
        for item in register:
            by_agreement.setdefault(item.agreement_id or "(no agreement named)", []).append(
                {
                    "key": item.key,
                    "value": item.value,
                    "state": item.state,
                    "version": item.version,
                    "content_hash": item.content_hash,
                }
            )
        preserved = sorted(
            key
            for key, before in register_before.items()
            if hashes_after.get(key) == before["content_hash"]
        )
        if state.get("duplicate_document"):
            status = "duplicate_noop"
        elif state.get("rederive_from_run") and not state.get("affected_keys"):
            # Nothing was outstanding by the time this run looked. Reporting "committed"
            # with an empty key list would read as a successful retry of something.
            status = "nothing_to_rederive"
        elif state.get("decisions_stale"):
            # A human approved a change to a register that moved under them. Nothing was
            # written, and calling it anything other than stale would hide that.
            status = "stale"
        elif state.get("commit_blocked"):
            status = "blocked"
        elif state.get("deliverable_confirmed") is False:
            status = "unconfirmed"
        elif state.get("stale_keys"):
            # Some approved proposal was refused. Calling this "committed" would hide
            # the fact that part of what a human approved is not in the register.
            status = "stale"
        else:
            status = "committed"
        report = {
            "status": status,
            "run_id": state["run_id"],
            "blocked_by": state.get("blocked_by", []),
            "override": (
                {"actor_id": state.get("override_by"), "reason": state["override_reason"]}
                if state.get("override_reason")
                else None
            ),
            "document_id": state.get("document_id"),
            "document_type": state.get("document_type"),
            # What this document says it is and what it says it relates to. A conflict
            # that was not opened because two facts were out of scope is only auditable
            # if the scoping decision is on the report next to it.
            "agreement_id": state.get("agreement_id"),
            "relations": state.get("relations", []),
            "affected_keys": state.get("affected_keys", []),
            "committed_keys": state.get("committed_keys", []),
            "stale_keys": state.get("stale_keys", []),
            # What a reviewer's decisions were bound to, and what stopped binding.
            "review": {
                "deliverable_reviewed_by": state.get("deliverable_review_by"),
                "deliverable_confirmed": state.get("deliverable_confirmed"),
                "candidate_basis_hash": state.get("candidate_basis_hash"),
                "ruleset_hash": state.get("ruleset_hash"),
                "decisions_stale": state.get("decisions_stale", []),
            },
            "unchanged_keys": sorted(set(state.get("unchanged_keys", [])) | set(preserved)),
            "unsupported_count": state.get("unsupported_count", 0),
            "open_conflicts": [
                {"key": conflict.key, "kind": conflict.kind, "rationale": conflict.rationale}
                for conflict in open_conflicts
            ],
            "rules": _rules_summary(state),
            # Named, not just counted. A warning nobody can act on is decoration.
            "injection": {
                "flagged": state.get("injection_flag", False),
                "quarantined_blocks": [
                    {
                        "index": entry["index"],
                        "page": entry["page"],
                        "extraction_method": entry["extraction_method"],
                        "signals": entry["signals"],
                        "quotes": entry["quotes"],
                    }
                    for entry in state.get("quarantined_blocks", [])
                ],
                # Says what the flag is and, just as importantly, what it is not.
                "detection_is_telemetry": (
                    "a block matching no pattern is still untrusted text; no proposal "
                    "commits without a human, whichever way the scan came out"
                ),
            },
            "register_items": len(register),
            "register_by_agreement": by_agreement,
            "register_hashes": hashes_after,
            "events": len(events) + 1,
            "injection_flag": state.get("injection_flag", False),
            # This node's own event, recorded below, is not in `events` yet -- everything
            # up to and including derivation, review and commit is, which is what a
            # cost report about "what this run cost" needs.
            "cost": build_run_cost_report(events, stage_ledger),
        }
        await _event(
            deps,
            state,
            stage="snapshot_diff_report",
            decision="complete",
            reason="final report assembled from persisted state",
            next_node="END",
            started=started,
        )
        return {"status": report["status"], "report": report}

    return {
        "pin_ruleset": pin_ruleset,
        "rederive": rederive,
        "ingest": ingest,
        "short_circuit": short_circuit,
        "classify": classify,
        "classify_review": classify_review,
        "parse_blocks": parse_blocks,
        "link_documents": link_documents,
        "detect_injection": detect_injection,
        "extract_facts": extract_facts,
        "validate_citations": validate_citations_node,
        "retry_extract": retry_extract,
        "mark_unsupported": mark_unsupported,
        "diff_against_register": diff_against_register,
        "detect_conflicts": detect_conflicts,
        "apply_source_rules": apply_source_rules,
        "route_source_findings": route_source_findings,
        "assemble_proposals": assemble_proposals,
        "await_review": await_review,
        "build_candidate_register": build_candidate_register,
        "apply_deliverable_rules": apply_deliverable_rules,
        "assemble_findings": assemble_findings,
        "await_finding_review": await_finding_review,
        "enforce_blockers": enforce_blockers,
        "verify_review_binding": verify_review_binding,
        "commit_approved": commit_approved,
        "snapshot_diff_report": snapshot_diff_report,
    }

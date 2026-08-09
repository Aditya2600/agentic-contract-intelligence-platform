"""A human decision has to be unavoidable, attributable, and impossible to lose.

Each test here is a way the review gate used to be a formality. A run with nothing adverse
skipped Gate 2 entirely and reported itself clean with nobody's name on it. A dismissed
finding left no trace at all, so "a reviewer judged this inapplicable" and "nobody looked"
were the same row. Decisions were written at commit, so a run that was blocked or refused
lost every decision a human had already made -- exactly the runs whose audit trail matters.
And an approval named no version, so it applied just as happily to a register that had
moved underneath it.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from langgraph.types import Command

from doctask.graph.builder import build_graph
from doctask.graph.nodes import NodeDependencies
from doctask.llm.fake import FakeLLM
from doctask.repositories.memory import InMemoryRepository
from doctask.services.hashing import candidate_basis_hash
from doctask.services.rules import parse_ruleset, source_rule_cache_key

CORPUS = Path(__file__).resolve().parent.parent / "sample_data"
PLAYBOOK = json.loads((CORPUS / "rules.json").read_text())

# Every source verdict is a violation, and PAY-01 is a blocker. The document is a clean
# contract; the playbook is what makes it fail, which keeps the corpus honest.
BLOCKING = {
    **PLAYBOOK,
    "version": 9,
    "rules": [
        {
            "code": "PAY-01",
            "severity": "blocker",
            "scope": "both",
            "keys": ["payment_due_days"],
            "text": "Payment terms must be at least 45 calendar days after receipt.",
        }
    ],
}


class Harness:
    """Runs the graph gate by gate, so a test can answer each one differently."""

    def __init__(self) -> None:
        self.repository = InMemoryRepository()
        self.graph = build_graph(NodeDependencies(repository=self.repository, model=FakeLLM()))
        self.gates: list[str] = []

    async def load(self, playbook: dict) -> None:
        await self.repository.put_ruleset(parse_ruleset(playbook, self.collection_id))

    async def run(self, filename: str, decide, *, override=None, before_gate=None) -> dict:
        run_id = uuid4()
        self.run_id = run_id
        self.gates = []
        config = {"configurable": {"thread_id": str(run_id)}}
        result = await self.graph.ainvoke(
            {
                "run_id": str(run_id),
                "collection_id": str(self.collection_id),
                "idempotency_key": filename + str(run_id),
                "input_document": {
                    "filename": filename,
                    "mime_type": "text/plain",
                    "text": (CORPUS / filename).read_text(),
                },
                "validation_attempt": 0,
                "status": "running",
            },
            config=config,
        )
        while "report" not in result:
            kind = result["__interrupt__"][0].value["kind"]
            self.gates.append(kind)
            if before_gate is not None:
                await before_gate(kind, self)
            if kind == "blocker_override":
                resume = {
                    "actor_id": "reviewer-1",
                    "actor_role": "reviewer",
                    **(override or {"override": False}),
                }
            else:
                pending = [
                    item
                    for item in await self.repository.list_review_items(run_id)
                    if item.state == "pending"
                ]
                resume = {
                    "actor_id": "reviewer-1",
                    "actor_role": "reviewer",
                    "decisions": {str(item.id): decide(item) for item in pending},
                }
            result = await self.graph.ainvoke(Command(resume=resume), config=config)
        self.last_items = await self.repository.list_review_items(run_id)
        return result["report"]

    async def findings(self, run_id=None):
        return await self.repository.list_findings(run_id or self.run_id)

    async def register(self) -> dict:
        return {
            item.register_key.text: item
            for item in await self.repository.list_register(self.collection_id)
        }


@pytest.fixture
async def harness():
    harness = Harness()
    harness.collection_id = await harness.repository.create_collection("acme")
    harness.last_items = []
    return harness


# ------------------------------------------------------------------- partial approval


async def test_approving_some_proposals_and_rejecting_others_commits_only_the_approved(
    harness,
) -> None:
    """A gate is item-level or it is nothing.

    Approving in bulk is the failure mode a review gate exists to prevent, so the run has
    to be able to carry a mixed answer all the way to the register: what was approved
    lands, what was rejected does not, and the deliverable is then judged on what actually
    landed rather than on what was asked for.
    """
    await harness.load(PLAYBOOK)
    report = await harness.run(
        "vendor_msa.txt",
        lambda item: "rejected" if item.target_key.endswith("liability_cap") else "approved",
    )

    register = await harness.register()
    assert "::payment_due_days" in register
    assert "::notice_days" in register
    assert "::liability_cap" not in register, "a rejected proposal must not reach the register"
    assert report["committed_keys"] == ["::notice_days", "::payment_due_days"]

    # The deliverable stage judged what would actually be written, not the request: the
    # rejected key is absent from the candidate register and so was never evaluated.
    judged = {
        finding["target_key"]
        for finding in report["rules"]["findings"]
        if finding["target_kind"] == "register_item"
    }
    assert "::liability_cap" not in judged

    decided = {item.target_key: item.state for item in harness.last_items}
    assert decided["::liability_cap"] == "rejected"
    assert decided["::payment_due_days"] == "approved"
    assert all(item.decided_by == "reviewer-1" for item in harness.last_items)


# --------------------------------------------------------------- dismissal is recorded


async def test_dismissing_a_finding_records_the_disagreement_and_forces_a_recheck(
    harness,
) -> None:
    """Rejecting a finding is a reviewer disagreeing, not the finding going away.

    The verdict, its rationale and its citations stay exactly as the evaluation wrote
    them; what changes is that a named human is now on record saying it does not apply.
    And because that is a judgement about one run's evidence, the source-rule cache entry
    that would replay it to every future upload of the same bytes is dropped -- otherwise
    one reviewer's call quietly becomes the playbook's answer.
    """
    await harness.load(BLOCKING)
    report = await harness.run(
        "vendor_msa.txt",
        lambda item: "rejected" if item.kind == "finding" else "approved",
    )

    findings = await harness.findings()
    dismissed = [f for f in findings if f.review_decision == "dismissed"]
    assert dismissed, "a rejected finding has to leave a record"
    for finding in dismissed:
        assert finding.system_verdict == "violation"  # untouched by the disagreement
        assert finding.decided_by == "reviewer-1"
        assert finding.recheck_required is True
        assert finding.rationale and finding.citations

    # Reported, not swallowed: the run cannot call itself clean over a dismissed
    # violation, and the report names who dismissed it.
    assert report["rules"]["clean"] is False
    assert report["rules"]["violation"] >= 1
    assert {entry["decided_by"] for entry in report["rules"]["dismissed"]} == {"reviewer-1"}

    # The cache entry that would have served the dismissed verdict back is gone.
    cache_key = source_rule_cache_key(
        collection_id=harness.collection_id,
        document_sha256=harness.repository.documents[UUID(report["document_id"])].sha256,
        ruleset_hash=report["review"]["ruleset_hash"],
    )
    assert await harness.repository.get_source_rule_cache(cache_key) is None

    # So re-uploading the same bytes re-earns the verdict rather than inheriting the call.
    second = await harness.run("vendor_msa.txt", lambda item: "approved")
    assert second["status"] == "duplicate_noop"
    reissued = [
        finding
        for finding in await harness.findings(UUID(second["run_id"]))
        if finding.target_kind == "document"
    ]
    assert [finding.system_verdict for finding in reissued] == ["violation"]
    assert [finding.review_decision for finding in reissued] == ["upheld"]
    assert [finding.recheck_required for finding in reissued] == [False]
    # And the dismissal on the first run is still exactly where it was.
    original = await harness.findings(UUID(report["run_id"]))
    assert {f.review_decision for f in original} == {"dismissed"}
    assert {f.target_kind for f in original} == {"document", "register_item"}


# ------------------------------------------------------------------- upheld blocker


async def test_an_upheld_blocker_stops_the_commit_and_keeps_the_decision(harness) -> None:
    """Approving a blocker finding means "this problem is real". It cannot also mean
    "commit anyway", and the decision has to survive the run being blocked."""
    await harness.load(BLOCKING)
    report = await harness.run("vendor_msa.txt", lambda item: "approved")

    assert report["status"] == "blocked"
    assert report["blocked_by"] == ["PAY-01"]
    assert report["committed_keys"] == []
    assert await harness.register() == {}

    # Recorded at the gate, not at commit. This run never reached commit, and the decision
    # is on the record anyway -- which is the only version of this that is an audit trail.
    findings = await harness.findings()
    assert {f.review_decision for f in findings} == {"upheld"}
    assert {f.decided_by for f in findings} == {"reviewer-1"}
    assert {f.system_verdict for f in findings} == {"violation"}
    assert harness.repository.run_status[harness.run_id] == "blocked"


async def test_an_override_still_needs_the_findings_decided_first(harness) -> None:
    """The override gate is offered after Gate 2, never instead of it."""
    await harness.load(BLOCKING)
    report = await harness.run(
        "vendor_msa.txt",
        lambda item: "approved",
        override={"override": True, "reason": "counsel signed off in LEG-9"},
    )

    assert harness.gates == [
        "item_level_review",
        "deliverable_finding_review",
        "blocker_override",
    ]
    assert report["status"] == "committed"
    assert report["override"] == {
        "actor_id": "reviewer-1",
        "reason": "counsel signed off in LEG-9",
    }
    # Committing past a blocker is not the same as the blocker not existing.
    assert report["rules"]["clean"] is False


# ------------------------------------------------- an all-pass run still needs a human


async def test_a_run_with_nothing_adverse_still_has_to_be_confirmed(harness) -> None:
    """The gate that used not to open at all.

    Gate 2 was conditional on something being wrong, so the runs that reported themselves
    clean were precisely the runs no human ever saw. "No adverse findings" is a claim
    about the deliverable; it gets a name on it like any other.
    """
    await harness.load(PLAYBOOK)
    report = await harness.run("vendor_msa.txt", lambda item: "approved")

    assert harness.gates == ["item_level_review", "deliverable_finding_review"]
    confirmation = next(
        item for item in harness.last_items if item.kind == "deliverable_confirmation"
    )
    assert confirmation.state == "approved"
    assert confirmation.decided_by == "reviewer-1"
    # It is a confirmation *of* something: every evaluation it stands for is in the payload.
    assert confirmation.payload["evaluations"]
    assert all(
        entry["system_verdict"] == "pass" for entry in confirmation.payload["evaluations"]
    )
    assert confirmation.payload["rules_expected"] == report["rules"]["rules_expected"]

    assert report["rules"]["clean"] is True
    assert report["rules"]["reviewed_by"] == "reviewer-1"
    assert report["review"]["deliverable_confirmed"] is True


async def test_refusing_to_confirm_denies_the_clean_result(harness) -> None:
    """Declining the confirmation is a statement that the evaluation is not trusted.

    It is not a pass, so the run does not get to claim one, and it does not quietly
    commit as though the reviewer had agreed.
    """
    await harness.load(PLAYBOOK)
    report = await harness.run(
        "vendor_msa.txt",
        lambda item: "rejected" if item.kind == "deliverable_confirmation" else "approved",
    )

    assert report["status"] == "unconfirmed"
    assert report["rules"]["clean"] is False
    assert report["review"]["deliverable_confirmed"] is False
    assert harness.repository.run_status[harness.run_id] == "blocked"


async def test_a_gate_2_item_cannot_be_walked_past_by_leaving_it_out(harness) -> None:
    """Silence is not a decision. A payload that omits an item is refused, not obeyed."""
    await harness.load(PLAYBOOK)
    with pytest.raises(ValueError, match="needs a decision"):
        await harness.run(
            "vendor_msa.txt",
            lambda item: "approved",
            before_gate=_drop_gate_two_items,
        )


async def _drop_gate_two_items(kind: str, harness: Harness) -> None:
    """Make the next resume payload omit the confirmation, by hiding it from `pending`."""
    if kind != "deliverable_finding_review":
        return
    for item in await harness.repository.list_review_items(harness.run_id):
        if item.kind == "deliverable_confirmation":
            item.state = "approved"  # already "decided", so the harness will not send it


# ----------------------------------------------------------- decisions bind to a basis


async def test_every_decision_names_the_register_and_playbook_it_was_made_against(
    harness,
) -> None:
    await harness.load(PLAYBOOK)
    report = await harness.run("vendor_msa.txt", lambda item: "approved")

    basis = report["review"]["candidate_basis_hash"]
    assert basis and report["review"]["ruleset_hash"]
    bound = [item for item in harness.last_items if "basis_hash" in item.payload]
    assert bound, "Gate 2 items carry the basis they were decided against"
    for item in bound:
        assert item.payload["basis_hash"] == basis
        assert item.payload["ruleset_hash"] == report["review"]["ruleset_hash"]
        assert item.payload["item_versions"]


async def test_a_register_that_moves_after_the_decision_makes_the_run_stale(harness) -> None:
    """Another run commits between the gate and the commit.

    The per-key staleness check catches a key this run is itself writing. It cannot catch
    the rest, and a deliverable verdict is a statement about the whole candidate register
    -- so a row this run never touches moving underneath it still means the human agreed
    to something that no longer exists. Refusing costs one re-run; not refusing writes a
    change nobody approved.
    """
    await harness.load(PLAYBOOK)
    await harness.run("vendor_msa.txt", lambda item: "approved")
    before = await harness.register()

    async def commit_something_else(kind: str, inner: Harness) -> None:
        if kind != "deliverable_finding_review":
            return
        item = before["::liability_cap"]
        item.version += 1
        item.content_hash = "0" * 64

    report = await harness.run(
        "amendment_1.txt", lambda item: "approved", before_gate=commit_something_else
    )

    assert report["status"] == "stale"
    assert report["committed_keys"] == []
    assert [entry["kind"] for entry in report["review"]["decisions_stale"]] == [
        "register_moved"
    ]
    assert report["review"]["decisions_stale"][0]["key"] == "::liability_cap"
    assert harness.repository.run_status[harness.run_id] == "blocked"

    # And the decisions the human did make are still on the record, on a run that
    # committed nothing.
    adverse = [f for f in await harness.findings() if f.adverse]
    assert adverse and all(f.review_decision == "upheld" for f in adverse)
    assert all(f.decided_by == "reviewer-1" for f in adverse)


async def test_an_edited_playbook_after_the_decision_makes_the_run_stale(harness) -> None:
    """The verdicts a human upheld were reached under one playbook. Editing it mid-run
    means the rules they were judged under are not the rules in force."""
    await harness.load(PLAYBOOK)

    async def edit_the_playbook(kind: str, inner: Harness) -> None:
        if kind == "deliverable_finding_review":
            await inner.load({**PLAYBOOK, "version": 2})

    report = await harness.run(
        "vendor_msa.txt", lambda item: "approved", before_gate=edit_the_playbook
    )

    assert report["status"] == "stale"
    assert report["committed_keys"] == []
    assert [entry["kind"] for entry in report["review"]["decisions_stale"]] == [
        "ruleset_changed"
    ]


def test_the_basis_hash_ignores_what_cannot_change_a_verdict() -> None:
    """It has to be sensitive enough to catch a real move and quiet enough not to cry
    stale on a fact re-inserted under a new row id."""
    rows = [
        {"scoped_key": "::payment_due_days", "value": {"days": 30}, "state": "supported",
         "version": 1, "citation_fact_ids": [str(uuid4())]},
    ]
    reordered = [{**rows[0], "citation_fact_ids": [str(uuid4())]}]
    assert candidate_basis_hash(rows, ruleset_hash="a") == candidate_basis_hash(
        reordered, ruleset_hash="a"
    )
    moved = [{**rows[0], "version": 2}]
    assert candidate_basis_hash(moved, ruleset_hash="a") != candidate_basis_hash(
        rows, ruleset_hash="a"
    )
    revalued = [{**rows[0], "value": {"days": 45}}]
    assert candidate_basis_hash(revalued, ruleset_hash="a") != candidate_basis_hash(
        rows, ruleset_hash="a"
    )
    assert candidate_basis_hash(rows, ruleset_hash="b") != candidate_basis_hash(
        rows, ruleset_hash="a"
    )

"""Two-stage rule evaluation: explicit verdicts, cited, and never silently empty."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
from langgraph.types import Command

from doctask.domain import Block, Rule
from doctask.graph import nodes
from doctask.graph.builder import build_graph
from doctask.graph.nodes import NodeDependencies, _rules_summary
from doctask.llm.base import RuleExcerpt, RuleVerdict
from doctask.llm.fake import FakeLLM
from doctask.repositories.memory import InMemoryRepository
from doctask.services.hashing import sha256_text
from doctask.services.rules import ground_verdict, parse_ruleset, rules_for, ruleset_hash

CORPUS = Path(__file__).resolve().parent.parent / "data" / "sample_data"
PLAYBOOK = json.loads((CORPUS / "rules.json").read_text())


class Harness:
    def __init__(self) -> None:
        self.repository = InMemoryRepository()
        self.model = FakeLLM()
        self.graph = build_graph(
            NodeDependencies(repository=self.repository, model=self.model)
        )

    async def load_rules(self, payload: dict) -> None:
        await self.repository.put_ruleset(parse_ruleset(payload, self.collection_id))

    async def run(self, filename: str, decide=lambda item: "approved", override=None) -> dict:
        run_id = uuid4()
        self.run_id = run_id
        self.gates: list[str] = []
        config = {"configurable": {"thread_id": str(run_id)}}
        result = await self.graph.ainvoke(
            {
                "run_id": str(run_id),
                "collection_id": str(self.collection_id),
                "idempotency_key": filename,
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
        # Gates, in order: register proposals, deliverable findings, blocker override.
        while "report" not in result:
            self.gates.append(result["__interrupt__"][0].value["kind"])
            if self.gates[-1] == "blocker_override":
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


@pytest.fixture
async def harness():
    harness = Harness()
    harness.collection_id = await harness.repository.create_collection("acme")
    return harness


# --------------------------------------------------------------------------- parsing


def test_playbook_parses_into_versioned_rules() -> None:
    ruleset = parse_ruleset(PLAYBOOK, uuid4())
    assert [rule.code for rule in ruleset.rules] == ["PAY-01", "LIA-01"]
    assert [rule.code for rule in rules_for(ruleset, "source")] == ["PAY-01", "LIA-01"]
    assert [rule.code for rule in rules_for(ruleset, "deliverable")] == ["PAY-01"]


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"rules": []}, "non-empty rules"),
        (
            {"rules": [{"code": "A", "text": "t", "severity": "huge", "scope": "source"}]},
            "severity",
        ),
        ({"rules": [{"code": "A", "text": "t", "severity": "major", "scope": "nowhere"}]}, "scope"),
        ({"name": ""}, "name"),
    ],
)
def test_malformed_playbooks_are_rejected(mutation, message) -> None:
    with pytest.raises(ValueError, match=message):
        parse_ruleset({**PLAYBOOK, **mutation}, uuid4())


def test_duplicate_rule_codes_are_rejected() -> None:
    rule = {"code": "PAY-01", "text": "t", "severity": "major", "scope": "source"}
    with pytest.raises(ValueError, match="duplicate rule code"):
        parse_ruleset({**PLAYBOOK, "rules": [rule, rule]}, uuid4())


def test_a_rule_aimed_at_a_key_the_register_cannot_hold_is_refused() -> None:
    """A control that never runs looks exactly like a control that found nothing wrong.

    `target_keys` is matched against the register keys a run touched, so a rule naming
    `termination_notice_days` when the ontology says `notice_days` is skipped at the
    deliverable stage forever, silently, and the report stays clean.
    """
    rule = {
        "code": "TERM-01",
        "text": "Convenience termination notice must be at least 60 calendar days.",
        "severity": "minor",
        "scope": "both",
        "keys": ["termination_notice_days"],
    }
    with pytest.raises(ValueError, match="unknown register key"):
        parse_ruleset({**PLAYBOOK, "rules": [rule]}, uuid4())

    accepted = parse_ruleset({**PLAYBOOK, "rules": [{**rule, "keys": ["notice_days"]}]}, uuid4())
    assert accepted.rules[0].target_keys == ["notice_days"]


# --------------------------------------------------------------------------- grounding


def _block(text: str) -> Block:
    return Block(
        document_id=uuid4(),
        index=0,
        text=text,
        text_sha256=sha256_text(text),
        char_start=0,
        char_end=len(text),
    )


def _excerpt(text: str, index: int = 0) -> RuleExcerpt:
    block = _block(text)
    return RuleExcerpt(index=index, label=f"block {index}", text=text, block_id=block.id)


def test_violation_without_a_verbatim_quote_is_downgraded() -> None:
    excerpt = _excerpt("Payment is due within 30 calendar days.")
    verdict, rationale, citations = ground_verdict(
        RuleVerdict("violation", "terms are too long", "Payment is due within 90 days", 0),
        [excerpt],
    )
    assert verdict == "insufficient_evidence"
    assert "not verbatim" in rationale
    assert citations == []


def test_grounded_violation_keeps_its_citation_offsets() -> None:
    excerpt = _excerpt("Payment is due within 10 calendar days.")
    quote = "due within 10 calendar days"
    verdict, _, citations = ground_verdict(
        RuleVerdict("violation", "below the minimum", quote, 0), [excerpt]
    )
    assert verdict == "violation"
    assert len(citations) == 1
    citation = citations[0]
    assert citation.block_id == excerpt.block_id
    assert excerpt.text[citation.quote_start : citation.quote_end] == quote


def test_a_quote_from_a_different_excerpt_than_the_one_named_is_rejected() -> None:
    """Location is half of what a citation is for.

    The quote below is verbatim somewhere, so a scan across all excerpts would have
    accepted it and silently repaired the model's claim about where it read it.
    """
    payment = _excerpt("Payment is due within 10 calendar days.", index=0)
    liability = _excerpt("Liability is capped at $250,000.", index=1)

    verdict, rationale, citations = ground_verdict(
        RuleVerdict("violation", "below the minimum", "due within 10 calendar days", 1),
        [payment, liability],
    )

    assert verdict == "insufficient_evidence"
    assert "not verbatim in block 1" in rationale
    assert citations == []


def test_an_excerpt_index_that_was_never_offered_is_rejected() -> None:
    verdict, rationale, _ = ground_verdict(
        RuleVerdict("violation", "below the minimum", "Payment is due", 7),
        [_excerpt("Payment is due within 10 calendar days.")],
    )
    assert verdict == "insufficient_evidence"
    assert "excerpt 7 was never offered" in rationale


def test_quoting_derived_state_grounds_nothing() -> None:
    """A register row is a rendering. Quoting it back proves the rendering, not the term."""
    row = RuleExcerpt(index=0, label="proposed value for payment_due_days",
                      text='payment_due_days = {"days":10} (state: supported)')

    verdict, rationale, citations = ground_verdict(
        RuleVerdict("violation", "below the minimum", '{"days":10}', 0), [row]
    )

    assert verdict == "insufficient_evidence"
    assert "derived state, not a source block" in rationale
    assert citations == []


# --------------------------------------------------------------------------- stages


async def test_clean_corpus_produces_explicit_pass_rows(harness) -> None:
    await harness.load_rules(PLAYBOOK)

    report = await harness.run("vendor_msa.txt")

    rules = report["rules"]
    # Two source rules plus one deliverable rule, every one of them recorded.
    assert rules["rules_expected"] == 3
    assert rules["rules_completed"] == 3
    assert rules["rules_failed"] == 0
    assert rules["evaluation_complete"] is True
    assert rules["clean"] is True
    assert rules["violation"] == 0
    assert rules["pass"] == 3
    verdicts = {(f["rule_code"], f["target_kind"], f["system_verdict"]) for f in rules["findings"]}
    assert verdicts == {
        ("PAY-01", "document", "pass"),
        ("LIA-01", "document", "pass"),
        ("PAY-01", "register_item", "pass"),
    }
    # Source-stage verdicts quote the document. The register-stage pass quotes the
    # proposal it read, which is not a block, so it carries a rationale and no citation.
    assert all(
        finding["citations"]
        for finding in rules["findings"]
        if finding["target_kind"] == "document"
    )
    assert all(finding["rationale"] for finding in rules["findings"])
    # A pass needs no human decision, so no finding review item was raised.
    assert all(item.kind != "finding" for item in harness.last_items)


async def test_changing_the_ruleset_changes_behaviour_without_code_edits(harness) -> None:
    stricter = {
        **PLAYBOOK,
        "version": 2,
        "rules": [
            {
                "code": "PAY-01",
                "severity": "blocker",
                "scope": "source",
                "text": "Payment terms must be at least 45 calendar days after receipt.",
            }
        ],
    }
    await harness.load_rules(stricter)

    report = await harness.run("vendor_msa.txt")

    rules = report["rules"]
    assert (rules["violation"], rules["pass"]) == (1, 0)
    violation = rules["findings"][0]
    assert violation["severity"] == "blocker"
    assert "30 is below the required minimum 45" in violation["rationale"]
    # A violation is a decision a human owns, and it cites the source text.
    finding_items = [item for item in harness.last_items if item.kind == "finding"]
    assert [item.target_key for item in finding_items] == ["PAY-01"]
    assert finding_items[0].payload["citations"][0]["quote"].startswith("Payment is due")


async def test_a_model_outage_fails_the_run_instead_of_becoming_evidence(
    harness, monkeypatch
) -> None:
    await harness.load_rules(PLAYBOOK)

    async def explode(rule: Rule, *, target_label: str, excerpts: list, statement=None) -> RuleVerdict:
        raise RuntimeError("model server unreachable")

    monkeypatch.setattr(harness.model, "evaluate_rule", explode)

    with pytest.raises(RuntimeError, match="model server unreachable"):
        await harness.run("vendor_msa.txt")

    # An unreachable server must not read as "the document says nothing about this".
    assert await harness.repository.list_findings(harness.run_id) == []
    assert await harness.repository.list_register(harness.collection_id) == []
    events = {event.stage: event for event in await harness.repository.list_events(harness.run_id)}
    assert events["apply_source_rules"].error_class == "transient"
    assert "none recorded" in events["apply_source_rules"].reason


async def test_a_partly_evaluated_stage_records_nothing(harness, monkeypatch) -> None:
    """The stage is all or nothing: a rule that fails discards the verdicts before it."""
    await harness.load_rules(PLAYBOOK)
    calls: list[str] = []
    original = harness.model.evaluate_rule

    async def flaky(rule: Rule, *, target_label: str, excerpts: list, statement=None) -> RuleVerdict:
        calls.append(rule.code)
        if rule.code == "LIA-01":
            raise TimeoutError("read timeout")
        return await original(rule, target_label=target_label, excerpts=excerpts, statement=statement)

    monkeypatch.setattr(harness.model, "evaluate_rule", flaky)

    with pytest.raises(TimeoutError):
        await harness.run("vendor_msa.txt")

    assert calls == ["PAY-01", "LIA-01"]
    # PAY-01 had a verdict. It is not kept: half a stage is not a rules result.
    assert await harness.repository.list_findings(harness.run_id) == []


async def test_deliverable_rules_read_the_approved_register_not_the_proposal(harness) -> None:
    await harness.load_rules(PLAYBOOK)

    # Reject every proposal. Nothing reaches the candidate register, so the deliverable
    # stage has no target at all rather than judging values a human threw away.
    report = await harness.run("vendor_msa.txt", decide=lambda item: "rejected")

    assert report["committed_keys"] == []
    assert [f for f in report["rules"]["findings"] if f["target_kind"] == "register_item"] == []
    # Only the two source rules were ever expected, and the denominator says so.
    assert report["rules"]["rules_expected"] == 2
    assert report["rules"]["rules_completed"] == 2

    stages = [event.stage for event in await harness.repository.list_events(harness.run_id)]
    # The deliverable stage ran only after the human decided.
    assert stages.index("await_review") < stages.index("apply_deliverable_rules")


async def test_a_deliverable_violation_opens_the_second_gate(harness) -> None:
    """The register value passes the source rule but fails a stricter register rule."""
    await harness.load_rules(
        {
            **PLAYBOOK,
            "version": 7,
            "rules": [
                {
                    "code": "PAY-02",
                    "severity": "major",
                    "scope": "deliverable",
                    "keys": ["payment_due_days"],
                    "text": "Payment terms must be at least 45 calendar days after receipt.",
                }
            ],
        }
    )

    report = await harness.run("vendor_msa.txt")

    assert harness.gates == ["item_level_review", "deliverable_finding_review"]
    finding = report["rules"]["findings"][0]
    assert (finding["target_kind"], finding["system_verdict"]) == ("register_item", "violation")
    # The verdict is about one named obligation, and it cites the contract rather than
    # the rendering of the contract.
    assert finding["citations"][0]["quote"].startswith("Payment is due")
    items = [item for item in harness.last_items if item.kind == "finding"]
    assert [item.payload["target_kind"] for item in items] == ["register_item"]


async def test_findings_persist_and_replay_without_duplicating(harness) -> None:
    await harness.load_rules(PLAYBOOK)
    await harness.run("vendor_msa.txt")

    findings = await harness.repository.list_findings(harness.run_id)
    assert len(findings) == 3
    stored = await harness.repository.put_findings(findings)
    assert [finding.id for finding in stored] == [finding.id for finding in findings]
    assert len(await harness.repository.list_findings(harness.run_id)) == 3


# --------------------------------------------------------------------------- pinning


async def test_the_ruleset_is_pinned_for_the_whole_run(harness) -> None:
    """A playbook uploaded mid-run must not judge one document against two versions."""
    await harness.load_rules(PLAYBOOK)

    stricter = {
        **PLAYBOOK,
        "version": 99,
        "rules": [
            {
                "code": "PAY-01",
                "severity": "blocker",
                "scope": "deliverable",
                "keys": ["payment_due_days"],
                "text": "Payment terms must be at least 45 calendar days after receipt.",
            }
        ],
    }

    async def upload_between_stages(item):
        # Runs while the graph is parked at the first gate, before deliverable rules.
        await harness.load_rules(stricter)
        return "approved"

    run_id = uuid4()
    harness.run_id = run_id
    config = {"configurable": {"thread_id": str(run_id)}}
    result = await harness.graph.ainvoke(
        {
            "run_id": str(run_id),
            "collection_id": str(harness.collection_id),
            "idempotency_key": "pin",
            "input_document": {
                "filename": "vendor_msa.txt",
                "mime_type": "text/plain",
                "text": (CORPUS / "vendor_msa.txt").read_text(),
            },
            "validation_attempt": 0,
            "status": "running",
        },
        config=config,
    )
    while "report" not in result:
        pending = [
            item
            for item in await harness.repository.list_review_items(run_id)
            if item.state == "pending"
        ]
        decisions = {str(item.id): await upload_between_stages(item) for item in pending}
        result = await harness.graph.ainvoke(
            Command(
                resume={
                    "actor_id": "reviewer-1",
                    "actor_role": "reviewer",
                    "decisions": decisions,
                }
            ),
            config=config,
        )

    # v99 is now the active ruleset, but this run finished on the one it pinned: the
    # original PAY-01 at 30 days, which the MSA satisfies.
    assert (await harness.repository.get_active_ruleset(harness.collection_id)).version == 99
    rules = result["report"]["rules"]
    assert rules["violation"] == 0
    assert rules["clean"] is True

    events = {event.stage: event for event in await harness.repository.list_events(run_id)}
    assert "Buyer Contract Playbook v1" in events["pin_ruleset"].reason
    assert "sha256:" in events["pin_ruleset"].reason


async def test_editing_a_playbook_changes_its_hash(harness) -> None:
    original = parse_ruleset(PLAYBOOK, harness.collection_id)
    edited = parse_ruleset({**PLAYBOOK, "version": 2}, harness.collection_id)

    assert ruleset_hash(original) == ruleset_hash(parse_ruleset(PLAYBOOK, harness.collection_id))
    assert ruleset_hash(original) != ruleset_hash(edited)


# ------------------------------------------------------------------- clean is earned


async def test_a_collection_with_no_playbook_is_not_clean(harness) -> None:
    """Zero violations out of zero rules is not a clean bill of health."""
    report = await harness.run("vendor_msa.txt")

    rules = report["rules"]
    assert rules["rules_expected"] == 0
    assert rules["violation"] == 0
    # Nothing was checked, so nothing may be claimed.
    assert rules["clean"] is False
    # Vacuously complete: no rule was expected, none is missing.
    assert rules["evaluation_complete"] is True


async def test_one_adverse_verdict_denies_clean_however_many_passed(harness) -> None:
    await harness.load_rules(PLAYBOOK)

    report = await harness.run("amendment_1.txt")

    rules = report["rules"]
    assert (rules["pass"], rules["insufficient_evidence"]) == (2, 1)
    assert rules["evaluation_complete"] is True
    # Two of three passed. That is not "no findings".
    assert rules["clean"] is False


async def test_clean_is_never_claimed_when_a_rule_is_missing_a_verdict(harness) -> None:
    """The report's denominator is what makes a dropped rule visible.

    Nothing in the graph can drop a rule today - an evaluation error re-raises - so this
    forces the state a buggy repository would produce and checks the summary refuses to
    call it clean.
    """
    await harness.load_rules(PLAYBOOK)
    await harness.run("vendor_msa.txt")

    state = await harness.graph.aget_state(
        {"configurable": {"thread_id": str(harness.run_id)}}
    )
    clean = _rules_summary(state.values)
    assert clean["clean"] is True

    # Same three passes, but one more rule was expected than came back.
    short = _rules_summary({**state.values, "rules_expected": 4})
    assert short["violation"] == 0 and short["pass"] == 3
    assert short["evaluation_complete"] is False
    assert short["clean"] is False

    # And a rule that failed denies clean even with the counts lining up.
    failed = _rules_summary({**state.values, "rules_failed": 1})
    assert failed["evaluation_complete"] is False
    assert failed["clean"] is False


# --------------------------------------------------------------------------- blockers

BLOCKER_PLAYBOOK = {
    **PLAYBOOK,
    "version": 9,
    "rules": [
        {
            "code": "PAY-01",
            "severity": "blocker",
            "scope": "source",
            "text": "Payment terms must be at least 45 calendar days after receipt.",
        }
    ],
}


async def test_an_upheld_blocker_stops_the_commit(harness) -> None:
    await harness.load_rules(BLOCKER_PLAYBOOK)

    # "approved" on a finding means the problem is real, so it cannot also mean
    # "commit anyway". The override gate is offered, and declined.
    report = await harness.run("vendor_msa.txt")

    # Gate 2 sits between them now: the deliverable findings are decided before anyone is
    # offered an override on the blocker among them.
    assert harness.gates == [
        "item_level_review",
        "deliverable_finding_review",
        "blocker_override",
    ]
    assert report["status"] == "blocked"
    assert report["blocked_by"] == ["PAY-01"]
    assert report["override"] is None
    assert report["committed_keys"] == []
    # Nothing was written. The register is exactly as it was.
    assert await harness.repository.list_register(harness.collection_id) == []
    assert harness.repository.run_status[harness.run_id] == "blocked"


async def test_rejecting_the_blocker_finding_lets_the_run_continue(harness) -> None:
    await harness.load_rules(BLOCKER_PLAYBOOK)

    # A human who judges the finding not to apply rejects it. That is not an override:
    # there is no blocker left to override, so no gate is offered.
    report = await harness.run(
        "vendor_msa.txt",
        decide=lambda item: "rejected" if item.kind == "finding" else "approved",
    )

    assert harness.gates == ["item_level_review", "deliverable_finding_review"]
    assert report["status"] == "committed"
    assert report["blocked_by"] == []
    assert report["committed_keys"] == [
        "::liability_cap",
        "::notice_days",
        "::payment_due_days",
    ]


async def test_an_override_needs_a_reason_and_is_recorded(harness) -> None:
    await harness.load_rules(BLOCKER_PLAYBOOK)

    report = await harness.run(
        "vendor_msa.txt",
        override={"override": True, "reason": "counsel signed off in ticket LEG-412"},
    )

    assert report["status"] == "committed"
    # The commit happened, and why it was allowed to is on the record.
    assert report["blocked_by"] == ["PAY-01"]
    assert report["override"] == {
        "actor_id": "reviewer-1",
        "reason": "counsel signed off in ticket LEG-412",
    }
    assert report["committed_keys"] == [
        "::liability_cap",
        "::notice_days",
        "::payment_due_days",
    ]

    events = [e for e in await harness.repository.list_events(harness.run_id)]
    override_event = next(e for e in events if e.decision == "human_override")
    assert "LEG-412" in override_event.reason
    assert override_event.error_class == "policy"


async def test_a_reasonless_override_is_refused(harness) -> None:
    await harness.load_rules(BLOCKER_PLAYBOOK)

    with pytest.raises(ValueError, match="needs a reason"):
        await harness.run("vendor_msa.txt", override={"override": True, "reason": "   "})

    # Refused, not downgraded to "leave it blocked": nothing was committed and the run
    # is still parked at the gate, so a corrected override can still be supplied.
    assert await harness.repository.list_register(harness.collection_id) == []
    assert harness.run_id not in harness.repository.run_status


# -------------------------------------------------- the source stage cannot be skipped

# A DPA states no obligation this register can hold, so it derives nothing and proposes
# nothing. It still says something a playbook has an opinion about.
DPA = "dpa_no_terms.txt"


def _liability_playbook(version: int, limit: str) -> dict:
    return {
        **PLAYBOOK,
        "version": version,
        "rules": [
            {
                "code": "LIA-01",
                "severity": "major",
                "scope": "source",
                "text": f"Liability cap must not exceed USD {limit} without legal approval.",
            }
        ],
    }


LENIENT = _liability_playbook(11, "1,000,000")
STRICT = _liability_playbook(12, "500,000")


async def test_a_document_that_changes_no_register_key_is_still_judged(harness) -> None:
    """No obligation moved, so nothing is derived. That is not the same as nothing to say.

    The source stage used to run after derivation, and derivation is skipped when the
    affected key set is empty -- so this document went straight to the report, which then
    said zero violations out of zero rules for a file that violates one.
    """
    await harness.load_rules(STRICT)

    report = await harness.run(DPA)

    assert report["affected_keys"] == []
    assert report["committed_keys"] == []
    rules = report["rules"]
    assert (rules["rules_expected"], rules["rules_completed"]) == (1, 1)
    assert rules["violation"] == 1
    assert rules["clean"] is False
    finding = rules["findings"][0]
    assert finding["target_kind"] == "document"
    assert "750000 exceeds the permitted maximum 500000" in finding["rationale"]
    assert finding["citations"][0]["quote"].startswith("Processor aggregate liability")
    # Recorded is not enough: a human was asked about it.
    assert harness.gates == ["item_level_review", "deliverable_finding_review"]
    assert [item.target_key for item in harness.last_items if item.kind == "finding"] == [
        "LIA-01"
    ]


async def test_a_duplicate_upload_is_re_judged_when_the_playbook_changed(harness) -> None:
    """The bytes are cached, the verdict is not.

    The duplicate short-circuit used to end the run before the source stage, so a playbook
    edited between two uploads of the same file was never applied to it -- and the second
    report read as clean.
    """
    await harness.load_rules(LENIENT)
    first = await harness.run(DPA)
    assert (first["rules"]["pass"], first["rules"]["violation"]) == (1, 0)

    await harness.load_rules(STRICT)
    second = await harness.run(DPA)

    assert second["status"] == "duplicate_noop"
    assert (second["rules"]["rules_expected"], second["rules"]["violation"]) == (1, 1)
    assert [item.target_key for item in harness.last_items if item.kind == "finding"] == [
        "LIA-01"
    ]
    stages = [event.stage for event in await harness.repository.list_events(harness.run_id)]
    # Still no re-extraction and no model spend on facts: only the rules were re-run.
    assert "extract_facts" not in stages
    assert stages.count("apply_source_rules") == 1


async def test_an_unchanged_playbook_reuses_the_verdicts_instead_of_re_asking(
    harness, monkeypatch
) -> None:
    await harness.load_rules(STRICT)
    await harness.run(DPA)
    first_findings = await harness.repository.list_findings(harness.run_id)

    calls: list[str] = []
    original = harness.model.evaluate_rule

    async def counted(rule: Rule, *, target_label: str, excerpts: list, statement=None):
        calls.append(rule.code)
        return await original(
            rule, target_label=target_label, excerpts=excerpts, statement=statement
        )

    monkeypatch.setattr(harness.model, "evaluate_rule", counted)
    report = await harness.run(DPA)

    # Exact match on document, pinned playbook and evaluator: nothing to re-ask.
    assert calls == []
    assert (report["rules"]["rules_expected"], report["rules"]["violation"]) == (1, 1)
    event = next(
        event
        for event in await harness.repository.list_events(harness.run_id)
        if event.stage == "apply_source_rules"
    )
    assert event.decision == "reuse"
    # Reused, not borrowed: this run owns its own explicit verdict row.
    second_findings = await harness.repository.list_findings(harness.run_id)
    assert [finding.system_verdict for finding in second_findings] == ["violation"]
    assert {finding.id for finding in second_findings}.isdisjoint(
        {finding.id for finding in first_findings}
    )


async def test_a_bumped_evaluator_version_misses_the_cache(harness, monkeypatch) -> None:
    """The cache key names everything a verdict depended on, the evaluator included.

    Reusing across an evaluator change would serve a judgement made by code that no
    longer exists as though the current code had reached it.
    """
    await harness.load_rules(STRICT)
    await harness.run(DPA)

    monkeypatch.setattr(nodes, "SOURCE_RULE_EVALUATOR_VERSION", "2")
    calls: list[str] = []
    original = harness.model.evaluate_rule

    async def counted(rule: Rule, *, target_label: str, excerpts: list, statement=None):
        calls.append(rule.code)
        return await original(
            rule, target_label=target_label, excerpts=excerpts, statement=statement
        )

    monkeypatch.setattr(harness.model, "evaluate_rule", counted)
    report = await harness.run(DPA)

    assert calls == ["LIA-01"]
    assert report["rules"]["violation"] == 1


async def test_approving_a_finding_records_the_human_decision(harness) -> None:
    stricter = {
        **PLAYBOOK,
        "version": 3,
        "rules": [
            {
                "code": "PAY-01",
                "severity": "major",
                "scope": "source",
                "text": "Payment terms must be at least 45 calendar days after receipt.",
            }
        ],
    }
    await harness.load_rules(stricter)

    await harness.run("vendor_msa.txt")

    findings = await harness.repository.list_findings(harness.run_id)
    # The verdict is untouched and the decision sits beside it, with a name on it.
    assert [(f.system_verdict, f.review_decision, f.decided_by) for f in findings] == [
        ("violation", "upheld", "reviewer-1")
    ]
    assert [finding.recheck_required for finding in findings] == [False]


# ------------------------------------------------- deliverable rules, per agreement

COLLECTION_WIDE = {
    "name": "Register-wide playbook",
    "version": 4,
    "rules": [
        {
            "code": "REG-01",
            "severity": "minor",
            "scope": "deliverable",
            # No `keys`: a statement about the register, not about one obligation.
            "text": "Every payment term in the register must be at least 30 days.",
        }
    ],
}


async def test_a_keyed_deliverable_rule_judges_one_agreement_at_a_time(harness) -> None:
    """"payment_due_days violates PAY-01" does not say whose contract.

    With two agreements on file that is the only part anyone needs, and it is also what
    decides whether the finding is actionable: the second agreement's upload must not
    reopen a verdict on the first agreement's term.
    """
    await harness.load_rules(PLAYBOOK)
    await harness.run("msa_alpha.txt")
    report = await harness.run("msa_beta.txt")

    judged = [
        finding
        for finding in report["rules"]["findings"]
        if finding["target_kind"] == "register_item"
    ]
    assert judged, "the deliverable stage judged nothing"
    assert all(finding["rule_code"] == "PAY-01" for finding in judged)
    # Every target names its agreement, and only the agreement this run touched.
    assert {finding["target_key"] for finding in judged} == {
        "MSA-2024-002::payment_due_days"
    }
    # A keyed rule is already per-obligation: an aggregate would be one verdict twice.
    assert not [f for f in report["rules"]["findings"] if f["target_kind"] == "register"]


async def test_a_collection_wide_rule_also_gets_one_explicit_aggregate(harness) -> None:
    """A rule naming no key is about the register as a whole.

    Per-agreement rows alone leave "does this collection satisfy the rule" as something a
    reader has to reconstruct by scanning, which is the same silence as no verdict. The
    aggregate is derived from the rows rather than re-asked of the model: an aggregate
    that could disagree with its own parts would be worse than none, and it would cost a
    second pass to get.
    """
    await harness.load_rules(COLLECTION_WIDE)
    await harness.run("msa_alpha.txt")
    report = await harness.run("msa_beta.txt")

    findings = report["rules"]["findings"]
    scoped = [f for f in findings if f["target_kind"] == "register_item"]
    aggregate = [f for f in findings if f["target_kind"] == "register"]
    assert scoped
    assert len(aggregate) == 1

    verdicts = {finding["system_verdict"] for finding in scoped}
    expected = next(
        (v for v in ("violation", "insufficient_evidence") if v in verdicts), "pass"
    )
    assert aggregate[0] == {
        **aggregate[0],
        "rule_code": "REG-01",
        "target_key": "collection",
        "system_verdict": expected,
    }
    assert str(len(scoped)) in aggregate[0]["rationale"]
    # It counts against the denominator like any other verdict: an aggregate that is not
    # expected is an aggregate that can go missing without the report noticing.
    assert report["rules"]["rules_expected"] == len(findings)
    assert report["rules"]["rules_completed"] == len(findings)

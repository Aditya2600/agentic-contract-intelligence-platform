"""The cost/latency report: what a run cost and where the time went, aggregated purely
from `run_events` and `run_stage_ledger` -- the data the pipeline already writes.

`build_run_cost_report` is tested directly against hand-built events for the properties
that do not need a real graph run (totals summing, replay accounting, unpriced models).
The retried-stage and cache-hit properties are tested through a real, offline graph run,
because they depend on genuine pipeline behaviour (the repair loop, the cross-run source-
rule cache) that a hand-built event list would only be asserting its own construction.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID, uuid4

from langgraph.types import Command

from doctask.domain import Block, FactCandidate, RunEvent, StageRecord
from doctask.graph.builder import build_graph
from doctask.graph.nodes import NodeDependencies
from doctask.llm.fake import FakeLLM
from doctask.repositories.memory import InMemoryRepository
from doctask.services.cost_report import build_run_cost_report
from doctask.services.pricing import ModelPrice, PriceTable
from doctask.services.rules import parse_ruleset

CORPUS = Path(__file__).resolve().parent.parent / "data" / "sample_data"
PLAYBOOK = json.loads((CORPUS / "rules.json").read_text())

UNQUALIFIED = """MASTER SERVICES AGREEMENT

Section 4.3 Payment is due within 30 calendar days of receipt unless disputed in good faith.
"""

TABLE = PriceTable(
    version="test-1",
    prices={
        "priced-model": ModelPrice(input_per_million_usd=10.0, output_per_million_usd=20.0),
        "fake": ModelPrice(input_per_million_usd=0.0, output_per_million_usd=0.0),
    },
)


def _event(stage: str, *, tokens_in: int = 0, tokens_out: int = 0, duration_ms: int = 0, **kw) -> RunEvent:
    return RunEvent(
        run_id=uuid4(),
        stage=stage,
        decision=kw.pop("decision", "continue"),
        reason=kw.pop("reason", ""),
        next_node=kw.pop("next_node", ""),
        duration_ms=duration_ms,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        **kw,
    )


# ------------------------------------------------------------- pure aggregation ----


def test_per_stage_totals_sum_to_the_run_total() -> None:
    events = [
        _event("classify", tokens_in=100, tokens_out=10, duration_ms=5, model="priced-model"),
        _event("extract_facts", tokens_in=200, tokens_out=20, duration_ms=7, model="priced-model"),
        _event("extract_facts", tokens_in=50, tokens_out=5, duration_ms=3, model="priced-model"),
        _event("apply_source_rules", tokens_in=0, tokens_out=0, duration_ms=1, decision="skip"),
    ]
    report = build_run_cost_report(events, [], price_table=TABLE)

    stages = {entry["stage"]: entry for entry in report["stages"]}
    assert report["total_duration_ms"] == sum(entry["duration_ms"] for entry in stages.values())
    assert report["total_spend_usd"] == round(
        sum(entry["spend_usd"] for entry in stages.values()), 6
    )
    assert report["total_tokens_in"] == sum(entry["tokens_in"] for entry in stages.values())
    assert report["total_tokens_out"] == sum(entry["tokens_out"] for entry in stages.values())
    # And each stage's own attempts sum to that stage's total, the same property one
    # level down.
    for entry in stages.values():
        assert entry["duration_ms"] == sum(a["duration_ms"] for a in entry["attempts"])
        assert round(sum(a["spend_usd"] for a in entry["attempts"]), 6) == entry["spend_usd"]


def test_a_retried_stage_is_counted_twice_not_overwritten() -> None:
    events = [
        _event("extract_facts", tokens_in=200, tokens_out=20, duration_ms=7, model="priced-model"),
        _event("extract_facts", tokens_in=210, tokens_out=25, duration_ms=8, model="priced-model"),
    ]
    report = build_run_cost_report(events, [], price_table=TABLE)
    (stage,) = report["stages"]
    assert stage["attempt_count"] == 2
    assert len(stage["attempts"]) == 2
    assert stage["tokens_in"] == 200 + 210
    assert stage["tokens_out"] == 20 + 25
    assert stage["duration_ms"] == 7 + 8
    # Not ledgered, so the report will not claim it knows this is (or is not) a crash
    # replay -- it just refuses to guess.
    assert stage["ledger_backed"] is False
    assert stage["replay_attempts"] is None


def test_a_killed_and_resumed_run_neither_double_counts_nor_loses_spend() -> None:
    """Simulates a stage the exact-once ledger backs, replayed once after a crash.

    Two `RunEvent`s recorded under the same stage name (the node genuinely re-executed
    and genuinely re-billed), and a ledger row reporting `attempts=2` for that
    (run, stage, input_hash) -- exactly what `record_stage`'s upsert produces on a
    replay. The report must keep both events' cost (nothing genuinely spent is lost)
    while marking, via the ledger, that one of the two was a replay rather than a second
    independent attempt (so a reader is not misled into reading it as a repair retry).
    """
    run_id = uuid4()
    events = [
        RunEvent(
            run_id=run_id,
            stage="apply_source_rules",
            decision="continue",
            reason="first attempt, before the crash",
            next_node="assemble_proposals",
            tokens_in=300,
            tokens_out=40,
            duration_ms=12,
            model="priced-model",
        ),
        RunEvent(
            run_id=run_id,
            stage="apply_source_rules",
            decision="continue",
            reason="replay after resume",
            next_node="assemble_proposals",
            tokens_in=300,
            tokens_out=40,
            duration_ms=9,
            model="priced-model",
        ),
    ]
    ledger = [
        StageRecord(
            run_id=run_id,
            stage="apply_source_rules",
            input_hash="doc-sha:ruleset-hash",
            output_hash="verdicts-hash",
            status="completed",
            attempts=2,
        )
    ]
    report = build_run_cost_report(events, ledger, price_table=TABLE)
    (stage,) = report["stages"]

    # Nothing genuinely spent is lost: both attempts' tokens, duration and spend are in
    # the total.
    assert stage["attempt_count"] == 2
    assert stage["tokens_in"] == 600
    assert stage["tokens_out"] == 80
    assert stage["duration_ms"] == 21
    expected_spend = round(2 * (300 / 1_000_000 * 10.0 + 40 / 1_000_000 * 20.0), 6)
    assert stage["spend_usd"] == expected_spend
    assert report["total_spend_usd"] == expected_spend

    # And the ledger is what lets the report say plainly that this was a replay, not two
    # independent evaluations someone asked for.
    assert stage["ledger_backed"] is True
    assert stage["replay_attempts"] == 1
    assert "replay" in report["replay_note"]


def test_an_unpriced_model_surfaces_as_unpriced_not_zero() -> None:
    events = [
        _event("classify", tokens_in=100, tokens_out=10, duration_ms=2, model="mystery-model"),
        _event("extract_facts", tokens_in=50, tokens_out=5, duration_ms=1, model="priced-model"),
        # No tokens at all: a routing decision with nothing to price, not "unpriced".
        _event("route_source_findings", tokens_in=0, tokens_out=0, duration_ms=0, model=None),
    ]
    report = build_run_cost_report(events, [], price_table=TABLE)
    stages = {entry["stage"]: entry for entry in report["stages"]}

    assert stages["classify"]["unpriced_usage"] is True
    assert stages["classify"]["spend_usd"] == 0.0
    assert stages["extract_facts"]["unpriced_usage"] is False
    assert stages["extract_facts"]["spend_usd"] > 0.0
    assert stages["route_source_findings"]["unpriced_usage"] is False

    assert report["has_unpriced_usage"] is True
    assert report["unpriced_models"] == ["mystery-model"]
    # The priced stage's real spend is not swallowed by the unpriced one existing
    # alongside it.
    assert report["total_spend_usd"] == stages["extract_facts"]["spend_usd"]


def test_fakellm_reports_real_nonzero_tokens_priced_at_an_explicit_zero() -> None:
    """Offline runs still produce a real report, at zero spend -- not a silent report."""
    events = [_event("classify", tokens_in=97, tokens_out=6, duration_ms=1, model="fake")]
    report = build_run_cost_report(events, [], price_table=TABLE)
    (stage,) = report["stages"]
    assert stage["tokens_in"] == 97
    assert stage["tokens_out"] == 6
    assert stage["unpriced_usage"] is False  # "fake" is priced -- at zero, not unknown
    assert stage["spend_usd"] == 0.0
    assert report["has_unpriced_usage"] is False


# ------------------------------------------------------------ real graph, offline ----


class _BadQuoteOnce(FakeLLM):
    """First pass on the substantive block cites a quote the block does not contain;
    the repair retry (`wider_context=True`) reuses the real, deterministic extractor."""

    async def extract(self, block: Block, *, wider_context: bool = False) -> list[FactCandidate]:
        if "calendar days" in block.text and not wider_context:
            self.last_usage = {"tokens_in": 42, "tokens_out": 7}
            return [
                FactCandidate(
                    key="payment_due_days",
                    value={"days": 30, "anchor": "receipt"},
                    block_id=block.id,
                    quote="this text is not in the block",
                    quote_start=0,
                    quote_end=0,
                )
            ]
        return await super().extract(block, wider_context=wider_context)


async def _drive(repository: InMemoryRepository, graph, run_id: UUID, result: dict) -> dict:
    while "report" not in result:
        pending = [
            item
            for item in await repository.list_review_items(run_id)
            if item.state == "pending"
        ]
        result = await graph.ainvoke(
            Command(
                resume={
                    "actor_id": "reviewer-1",
                    "actor_role": "reviewer",
                    "decisions": {str(item.id): "approved" for item in pending},
                    "override": False,
                }
            ),
            config={"configurable": {"thread_id": str(run_id)}},
        )
    return result["report"]


async def test_a_genuinely_retried_extract_stage_is_counted_twice_in_a_real_run() -> None:
    repository = InMemoryRepository()
    collection_id = await repository.create_collection("acme")
    await repository.put_ruleset(parse_ruleset(PLAYBOOK, collection_id))
    graph = build_graph(NodeDependencies(repository=repository, model=_BadQuoteOnce()))
    run_id = uuid4()
    result = await graph.ainvoke(
        {
            "run_id": str(run_id),
            "collection_id": str(collection_id),
            "idempotency_key": "doc",
            "input_document": {
                "filename": "agreement.txt",
                "mime_type": "text/plain",
                "text": UNQUALIFIED,
            },
            "validation_attempt": 0,
            "status": "running",
        },
        config={"configurable": {"thread_id": str(run_id)}},
    )
    report = await _drive(repository, graph, run_id, result)

    cost = report["cost"]
    extract_stage = next(s for s in cost["stages"] if s["stage"] == "extract_facts")
    assert extract_stage["attempt_count"] == 2, extract_stage
    assert extract_stage["tokens_in"] > 0
    # The bogus first attempt's tokens are still in the total -- a repair retry does not
    # erase what the failed attempt cost.
    assert extract_stage["tokens_in"] >= 42
    assert extract_stage["ledger_backed"] is False
    assert extract_stage["replay_attempts"] is None

    validate_events = [e for e in cost["stages"] if e["stage"] == "validate_citations"]
    assert validate_events, "validate_citations should have run for both attempts"
    assert validate_events[0]["attempt_count"] == 2


async def test_a_cache_hit_run_costs_materially_less_than_the_first_run() -> None:
    repository = InMemoryRepository()
    collection_id = await repository.create_collection("acme")
    await repository.put_ruleset(parse_ruleset(PLAYBOOK, collection_id))
    graph = build_graph(NodeDependencies(repository=repository, model=FakeLLM()))

    async def _upload(key: str) -> dict:
        run_id = uuid4()
        result = await graph.ainvoke(
            {
                "run_id": str(run_id),
                "collection_id": str(collection_id),
                "idempotency_key": key,
                "input_document": {
                    "filename": "agreement.txt",
                    "mime_type": "text/plain",
                    "text": UNQUALIFIED,
                },
                "validation_attempt": 0,
                "status": "running",
            },
            config={"configurable": {"thread_id": str(run_id)}},
        )
        return await _drive(repository, graph, run_id, result)

    first = await _upload("doc-first")
    second = await _upload("doc-second")

    first_source = next(s for s in first["cost"]["stages"] if s["stage"] == "apply_source_rules")
    second_source = next(
        s for s in second["cost"]["stages"] if s["stage"] == "apply_source_rules"
    )
    # First run actually evaluated the source rules against the document; the second
    # upload of the same bytes reused those verdicts from the source-rule cache instead.
    assert first_source["tokens_in"] > 0
    assert second_source["cache_hits"] >= 1
    assert second_source["tokens_in"] == 0
    assert second_source["spend_usd"] == 0.0
    assert second["cost"]["total_spend_usd"] <= first["cost"]["total_spend_usd"]

    # The duplicate upload skipped classification and extraction entirely -- not merely
    # cached them -- which is a second, larger source of the saving.
    second_stage_names = {s["stage"] for s in second["cost"]["stages"]}
    assert "classify" not in second_stage_names
    assert "extract_facts" not in second_stage_names

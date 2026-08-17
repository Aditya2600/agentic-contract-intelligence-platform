"""Turn a run's own event log into what it cost and where the time went.

Every number here is read out of `run_events` (via `list_events`) and, for the replay
question, `run_stage_ledger` (via `list_stages`) -- nothing new is measured, only
aggregated. Pure function of its inputs: no repository, no I/O beyond an optional price
table load, so it is reproducible from the same events and testable without a database.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from doctask.domain import RunEvent, StageRecord
from doctask.services.pricing import PriceTable, load_price_table

# What "replay_attempts" does and does not mean, stated once rather than guessed at in
# every report. See the module docstring on `RunEvent`/`StageRecord` (domain.py) and
# `record_stage` (repositories/postgres.py) for why the ledger can prove this for some
# stages and not others.
REPLAY_NOTE = (
    "`attempts` lists every recorded execution of a stage, in order, including a stage "
    "re-executed after a crash and resume -- nothing here is dropped, because every "
    "entry reflects a call that genuinely ran and genuinely produced these numbers. For "
    "a stage the exact-once ledger backs (ledger_backed=true), replay_attempts counts "
    "how many of those executions the ledger can prove read the identical input -- a "
    "crash replay of already-attempted work, not a new repair attempt -- so a reader "
    "can tell the two apart. For a stage with no ledger row (ledger_backed=false), the "
    "pipeline keeps no record that would distinguish a crash replay from a genuine "
    "repair-loop retry; replay_attempts is reported as null there rather than a guessed "
    "zero, and every recorded execution still counts in full toward the totals."
)


def _event_cost(event: RunEvent, price_table: PriceTable) -> tuple[float, bool]:
    """(priced spend for this one event, whether its model has no declared price).

    An event with no tokens at all (a routing decision, a skip) costs nothing and is
    never "unpriced" -- there is no usage to have failed to price.
    """
    if event.tokens_in == 0 and event.tokens_out == 0:
        return 0.0, False
    cost = price_table.cost_usd(event.model, tokens_in=event.tokens_in, tokens_out=event.tokens_out)
    return (0.0, True) if cost is None else (cost, False)


def build_run_cost_report(
    events: list[RunEvent],
    stages: list[StageRecord],
    *,
    price_table: PriceTable | None = None,
) -> dict[str, Any]:
    price_table = price_table or load_price_table()

    ledger_replays: dict[str, int] = defaultdict(int)
    ledger_seen: set[str] = set()
    for record in stages:
        ledger_seen.add(record.stage)
        # `attempts` on a ledger row is already "how many times record_stage saw this
        # exact (stage, input_hash) again" -- the replay count is attempts minus the one
        # execution that was not a replay of itself.
        ledger_replays[record.stage] += max(record.attempts - 1, 0)

    by_stage: dict[str, list[RunEvent]] = defaultdict(list)
    for event in events:
        by_stage[event.stage].append(event)

    per_stage: list[dict[str, Any]] = []
    total_duration_ms = 0
    total_spend_usd = 0.0
    total_tokens_in = 0
    total_tokens_out = 0
    unpriced_models: set[str] = set()

    for stage, stage_events in by_stage.items():
        attempts: list[dict[str, Any]] = []
        stage_duration = 0
        stage_spend = 0.0
        stage_tokens_in = 0
        stage_tokens_out = 0
        stage_cache_hits = 0
        stage_external_calls = 0
        stage_models: set[str] = set()
        stage_unpriced = False

        for event in stage_events:
            spend, unpriced = _event_cost(event, price_table)
            if event.model:
                stage_models.add(event.model)
            if unpriced:
                stage_unpriced = True
                unpriced_models.add(event.model or "unknown")
            stage_duration += event.duration_ms
            stage_spend += spend
            stage_tokens_in += event.tokens_in
            stage_tokens_out += event.tokens_out
            stage_cache_hits += int(event.cache_hit)
            stage_external_calls += int(bool(event.external_service))
            attempts.append(
                {
                    "decision": event.decision,
                    "reason": event.reason,
                    "duration_ms": event.duration_ms,
                    "model": event.model,
                    "tokens_in": event.tokens_in,
                    "tokens_out": event.tokens_out,
                    "spend_usd": round(spend, 6),
                    "unpriced": unpriced,
                    "cache_hit": event.cache_hit,
                    "external_service": event.external_service,
                }
            )

        stage_backed = stage in ledger_seen
        per_stage.append(
            {
                "stage": stage,
                "attempt_count": len(stage_events),
                "duration_ms": stage_duration,
                "spend_usd": round(stage_spend, 6),
                "unpriced_usage": stage_unpriced,
                "models": sorted(stage_models),
                "tokens_in": stage_tokens_in,
                "tokens_out": stage_tokens_out,
                "cache_hits": stage_cache_hits,
                "external_service_calls": stage_external_calls,
                "ledger_backed": stage_backed,
                "replay_attempts": ledger_replays[stage] if stage_backed else None,
                "attempts": attempts,
            }
        )
        total_duration_ms += stage_duration
        total_spend_usd += stage_spend
        total_tokens_in += stage_tokens_in
        total_tokens_out += stage_tokens_out

    return {
        "price_table_version": price_table.version,
        # Sum of every stage below, by construction -- never a separately-measured
        # wall-clock figure that could disagree with the breakdown that explains it.
        "total_duration_ms": total_duration_ms,
        # Sum of priced spend only. Real spend that happened under a model this table
        # does not price is never folded in here as an invented number -- see
        # `has_unpriced_usage` and `unpriced_models` for where it went instead.
        "total_spend_usd": round(total_spend_usd, 6),
        "total_tokens_in": total_tokens_in,
        "total_tokens_out": total_tokens_out,
        "has_unpriced_usage": bool(unpriced_models),
        "unpriced_models": sorted(unpriced_models),
        "stages": per_stage,
        "replay_note": REPLAY_NOTE,
    }

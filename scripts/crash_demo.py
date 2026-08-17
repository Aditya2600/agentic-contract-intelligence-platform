"""Kill a run mid-flight, restart, and prove from the database that nothing was lost or
done twice.

Requirement 2 asks for crash recovery to be *demonstrated*, not asserted. So this script
starts a real run in a real subprocess, SIGKILLs that process in the window where a side
effect is already committed but the LangGraph checkpoint that records it is not, then
tears down and rebuilds this process's entire service stack -- connection pool,
checkpointer, compiled graph, through the same `doctask.runtime` wiring the API uses --
and resumes on the same `run_id`.

Every claim at the end is read back out of Postgres:

  * `run_stage_ledger`  -- which stages had completed before the kill, how many times
                           each executed, and whether a replayed stage produced the same
                           `output_hash` it produced the first time;
  * `documents` / `facts` / `register_items` -- that the replay wrote no duplicates;
  * `review_items`      -- that decisions a human made before the crash still carry that
                           human's name and timestamp, unchanged.

None of that comes from log narration. The process that wrote the first half of it was
killed with SIGKILL and never got to say anything.

This needs the durable stack -- `AsyncPostgresSaver` is what a checkpoint survives in --
so unlike `make demo` it requires Postgres. Run with `make demo-crash`.
"""

from __future__ import annotations

import asyncio
import json
import signal
import sys
import tempfile
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
from langgraph.types import Command
from psycopg.rows import dict_row

from doctask.auth import REVIEWER, Principal
from doctask.config import settings
from doctask.services.rules import parse_ruleset

ROOT = Path(__file__).resolve().parent.parent
WORKER = ROOT / "scripts" / "crash_worker.py"
CORPUS = ROOT / "data" / "sample_data"

WIDTH = 78
DATABASE_URL = settings.database_url
DEMO_REVIEWER = Principal(actor_id="demo-reviewer", role=REVIEWER)

failures: list[str] = []


def banner(text: str) -> None:
    print(f"\n{'=' * WIDTH}\n {text}\n{'=' * WIDTH}")


def section(text: str) -> None:
    print(f"\n  -- {text} {'-' * max(0, WIDTH - len(text) - 8)}")


def check(claim: str, condition: bool, detail: str = "") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {claim}" + (f"  [{detail}]" if detail else ""))
    if not condition:
        failures.append(f"{claim} ({detail})" if detail else claim)


async def query(sql: str, *params: Any) -> list[dict]:
    async with await psycopg.AsyncConnection.connect(DATABASE_URL, row_factory=dict_row) as conn:
        cur = await conn.execute(sql, params)
        return await cur.fetchall()


async def ledger(run_id: UUID) -> dict[str, dict]:
    rows = await query(
        "SELECT stage, output_hash, status, attempts FROM run_stage_ledger "
        "WHERE run_id = %s ORDER BY created_at, stage",
        run_id,
    )
    return {row["stage"]: row for row in rows}


def print_ledger(rows: dict[str, dict]) -> None:
    if not rows:
        print("    (nothing recorded yet)")
        return
    for stage, row in rows.items():
        digest = (row["output_hash"] or "-")[:16]
        replay = "   <- REPLAYED" if row["attempts"] > 1 else ""
        print(
            f"    {stage:<26} {row['status']:<10} attempts={row['attempts']}  "
            f"output={digest}{replay}"
        )


async def counts(collection_id: UUID, run_id: UUID) -> dict[str, int]:
    documents = await query(
        "SELECT count(*) AS n FROM documents WHERE collection_id = %s", collection_id
    )
    facts = await query(
        "SELECT count(*) AS rows, count(DISTINCT fact_fingerprint) AS unique_rows "
        "FROM facts WHERE collection_id = %s",
        collection_id,
    )
    items = await query("SELECT count(*) AS n FROM review_items WHERE run_id = %s", run_id)
    checkpoints = await query(
        "SELECT count(*) AS n FROM checkpoints WHERE thread_id = %s", str(run_id)
    )
    return {
        "documents": documents[0]["n"],
        "fact_rows": facts[0]["rows"],
        "fact_fingerprints": facts[0]["unique_rows"],
        "review_items": items[0]["n"],
        "checkpoints": checkpoints[0]["n"],
    }


def print_counts(current: dict[str, int]) -> None:
    print(f"    documents in the collection      {current['documents']}")
    print(
        f"    facts                            {current['fact_rows']} rows / "
        f"{current['fact_fingerprints']} distinct fingerprints"
    )
    print(f"    review items awaiting a human    {current['review_items']}")
    print(f"    LangGraph checkpoints for thread {current['checkpoints']}")


async def decisions_taken(run_id: UUID) -> dict[str, tuple]:
    rows = await query(
        "SELECT id, state, decided_by, decided_at FROM review_items WHERE run_id = %s", run_id
    )
    return {
        str(row["id"]): (row["state"], row["decided_by"], row["decided_at"])
        for row in rows
        if row["decided_at"] is not None
    }


async def crash(spec: dict, label: str) -> None:
    """Run one leg in a subprocess and require that it really died by SIGKILL."""
    with tempfile.TemporaryDirectory() as directory:
        spec_file = Path(directory) / "spec.json"
        spec_file.write_text(json.dumps({"database_url": DATABASE_URL, **spec}))
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(WORKER),
            str(spec_file),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
    if process.returncode != -signal.SIGKILL:
        print(f"  the {label} worker did not crash (rc={process.returncode}):")
        print(stderr.decode()[-2000:])
        failures.append(f"{label} worker was supposed to be SIGKILLed")
        return
    print(f"  worker exited on SIGKILL (rc={process.returncode}) -- as intended, mid-run")


async def restart():
    """Throw this process's whole service stack away and build a fresh one.

    New pool, new `AsyncPostgresSaver`, newly compiled graph -- the same
    `doctask.runtime` wiring `uvicorn doctask.main:app` boots with. This is what a
    restarted deployment looks like to Postgres, and it is deliberately the production
    path rather than a test harness: the thing being demonstrated is that the shipped
    code recovers, not that a bespoke fixture can.
    """
    from doctask.runtime import get_services, shutdown_services

    await shutdown_services()
    return await get_services()


async def pending_decisions(services, run_id: UUID) -> dict:
    """Approve everything currently open, stamped with the reviewer's own identity.

    `resume_run` stamps this in production; the mid-graph legs here drive the compiled
    graph directly, so they stamp it themselves. Without it the gate refuses the payload,
    which is the point of the gate.
    """
    pending = [
        item
        for item in await services.repository.list_review_items(run_id)
        if item.state == "pending"
    ]
    return {
        "decisions": {str(item.id): "approved" for item in pending},
        **DEMO_REVIEWER.as_payload(),
    }


async def main() -> None:
    # Force the durable stack before `doctask.runtime` first builds its services: a
    # checkpoint only survives a SIGKILL if it was written to Postgres.
    settings.repository = "postgres"

    banner("doctask crash-recovery demo -- SIGKILL a run, restart, prove exactly-once")
    print(f"  database    {DATABASE_URL}")
    print("  checkpoint  AsyncPostgresSaver (thread_id = run_id)")
    print("  model       FakeLLM (deterministic, offline -- no API key, no GPU)")

    try:
        services = await restart()
        await services.repository.get_run(uuid4())  # forces a real connection
    except Exception as exc:  # noqa: BLE001 - the message is the whole point here
        print(f"\n  cannot reach Postgres at {DATABASE_URL}: {exc}")
        print("  Run `make db && make migrate` first, or `make demo` for the offline demo.")
        sys.exit(2)

    collection_id = await services.repository.create_collection(f"crash-{uuid4().hex[:8]}")
    # Installed before the run starts, so `pin_ruleset` has something to pin and the run
    # reaches *both* human gates. A collection with no playbook skips the second gate
    # entirely -- correctly, since there is nothing for a human to confirm -- and the
    # commit-replay leg below needs that gate to exist.
    ruleset = await services.repository.put_ruleset(
        parse_ruleset(json.loads((CORPUS / "rules.json").read_text()), collection_id)
    )
    run_id = uuid4()
    start_input = {
        "run_id": str(run_id),
        "collection_id": str(collection_id),
        "idempotency_key": f"crash-demo-{run_id}",
        "input_document": {
            "filename": "vendor_msa.txt",
            "mime_type": "text/plain",
            "text": (CORPUS / "vendor_msa.txt").read_text(),
        },
        "validation_attempt": 0,
        "status": "running",
    }
    config = {"configurable": {"thread_id": str(run_id)}}
    print(f"  collection  {collection_id}")
    print(f"  playbook    {ruleset.name} v{ruleset.version} -> {[r.code for r in ruleset.rules]}")
    print(f"  run id      {run_id}")

    # ---------------------------------------------------------------- kill 1
    banner("Kill 1 -- extracted facts are durable, the checkpoint recording them is not")
    await crash(
        {
            "kill_after": "put_facts",
            "run_id": str(run_id),
            "collection_id": str(collection_id),
            "idempotency_key": f"crash-demo-{run_id}",
            "input": start_input,
        },
        "extraction",
    )

    section("what survived the kill (read from Postgres, not from the dead process)")
    at_kill = await counts(collection_id, run_id)
    print_counts(at_kill)
    print("\n    stage ledger -- the stages that had completed when the process died:")
    ledger_at_kill = await ledger(run_id)
    print_ledger(ledger_at_kill)
    check("work committed before the crash is still there",
          at_kill["fact_rows"] > 0, f"{at_kill['fact_rows']} facts")
    check("the ledger records which stages had completed",
          bool(ledger_at_kill), f"{len(ledger_at_kill)} stage(s)")

    # ------------------------------------------------------------- restart 1
    banner("Restart -- fresh pool, fresh checkpointer, fresh graph, same run_id")
    services = await restart()
    result = await services.graph.ainvoke(None, config=config)
    parked = "__interrupt__" in result
    print(f"  resumed from the checkpoint and ran on to the next human gate: {parked}")

    resumed = await counts(collection_id, run_id)
    section("after the resume")
    print_counts(resumed)
    print("\n    stage ledger -- `attempts=2` is one node replaying, not a second run:")
    print_ledger(await ledger(run_id))

    check("the run continued rather than starting over", parked,
          "parked at a human gate" if parked else "no interrupt")
    check("the replayed ingest did not create a second document",
          resumed["documents"] == 1, str(resumed["documents"]))
    check("the replayed extraction wrote no duplicate facts",
          resumed["fact_rows"] == resumed["fact_fingerprints"] == at_kill["fact_rows"],
          f"{resumed['fact_rows']} rows / {resumed['fact_fingerprints']} fingerprints")

    # ---------------------------------------------------------------- kill 2
    banner("Kill 2 -- the human's decisions are durable, the node recording them is not")
    payload = await pending_decisions(services, run_id)
    print(f"  reviewer answers gate 1 with {len(payload['decisions'])} decision(s); "
          "the process dies before the graph checkpoints them")
    await crash(
        {"kill_after": "decide_review_items", "run_id": str(run_id), "resume": payload},
        "review",
    )
    decided = await decisions_taken(run_id)
    section("the reviewer's decisions, as they stand in the database")
    for item_id, (state, actor, when) in sorted(decided.items()):
        print(f"    {item_id}  {state:<9} by {actor} at {when:%H:%M:%S}")
    check("every decision the reviewer made was committed before the crash",
          set(decided) == set(payload["decisions"]),
          f"{len(decided)} of {len(payload['decisions'])}")

    # ------------------------------------------------------------- restart 2
    banner("Restart -- replay the decision node, carry on to the second gate")
    services = await restart()
    result = await services.graph.ainvoke(None, config=config)
    check("the resumed run reached the second human gate",
          "__interrupt__" in result,
          result["__interrupt__"][0].value["kind"] if "__interrupt__" in result else "no gate")

    # ---------------------------------------------------------------- kill 3
    banner("Kill 3 -- the register write and its ledger row land in one transaction")
    print("  This is the kill that proves idempotent commit. `commit_approved` writes its")
    print("  ledger row inside the same transaction as the register rows, so there is no")
    print("  window where the register has moved and the ledger does not know. The replay")
    print("  after this kill has to return the first commit, not version every row again.")
    gate_two = await pending_decisions(services, run_id)
    await crash(
        {"kill_after": "commit_approved", "run_id": str(run_id), "resume": gate_two},
        "commit",
    )
    committed_at_kill = await query(
        "SELECT key, value, version, content_hash FROM register_items "
        "WHERE collection_id = %s ORDER BY key",
        collection_id,
    )
    ledger_at_commit = await ledger(run_id)
    section("the register, durable, written by a process that then died")
    for row in committed_at_kill:
        print(f"    {row['key']:<20} v{row['version']} {row['value']}")
    check("the killed process committed the register before it died",
          bool(committed_at_kill), f"{len(committed_at_kill)} rows")
    check("the commit's own ledger row is durable too, from the same transaction",
          ledger_at_commit.get("commit_approved", {}).get("status") == "completed",
          str(ledger_at_commit.get("commit_approved", {}).get("status")))

    # ------------------------------------------------------------- restart 3
    banner("Restart -- resume once more; the commit node replays and must agree with itself")
    services = await restart()
    result = await services.graph.ainvoke(None, config=config)
    while "report" not in result:
        result = await services.graph.ainvoke(
            Command(resume=await pending_decisions(services, run_id)), config=config
        )
    report = result["report"]
    print(f"  run finished with status: {report['status']}")

    final_ledger = await ledger(run_id)
    section("final stage ledger")
    print_ledger(final_ledger)

    register = await query(
        "SELECT key, value, version, content_hash FROM register_items "
        "WHERE collection_id = %s ORDER BY key",
        collection_id,
    )
    section("final register")
    for row in register:
        print(
            f"    {row['key']:<20} v{row['version']} {row['value']}"
            f"\n    {'':<20} content_hash {row['content_hash'][:16]}..."
        )

    section("proof, entirely from stored state")
    replayed = {stage: row for stage, row in final_ledger.items() if row["attempts"] > 1}
    check("the run reached its report on the run_id it started on",
          report["run_id"] == str(run_id), report["run_id"])
    check("at least one stage genuinely replayed after a crash",
          bool(replayed), f"replayed: {sorted(replayed)}")
    check("the commit node in particular replayed", "commit_approved" in replayed,
          f"attempts={final_ledger.get('commit_approved', {}).get('attempts')}")
    # `attempts > 1` with an unchanged `output_hash` is a replay that agreed with itself,
    # which is the only thing exactly-once can concretely mean here. A replayed stage
    # that produced something different would be a silent second execution.
    before_replay = {**ledger_at_kill, **ledger_at_commit}
    check("every replayed stage produced the hash it produced the first time",
          all(row["output_hash"] == before_replay[stage]["output_hash"]
              for stage, row in replayed.items() if stage in before_replay),
          "output hashes unchanged across replay")
    check("the replayed commit did not version any register row a second time",
          [(r["key"], r["version"], r["content_hash"]) for r in register]
          == [(r["key"], r["version"], r["content_hash"]) for r in committed_at_kill],
          "register byte-identical to what the killed process wrote")
    check("every ledgered stage finished",
          all(row["status"] == "completed" for row in final_ledger.values()),
          str({s: r["status"] for s, r in final_ledger.items() if r["status"] != "completed"}))

    final = await counts(collection_id, run_id)
    check("exactly one document, after three crashes and a replayed ingest",
          final["documents"] == 1, str(final["documents"]))
    check("no duplicate facts",
          final["fact_rows"] == final["fact_fingerprints"], str(final["fact_rows"]))
    # Not a count: gate 2 legitimately adds its confirmation item after gate 1's, so the
    # total is expected to grow. A duplicate is two items asking the same question.
    duplicates = await query(
        "SELECT kind, target_id, count(*) AS n FROM review_items WHERE run_id = %s "
        "GROUP BY kind, target_id HAVING count(*) > 1",
        run_id,
    )
    check("no review item a human would be asked about twice",
          not duplicates, str([(d["kind"], str(d["target_id"]), d["n"]) for d in duplicates]))

    after = await decisions_taken(run_id)
    check("the decisions taken before the crash are the ones that stand, unchanged",
          {key: after[key] for key in decided} == decided,
          "same actor, same timestamps, no second decision on replay")
    check("the commit wrote each register row exactly once",
          {row["version"] for row in register} == {1},
          str(sorted({row["version"] for row in register})))
    check("the resumed run actually committed something",
          bool(register), f"{len(register)} rows")

    from doctask.runtime import shutdown_services

    await shutdown_services()

    print()
    if failures:
        print(f"CRASH DEMO FAILED: {len(failures)} claim(s) no longer hold")
        for failure in failures:
            print(f"  - {failure}")
        sys.exit(1)
    print("CRASH DEMO OK: killed three times, resumed three times, nothing lost and")
    print("nothing done twice. Every claim above was read back out of Postgres, not")
    print("narrated by a process -- each one that wrote the evidence was SIGKILLed.")


if __name__ == "__main__":
    asyncio.run(main())

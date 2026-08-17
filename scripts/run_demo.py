"""The whole system, end to end, offline, with nobody sitting at the gates.

Seven synthetic documents in four formats through one collection. No model API key, no
GPU, no model server: `FakeLLM` is deterministic, so every number below is reproducible.

    1. MSA PDF          -> payment 30, liability USD 250,000, notice 60; every rule passes
    2. Amendment PDF    -> supersession *proposed*, not applied: payment 30->45, notice 60->90
    3. Invoice PDF      -> NET 10 breaks PAY-01 at the source stage; the human rejects the
                           term and the contractual 45 days stands
    4. DPA DOCX         -> parses, and changes neither payment nor liability
    5. Notice TXT       -> cannot be typed confidently; escalates to a human
    6. Portal policy TXT-> a paragraph instructing the reader to approve it is quarantined;
                           the 5-day term inside it never reaches extraction
    7. SOW TXT          -> a claim the source text does not support: one repair attempt,
                           then abstention. Nothing is committed on a quote that fails.

The gates are driven by the demo reviewer credential rather than skipped, and every
decision is printed as it is taken. Every arrow above is *asserted* against stored state,
not narrated at it: this script exits non-zero the day the pipeline stops producing one
of them. A demo that only prints is a demo that can quietly start lying.

Run with `make demo`.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from uuid import UUID

from doctask.auth import REVIEWER, Principal
from doctask.domain import RegisterKey, ReviewItem
from doctask.runtime import get_services, resume_run, start_run
from doctask.services.extraction import extract_document
from doctask.services.rules import parse_ruleset

PACK = Path("data/demo_pack")

PDF = "application/pdf"
WORD = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PLAIN = "text/plain"

MSA = ("01_Master_Services_Agreement_MSA-2026-014.pdf", PDF)
AMENDMENT = ("02_Amendment_No_1_AMD-2026-014-01.pdf", PDF)
INVOICE = ("03_Invoice_INV-2026-0417.pdf", PDF)
DPA = ("04_Data_Processing_Addendum_DPA-2026-014-A.docx", WORD)
NOTICE = ("05_Operational_Notice_OPS-NOTICE-2026-0528.txt", PLAIN)
PORTAL = ("06_Vendor_Portal_Policy_VPT-2026-014.txt", PLAIN)
SOW = ("07_Statement_of_Work_SOW-2026-014-A.txt", PLAIN)

# The demo runs in-process, so the operator running it is the reviewer. Over the
# network this identity would come from a credential instead: see doctask.auth.
DEMO_REVIEWER = Principal(actor_id="demo-reviewer", role=REVIEWER)

WIDTH = 78

failures: list[str] = []


def check(claim: str, condition: bool, detail: str = "") -> None:
    """Assert one thing the demo promises, and keep going so the run shows all of them."""
    print(f"  {'PASS' if condition else 'FAIL'}  {claim}" + (f"  [{detail}]" if detail else ""))
    if not condition:
        failures.append(f"{claim} ({detail})" if detail else claim)


def banner(text: str) -> None:
    print(f"\n{'=' * WIDTH}\n {text}\n{'=' * WIDTH}")


def section(text: str) -> None:
    print(f"\n  -- {text} {'-' * max(0, WIDTH - len(text) - 8)}")


def _wrap(text: str, indent: str, limit: int = 3) -> None:
    """Print a reason under its stage line, wrapped, and never more than `limit` lines."""
    words, line, lines = text.split(), "", []
    for word in words:
        if len(line) + len(word) + 1 > WIDTH - len(indent):
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        lines.append(line)
    for shown in lines[:limit]:
        print(f"{indent}{shown}")
    if len(lines) > limit:
        print(f"{indent}...")


def print_stages(events: list) -> None:
    """The pipeline's own account of itself: what ran, what it decided, where it went.

    Read straight out of `run_events` -- the same rows `GET /api/runs/{id}/events` and the
    MCP `get_run_events` tool return, and the same rows the cost report aggregates. This
    is not the demo describing the pipeline; it is the pipeline's record being printed.
    """
    section("pipeline stages (from run_events)")
    for index, event in enumerate(events, start=1):
        branch = f"-> {event.next_node}" if event.next_node else ""
        marker = "!" if event.error_class else " "
        print(f"  {index:>3}{marker} {event.stage:<24} {event.decision:<9} {branch}")
        if event.reason:
            _wrap(event.reason, "        ")


def describe_item(item: ReviewItem) -> str:
    """One pending decision, in the terms the reviewer is being asked to decide it."""
    payload = item.payload
    if item.kind == "finding":
        # `target_key` on the item is the rule code; `target_key` in the payload is what
        # that rule was judged against.
        return (
            f"finding      {item.target_key} on {payload['target_key']} "
            f"[{payload['severity']}] {payload['system_verdict']}: {payload['rationale']}"
        )
    if item.kind == "deliverable_confirmation":
        return (
            f"confirmation {len(payload['evaluations'])} deliverable evaluation(s), none "
            "adverse -- a human signs off; the run does not assume it"
        )
    if item.kind == "scope_question":
        return f"scope        {item.target_key} -- {payload['reason']}"
    if item.kind == "injection_review":
        return (
            f"quarantine   {item.target_key} withheld ({', '.join(payload['signals'])})\n"
            f"                 effect: {payload['effect']}\n"
            f"                 matched: {'; '.join(payload['quotes'])[:150]}"
        )
    conflict = payload.get("conflict")
    marker = f" [{conflict['kind']}]" if conflict else ""
    before = (payload.get("before") or {}).get("value")
    return (
        f"proposal     {item.target_key}{marker}: "
        f"{before if before is not None else '(new)'} -> {payload['after']['value']}"
    )


async def register_hashes(collection_id: UUID) -> dict[str, str]:
    services = await get_services()
    return {
        item.register_key.text: item.content_hash
        for item in await services.repository.list_register(collection_id)
    }


async def drive(
    collection_id: UUID,
    document: tuple[str, str],
    key: str,
    decide,
    *,
    document_type: str | None = None,
) -> tuple[UUID, dict]:
    """Extract one real file and drive its run to a report, answering every gate."""
    services = await get_services()
    filename, mime_type = document
    extracted = await extract_document(
        filename=filename, mime_type=mime_type, data=(PACK / filename).read_bytes()
    )
    methods = sorted(extracted.methods)
    pages = sorted({block.page for block in extracted.blocks if block.page})
    print(
        f"  extracted  {len(extracted.blocks)} blocks via {', '.join(methods)}"
        + (f", pages {pages[0]}-{pages[-1]}" if pages else "")
    )

    run_id, result = await start_run(
        collection_id=collection_id,
        idempotency_key=key,
        filename=filename,
        mime_type=mime_type,
        text=extracted.text,
        blocks=[
            {"text": block.text, "page": block.page, "extraction_method": block.extraction_method}
            for block in extracted.blocks
        ],
        principal=DEMO_REVIEWER,
    )

    # Gates, in whatever order this document reaches them: document type, register
    # proposals, deliverable findings, blocker override. Each one is answered with the
    # demo reviewer's credential -- `resume_run` stamps the actor, so `decided_by` records
    # who presented a credential rather than who the payload claimed to be.
    gate = 0
    while "report" not in result:
        gate += 1
        kind = result["__interrupt__"][0].value["kind"]
        if kind == "document_classification":
            chosen = document_type or "unknown"
            section(f"human gate {gate}: document classification")
            print(f"     ESCALATED  classifier was not confident; reviewer files it as {chosen}")
            result = await resume_run(run_id, {"document_type": chosen}, principal=DEMO_REVIEWER)
            continue
        if kind == "blocker_override":
            section(f"human gate {gate}: blocker override")
            print("     UPHELD     blocker stands; nothing is committed")
            result = await resume_run(
                run_id, {"override": False, "reason": ""}, principal=DEMO_REVIEWER
            )
            continue
        pending = [
            item
            for item in await services.repository.list_review_items(run_id)
            if item.state == "pending"
        ]
        section(f"human gate {gate}: {kind} ({len(pending)} item(s))")
        decisions = {}
        for item in pending:
            verdict = decide(item)
            decisions[str(item.id)] = verdict
            print(f"     {verdict.upper():<9}  {describe_item(item)}")
        result = await resume_run(run_id, {"decisions": decisions}, principal=DEMO_REVIEWER)

    print_stages(await services.repository.list_events(run_id))
    return run_id, result["report"]


def print_outcome(report: dict, before: dict[str, str], after: dict[str, str]) -> None:
    section("outcome")
    print(f"  status            {report['status']}")
    if report["blocked_by"]:
        override = report["override"]
        print(
            f"  blocked by        {report['blocked_by']}"
            + (f" (overridden by {override['actor_id']}: {override['reason']})" if override else "")
        )
    print(f"  document type     {report['document_type']}")
    print(f"  affected keys     {report['affected_keys']}")
    print(f"  committed keys    {report['committed_keys']}")
    if report["stale_keys"]:
        print(f"  stale keys        {report['stale_keys']}")
    if report["unsupported_count"]:
        print(
            f"  abstained on      {report['unsupported_count']} claim(s) the source text "
            "does not support -- kept for audit, never cited, never committed"
        )

    # "Untouched" as a checkable claim rather than an adjective: the content hash of every
    # row this run did not commit is compared before and after, and printed.
    untouched = sorted(k for k in before if k in after and before[k] == after[k])
    moved = sorted(k for k in after if before.get(k) != after[k])
    print(f"  rows byte-identical before/after   {untouched or '(register was empty)'}")
    if moved:
        print(f"  rows whose content hash moved      {moved}")

    rules = report["rules"]
    print(
        f"  rules             {rules['rules_completed']}/{rules['rules_expected']} evaluated, "
        f"{rules['rules_failed']} failed, {rules['violation']} violation, "
        f"{rules['pass']} pass, {rules['insufficient_evidence']} insufficient evidence"
    )
    print(
        f"  clean             {rules['clean']}"
        + (f"  (confirmed by {rules['reviewed_by']})" if rules["reviewed_by"] else "")
        + ("" if rules["evaluation_complete"] else "  (evaluation INCOMPLETE)")
    )
    for finding in rules["findings"]:
        dismissed = (
            f"  (DISMISSED by {finding['decided_by']})"
            if finding["review_decision"] == "dismissed"
            else ""
        )
        print(
            f"    {finding['rule_code']} [{finding['target_key']}] "
            f"{finding['system_verdict']}: {finding['rationale']}{dismissed}"
        )
    for conflict in report["open_conflicts"]:
        print(f"  OPEN CONFLICT     {conflict['key']} [{conflict['kind']}] {conflict['rationale']}")

    injection = report["injection"]
    if injection["flagged"]:
        for entry in injection["quarantined_blocks"]:
            print(
                f"  QUARANTINED       block {entry['index']} "
                f"(page {entry['page']}, {entry['extraction_method']}): "
                f"{', '.join(entry['signals'])}"
            )
        print(f"                    {injection['detection_is_telemetry']}")

    cost = report["cost"]
    print(
        f"  cost / latency    {cost['total_duration_ms']} ms, "
        f"${cost['total_spend_usd']:.6f} across {cost['total_tokens_in']} in / "
        f"{cost['total_tokens_out']} out tokens "
        f"(price table {cost['price_table_version']})"
        + ("  [UNPRICED USAGE]" if cost["has_unpriced_usage"] else "")
    )
    for stage in cost["stages"]:
        if stage["tokens_in"] or stage["tokens_out"] or stage["duration_ms"]:
            extras = ""
            if stage["cache_hits"]:
                extras += f"  cache_hit x{stage['cache_hits']}"
            if stage["external_service_calls"]:
                extras += f"  external x{stage['external_service_calls']}"
            if stage["attempt_count"] > 1:
                extras += f"  attempts x{stage['attempt_count']}"
            print(
                f"    {stage['stage']:<24} {stage['duration_ms']:>4} ms  "
                f"{stage['tokens_in']:>5} in / {stage['tokens_out']:>4} out  "
                f"${stage['spend_usd']:.6f}{extras}"
            )


async def register(collection_id: UUID) -> dict[str, dict]:
    services = await get_services()
    return {
        item.key: {"value": item.value, "state": item.state, "version": item.version}
        for item in await services.repository.list_register(collection_id)
    }


def obligations(scoped_keys: list[str]) -> set[str]:
    """Register keys carry their agreement; the demo corpus is one unnamed agreement."""
    return {RegisterKey.parse(key).key for key in scoped_keys}


def days(current: dict[str, dict], key: str) -> int | None:
    entry = current.get(key)
    return entry["value"].get("days") if entry else None


def all_verdicts(report: dict) -> list[str]:
    return [finding["system_verdict"] for finding in report["rules"]["findings"]]


def verdicts(report: dict, code: str) -> list[str]:
    return [
        finding["system_verdict"]
        for finding in report["rules"]["findings"]
        if finding["rule_code"] == code
    ]


def approve_all(item: ReviewItem) -> str:
    return "approved"


async def main() -> None:
    services = await get_services()
    collection_id = await services.repository.create_collection("Northstar vendor file")
    ruleset = await services.repository.put_ruleset(
        parse_ruleset(json.loads((PACK / "rules.json").read_text()), collection_id)
    )
    banner("doctask offline demo -- 7 documents, 4 formats, one obligations register")
    print(f"  playbook   {ruleset.name} v{ruleset.version} -> {[r.code for r in ruleset.rules]}")
    print("  model      FakeLLM (deterministic, offline -- no API key, no GPU, no server)")
    print(f"  reviewer   {DEMO_REVIEWER.actor_id} ({DEMO_REVIEWER.role})")

    async def step(document, key, decide, **kwargs) -> tuple[UUID, dict, dict, dict]:
        before = await register_hashes(collection_id)
        run_id, report = await drive(collection_id, document, key, decide, **kwargs)
        after = await register_hashes(collection_id)
        print_outcome(report, before, after)
        section("assertions")
        return run_id, report, before, after

    # ------------------------------------------------------------ 1. MSA (PDF)
    banner("1/7  Master services agreement (PDF) -- baseline register")
    _, report, _, _ = await step(MSA, "demo-msa", approve_all)
    current = await register(collection_id)
    check("MSA typed as a master agreement", report["document_type"] == "master_agreement",
          report["document_type"])
    check("every playbook rule cleared the MSA", set(all_verdicts(report)) == {"pass"},
          str(sorted(set(all_verdicts(report)))))
    check("payment_due_days = 30", days(current, "payment_due_days") == 30,
          str(days(current, "payment_due_days")))
    check("liability_cap = USD 250,000",
          current.get("liability_cap", {}).get("value", {}).get("amount") == "$250,000",
          str(current.get("liability_cap", {}).get("value")))
    check("notice_days = 60", days(current, "notice_days") == 60,
          str(days(current, "notice_days")))
    # Every rule judged the register itself, not only the document it came from. A rule
    # aimed at a key the register cannot hold silently never runs, and reports nothing.
    judged = {
        finding["rule_code"]
        for finding in report["rules"]["findings"]
        if finding["target_kind"] == "register_item"
    }
    check("all three rules judged the register, not just the document",
          judged == {"PAY-01", "LIA-01", "TERM-01"}, str(sorted(judged)))

    # ------------------------------------------------------ 2. Amendment (PDF)
    banner("2/7  Amendment No. 1 (PDF) -- supersession proposed, never auto-applied")
    _, report, before, after = await step(AMENDMENT, "demo-amendment", approve_all)
    current = await register(collection_id)
    check("amendment typed as an amendment", report["document_type"] == "amendment",
          report["document_type"])
    check("payment_due_days now 45", days(current, "payment_due_days") == 45,
          str(days(current, "payment_due_days")))
    check("notice_days now 90", days(current, "notice_days") == 90,
          str(days(current, "notice_days")))
    check("payment_due_days is at version 2", current["payment_due_days"]["version"] == 2,
          str(current["payment_due_days"]["version"]))
    check("liability_cap untouched by the amendment",
          "liability_cap" not in obligations(report["committed_keys"]),
          str(report["committed_keys"]))
    liability_key = RegisterKey("", "liability_cap").text
    check("liability_cap row is byte-identical before and after",
          before.get(liability_key) == after.get(liability_key) is not None,
          f"{before.get(liability_key)} -> {after.get(liability_key)}")

    # -------------------------------------------------------- 3. Invoice (PDF)
    banner("3/7  Invoice (PDF) -- NET 10 contradicts the amended 45-day term")

    def decide_invoice(item: ReviewItem) -> str:
        # An invoice does not amend a contract, and the MSA says so in Section 4.3.
        # The finding is real and gets approved; the payment term it asserts does not.
        return "rejected" if item.target_key == "payment_due_days" else "approved"

    _, report, _, _ = await step(INVOICE, "demo-invoice", decide_invoice)
    current = await register(collection_id)
    check("invoice typed as an invoice", report["document_type"] == "invoice",
          report["document_type"])
    check("PAY-01 raised a source violation on NET 10", "violation" in verdicts(report, "PAY-01"),
          str(verdicts(report, "PAY-01")))
    check("TERM-01 did not fire on an invoice that says nothing about notice",
          "violation" not in verdicts(report, "TERM-01"), str(verdicts(report, "TERM-01")))
    check("the rejected 10-day term did not reach the register",
          days(current, "payment_due_days") == 45, str(days(current, "payment_due_days")))
    check("payment_due_days did not move a version",
          current["payment_due_days"]["version"] == 2,
          str(current["payment_due_days"]["version"]))
    check("the invoice total was recorded",
          current.get("invoice_amount_due", {}).get("value", {}).get("amount") == "$18,500.00",
          str(current.get("invoice_amount_due", {}).get("value")))

    # ------------------------------------------------------------ 4. DPA (DOCX)
    banner("4/7  Data processing addendum (DOCX) -- mixed-format ingestion, no-op update")
    snapshot = await register(collection_id)
    _, report, before, after = await step(DPA, "demo-dpa", approve_all, document_type="policy")
    current = await register(collection_id)
    check("DPA parsed as DOCX", report["status"] != "failed", report["status"])
    check("DPA changed neither payment nor liability",
          all(snapshot.get(k) == current.get(k) for k in ("payment_due_days", "liability_cap")),
          f"payment {days(current, 'payment_due_days')}, "
          f"liability {current.get('liability_cap', {}).get('value')}")
    check("every stored register row is byte-identical before and after the DPA",
          before == after, f"{len(before)} rows")

    # --------------------------------------------------------- 5. Notice (TXT)
    banner("5/7  Operational notice (TXT) -- low classification confidence escalates")
    _, report, before, after = await step(NOTICE, "demo-notice", approve_all,
                                          document_type="unknown")
    check("notice parsed as TXT", report["status"] != "failed", report["status"])
    check("notice committed nothing", not report["committed_keys"], str(report["committed_keys"]))
    check("register is byte-identical after the notice", before == after, f"{len(before)} rows")

    # --------------------------------------------------- 6. Portal policy (TXT)
    banner("6/7  Vendor portal policy (TXT) -- an injected instruction is contained")
    _, report, before, after = await step(PORTAL, "demo-portal", approve_all)
    current = await register(collection_id)
    quarantined = report["injection"]["quarantined_blocks"]
    signals = {signal for entry in quarantined for signal in entry["signals"]}
    check("the hostile paragraph was flagged", report["injection"]["flagged"] is True)
    check("exactly one block was withheld, not the whole document",
          len(quarantined) == 1, f"{len(quarantined)} of {report['register_items']} rows stand")
    check("the instruction was recognised for what it is",
          {"override_instructions", "approval_demand", "instruction_to_agent"} <= signals,
          str(sorted(signals)))
    check("the 5-day term buried in that paragraph never reached the register",
          days(current, "payment_due_days") == 45, str(days(current, "payment_due_days")))
    # The containment claim in its strongest form: not "the register rejected it" but
    # "no such fact was ever extracted", because that block was never sent to the model.
    payment_facts = await services.repository.get_active_facts(
        collection_id, ["payment_due_days"]
    )
    check("no stored fact anywhere carries the injected 5-day term",
          all(fact.value.get("days") != 5 for fact in payment_facts),
          f"{len(payment_facts)} payment fact(s): "
          f"{sorted({fact.value.get('days') for fact in payment_facts})}")
    check("a withheld block makes the run un-clean, whatever the rules said",
          report["rules"]["clean"] is False, str(report["rules"]["clean"]))
    check("the register is byte-identical after the injection attempt",
          before == after, f"{len(before)} rows")

    # ----------------------------------------------------- 7. Statement of work
    banner("7/7  Statement of work (TXT) -- an unsupported claim is abstained on")
    _, report, before, after = await step(SOW, "demo-sow", approve_all)
    current = await register(collection_id)
    stages = {stage["stage"]: stage for stage in report["cost"]["stages"]}
    check("the extractor's claim failed grounding and a repair was attempted",
          "retry_extract" in stages, "retry_extract ran" if "retry_extract" in stages else "absent")
    check("extraction ran twice: the original attempt and the repair",
          stages.get("extract_facts", {}).get("attempt_count") == 2,
          str(stages.get("extract_facts", {}).get("attempt_count")))
    check("the claim was abstained on rather than committed",
          report["unsupported_count"] >= 1, str(report["unsupported_count"]))
    check("the unsupported payment term did not enter the register",
          days(current, "payment_due_days") == 45, str(days(current, "payment_due_days")))
    check("the register is byte-identical after the abstention",
          before == after, f"{len(before)} rows")

    # ------------------------------------------------------------------ wrap-up
    banner("Final register")
    for item in await services.repository.list_register(collection_id):
        print(
            f"  {item.key:<20} v{item.version} [{item.state}] {item.value}"
            f"\n  {'':<20} {len(item.citation_fact_ids)} citation(s), "
            f"content_hash {item.content_hash[:16]}..."
        )

    print()
    if failures:
        print(f"DEMO FAILED: {len(failures)} claim(s) no longer hold")
        for failure in failures:
            print(f"  - {failure}")
        sys.exit(1)
    print("DEMO OK: every claim above was checked against stored state, not printed at it")
    print("Next: `make demo-crash` kills a run mid-flight and proves it resumes exactly once.")


if __name__ == "__main__":
    asyncio.run(main())

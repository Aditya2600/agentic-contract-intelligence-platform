"""Offline demo of the obligations register, over the real synthetic contract pack.

Five files, three formats, one register:

    1. MSA PDF        -> payment 30, liability USD 250,000, notice 60
    2. Amendment PDF  -> payment 30 -> 45, notice 60 -> 90
    3. Invoice PDF    -> NET 10 breaks PAY-01; the contractual term stays 45
    4. DPA DOCX       -> parses, and changes neither payment nor liability
    5. Notice TXT     -> parses, and changes nothing at all

Every arrow above is asserted, not narrated: this script exits non-zero if the pipeline
stops producing any of them. That is the point of running it -- a demo that only prints
is a demo that can quietly start lying.

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

PACK = Path("realistic_synthetic_demo_pack")

PDF = "application/pdf"
WORD = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PLAIN = "text/plain"

MSA = ("01_Master_Services_Agreement_MSA-2026-014.pdf", PDF)
AMENDMENT = ("02_Amendment_No_1_AMD-2026-014-01.pdf", PDF)
INVOICE = ("03_Invoice_INV-2026-0417.pdf", PDF)
DPA = ("04_Data_Processing_Addendum_DPA-2026-014-A.docx", WORD)
NOTICE = ("05_Operational_Notice_OPS-NOTICE-2026-0528.txt", PLAIN)

# The demo runs in-process, so the operator running it is the reviewer. Over the
# network this identity would come from a credential instead: see doctask.auth.
DEMO_REVIEWER = Principal(actor_id="demo-reviewer", role=REVIEWER)

failures: list[str] = []


def check(claim: str, condition: bool, detail: str = "") -> None:
    """Assert one thing the demo promises, and keep going so the run shows all of them."""
    print(f"  {'PASS' if condition else 'FAIL'}  {claim}" + (f"  [{detail}]" if detail else ""))
    if not condition:
        failures.append(f"{claim} ({detail})" if detail else claim)


def _print_report(title: str, report: dict) -> None:
    print(f"\n=== {title}")
    print(f"  status          : {report['status']}")
    if report["blocked_by"]:
        override = report["override"]
        print(
            f"  blocked by      : {report['blocked_by']}"
            + (f" (overridden by {override['actor_id']}: {override['reason']})" if override else "")
        )
    print(f"  document type   : {report['document_type']}")
    print(f"  affected keys   : {report['affected_keys']}")
    print(f"  committed keys  : {report['committed_keys']}")
    print(f"  unchanged keys  : {report['unchanged_keys']}")
    rules = report["rules"]
    print(
        f"  rules           : {rules['rules_completed']}/{rules['rules_expected']} evaluated, "
        f"{rules['rules_failed']} failed, {rules['violation']} violation, {rules['pass']} pass, "
        f"{rules['insufficient_evidence']} insufficient evidence"
    )
    print(
        f"  clean           : {rules['clean']}"
        + (f"  (confirmed by {rules['reviewed_by']})" if rules["reviewed_by"] else "")
        + ("" if rules["evaluation_complete"] else "  (evaluation INCOMPLETE)")
    )
    for finding in rules["findings"]:
        print(
            f"    {finding['rule_code']} [{finding['target_key']}] "
            f"{finding['system_verdict']}: {finding['rationale']}"
            + (
                f"  (DISMISSED by {finding['decided_by']})"
                if finding["review_decision"] == "dismissed"
                else ""
            )
        )
    for conflict in report["open_conflicts"]:
        print(f"  OPEN CONFLICT   : {conflict['key']} [{conflict['kind']}] {conflict['rationale']}")


async def _run(
    collection_id: UUID,
    document: tuple[str, str],
    key: str,
    decide,
    *,
    document_type: str | None = None,
) -> dict:
    """Extract one real file and drive its run to a report, answering every gate."""
    services = await get_services()
    filename, mime_type = document
    extracted = await extract_document(
        filename=filename, mime_type=mime_type, data=(PACK / filename).read_bytes()
    )
    methods = sorted(extracted.methods)
    pages = sorted({block.page for block in extracted.blocks if block.page})
    print(
        f"  parsed  : {len(extracted.blocks)} blocks via {', '.join(methods)}"
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
    )

    # Gates, in whatever order this document reaches them: document type, register
    # proposals, deliverable findings, blocker override.
    while "report" not in result:
        gate = result["__interrupt__"][0].value["kind"]
        if gate == "document_classification":
            chosen = document_type or "unknown"
            print(f"  gate    : classification unclear; reviewer files it as {chosen}")
            result = await resume_run(
                run_id, {"document_type": chosen}, principal=DEMO_REVIEWER
            )
            continue
        if gate == "blocker_override":
            # The demo reviewer does not override. Blocked means blocked.
            print("  BLOCKED : upheld blocker findings; nothing committed")
            result = await resume_run(run_id, {"override": False, "reason": ""}, principal=DEMO_REVIEWER)
            continue
        pending = [
            item
            for item in await services.repository.list_review_items(run_id)
            if item.state == "pending"
        ]
        for item in pending:
            if item.kind == "finding":
                print(
                    f"  finding : {item.target_key} on {item.payload['target_key']} "
                    f"[{item.payload['severity']}] "
                    f"{item.payload['system_verdict']}: {item.payload['rationale']}"
                )
                continue
            if item.kind == "deliverable_confirmation":
                print(
                    f"  confirm : {len(item.payload['evaluations'])} deliverable "
                    "evaluation(s), none adverse -- a human signs off, the run does not "
                    "assume it"
                )
                continue
            if item.kind == "scope_question":
                print(f"  question: {item.target_key} -- {item.payload['reason']}")
                continue
            conflict = item.payload.get("conflict")
            marker = f" [{conflict['kind']}]" if conflict else ""
            print(f"  proposal: {item.target_key}{marker} -> {item.payload['after']['value']}")
        result = await resume_run(
            run_id,
            {"decisions": {str(item.id): decide(item) for item in pending}},
            principal=DEMO_REVIEWER,
        )
    return result["report"]


async def _register(collection_id: UUID) -> dict[str, dict]:
    services = await get_services()
    return {
        item.key: {"value": item.value, "state": item.state, "version": item.version}
        for item in await services.repository.list_register(collection_id)
    }


def _obligations(scoped_keys: list[str]) -> set[str]:
    """Register keys carry their agreement; the demo corpus is one unnamed agreement."""
    return {RegisterKey.parse(key).key for key in scoped_keys}


def _days(register: dict[str, dict], key: str) -> int | None:
    entry = register.get(key)
    return entry["value"].get("days") if entry else None


def _all_verdicts(report: dict) -> list[str]:
    return [finding["system_verdict"] for finding in report["rules"]["findings"]]


def _verdicts(report: dict, code: str) -> list[str]:
    return [
        finding["system_verdict"]
        for finding in report["rules"]["findings"]
        if finding["rule_code"] == code
    ]


def _approve_all(item: ReviewItem) -> str:
    return "approved"


async def main() -> None:
    services = await get_services()
    collection_id = await services.repository.create_collection("Northstar vendor file")
    ruleset = await services.repository.put_ruleset(
        parse_ruleset(json.loads((PACK / "rules.json").read_text()), collection_id)
    )
    print(f"Playbook: {ruleset.name} v{ruleset.version} -> {[r.code for r in ruleset.rules]}")

    # ---------------------------------------------------------------- 1. MSA (PDF)
    print("\n--- 1. Master services agreement (PDF)")
    report = await _run(collection_id, MSA, "demo-msa", _approve_all)
    _print_report("MSA committed", report)
    register = await _register(collection_id)
    check("MSA typed as a master agreement", report["document_type"] == "master_agreement",
          report["document_type"])
    check("every playbook rule cleared the MSA", set(_all_verdicts(report)) == {"pass"},
          str(sorted(set(_all_verdicts(report)))))
    check("payment_due_days = 30", _days(register, "payment_due_days") == 30,
          str(_days(register, "payment_due_days")))
    check("liability_cap = USD 250,000",
          register.get("liability_cap", {}).get("value", {}).get("amount") == "$250,000",
          str(register.get("liability_cap", {}).get("value")))
    check("notice_days = 60", _days(register, "notice_days") == 60,
          str(_days(register, "notice_days")))
    # Every rule judged the register itself, not only the document it came from. A rule
    # aimed at a key the register cannot hold silently never runs, and reports nothing.
    judged = {
        finding["rule_code"]
        for finding in report["rules"]["findings"]
        if finding["target_kind"] == "register_item"
    }
    check("all three rules judged the register, not just the document",
          judged == {"PAY-01", "LIA-01", "TERM-01"}, str(sorted(judged)))

    # ---------------------------------------------------------- 2. Amendment (PDF)
    print("\n--- 2. Amendment No. 1 (PDF): payment 30 -> 45, notice 60 -> 90")
    report = await _run(collection_id, AMENDMENT, "demo-amendment", _approve_all)
    _print_report("Amendment reviewed", report)
    register = await _register(collection_id)
    check("amendment typed as an amendment", report["document_type"] == "amendment",
          report["document_type"])
    check("payment_due_days now 45", _days(register, "payment_due_days") == 45,
          str(_days(register, "payment_due_days")))
    check("notice_days now 90", _days(register, "notice_days") == 90,
          str(_days(register, "notice_days")))
    check("payment_due_days is at version 2", register["payment_due_days"]["version"] == 2,
          str(register["payment_due_days"]["version"]))
    check("liability_cap untouched by the amendment",
          "liability_cap" not in _obligations(report["committed_keys"]),
          str(report["committed_keys"]))

    # ------------------------------------------------------------ 3. Invoice (PDF)
    print("\n--- 3. Invoice (PDF): NET 10 contradicts the amended 45-day term")

    def decide_invoice(item: ReviewItem) -> str:
        # An invoice does not amend a contract, and the MSA says so in Section 4.3.
        # The finding is real and gets approved; the payment term it asserts does not.
        return "rejected" if item.target_key == "payment_due_days" else "approved"

    report = await _run(collection_id, INVOICE, "demo-invoice", decide_invoice)
    _print_report("Invoice reviewed", report)
    register = await _register(collection_id)
    check("invoice typed as an invoice", report["document_type"] == "invoice",
          report["document_type"])
    check("PAY-01 raised a source violation on NET 10", "violation" in _verdicts(report, "PAY-01"),
          str(_verdicts(report, "PAY-01")))
    check("TERM-01 did not fire on an invoice that says nothing about notice",
          "violation" not in _verdicts(report, "TERM-01"), str(_verdicts(report, "TERM-01")))
    check("contractual payment term is still 45", _days(register, "payment_due_days") == 45,
          str(_days(register, "payment_due_days")))
    check("payment_due_days did not move a version",
          register["payment_due_days"]["version"] == 2,
          str(register["payment_due_days"]["version"]))
    check("the invoice total was recorded",
          register.get("invoice_amount_due", {}).get("value", {}).get("amount") == "$18,500.00",
          str(register.get("invoice_amount_due", {}).get("value")))

    # -------------------------------------------------------------- 4. DPA (DOCX)
    print("\n--- 4. Data processing addendum (DOCX): mixed-format ingestion")
    before = await _register(collection_id)
    report = await _run(collection_id, DPA, "demo-dpa", _approve_all, document_type="policy")
    _print_report("DPA reviewed", report)
    after = await _register(collection_id)
    check("DPA parsed as DOCX", report["status"] != "failed", report["status"])
    check("DPA changed neither payment nor liability",
          all(before.get(key) == after.get(key) for key in ("payment_due_days", "liability_cap")),
          f"payment {_days(after, 'payment_due_days')}, liability "
          f"{after.get('liability_cap', {}).get('value')}")

    # ----------------------------------------------------------- 5. Notice (TXT)
    print("\n--- 5. Operational notice (TXT): operational only")
    before = await _register(collection_id)
    report = await _run(collection_id, NOTICE, "demo-notice", _approve_all, document_type="unknown")
    _print_report("Notice reviewed", report)
    after = await _register(collection_id)
    check("notice parsed as TXT", report["status"] != "failed", report["status"])
    check("notice committed nothing", not report["committed_keys"], str(report["committed_keys"]))
    check("register is byte-identical after the notice", before == after)

    print("\n=== Final register")
    for item in await services.repository.list_register(collection_id):
        print(f"  {item.key:20} v{item.version} [{item.state}] {item.value}")

    print()
    if failures:
        print(f"DEMO FAILED: {len(failures)} claim(s) no longer hold")
        for failure in failures:
            print(f"  - {failure}")
        sys.exit(1)
    print("DEMO OK: every claim above was checked against the register, not printed at it")


if __name__ == "__main__":
    asyncio.run(main())

"""The MCP surface is a real second front door, exercised end to end through it.

No UI, no REST client, no direct graph or repository calls: every step below goes
through the actual `mcp.streamable_http_app()` ASGI app -- the same bearer-token
middleware, the same tool dispatch -- driven by the real MCP client library over an
in-process transport. What this proves that a unit test calling Python functions
directly could not: a service credential can upload and read, only a reviewer
credential can decide, a stale or wrong basis is refused, and the report a caller
reads back at the end is the same grounded report the graph itself produced.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
from uuid import UUID, uuid4

import docx
import httpx2
import pytest
from mcp.client.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp_types import CallToolResult

from doctask import mcp_server, runtime
from doctask.config import settings
from doctask.services.extraction import extract_document

MSA = """MASTER SERVICES AGREEMENT

Payment is due within 30 calendar days of receipt.

Liability is capped at $250,000.
"""

REVIEWER_TOKEN = "mcp-reviewer-secret"
SERVICE_TOKEN = "mcp-service-secret"
BASE_URL = "http://localhost:8000"


@pytest.fixture(autouse=True)
async def tokens_and_services(monkeypatch):
    monkeypatch.setattr(settings, "reviewer_tokens", f"{REVIEWER_TOKEN}:alice")
    monkeypatch.setattr(settings, "service_tokens", f"{SERVICE_TOKEN}:ingest-bot")
    monkeypatch.setattr(settings, "repository", "memory")
    yield
    await runtime.shutdown_services()


def _basis_for(item: dict) -> dict:
    """What `decide_review_items` requires echoed back, read off the same dict
    `list_review_items` already returned -- exactly what a real caller has in hand."""
    payload = item["payload"]
    if item["kind"] in {"register_update", "conflict", "scope_question"}:
        before = payload.get("before")
        return {
            "version": before.get("version") if before else None,
            "content_hash": before.get("content_hash") if before else None,
        }
    if item["kind"] in {"finding", "deliverable_confirmation"}:
        return {"basis_hash": payload.get("basis_hash"), "ruleset_hash": payload.get("ruleset_hash")}
    return {}


def _obj(result: CallToolResult) -> dict:
    """A tool's `dict` return value. It round-trips as one JSON text content block, not
    `structured_content` -- these tools declare no output schema, so the wire format is
    plain JSON text, same as any other caller of this server would see."""
    return json.loads(result.content[0].text)


def _rows(result: CallToolResult) -> list[dict]:
    """A tool's `list[dict]` return value: one JSON text content block per row."""
    return [json.loads(item.text) for item in result.content]


def _mcp_client(app, token: str) -> Client:
    http = httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app),
        base_url=BASE_URL,
        headers={"Authorization": f"Bearer {token}"},
    )
    return Client(server=streamable_http_client(f"{BASE_URL}/mcp", http_client=http))


PLAYBOOK_V1 = {
    "name": "Northstar Vendor Contract Playbook",
    "rules": [
        {
            "code": "PAY-01",
            "severity": "major",
            "scope": "both",
            "keys": ["payment_due_days"],
            "text": "Payment terms must be at least 30 calendar days after receipt.",
        },
    ],
}
# Same name, same rule code -- different text, so the same collection's active
# ruleset is genuinely different content, not just a re-upload.
PLAYBOOK_V2 = {
    **PLAYBOOK_V1,
    "rules": [
        {
            **PLAYBOOK_V1["rules"][0],
            "text": "Payment terms must be at least 45 calendar days after receipt.",
        },
    ],
}


async def test_create_collection_and_put_ruleset_are_idempotent_then_the_workflow_runs() -> None:
    """A program configuring the whole system from nothing: a collection, a playbook,
    both installed the way a retried call has to behave, then a real run against what
    it just configured -- proving the installed ruleset is what actually judged it."""
    app = mcp_server.mcp.streamable_http_app()
    async with (
        app.router.lifespan_context(app),
        _mcp_client(app, SERVICE_TOKEN) as service,
        _mcp_client(app, REVIEWER_TOKEN) as reviewer,
    ):
        # --- create_collection: idempotent on slug, refuses a conflicting payload -----
        created = await service.call_tool(
            "create_collection", {"name": "Northstar vendor file"}
        )
        collection_id = _obj(created)["collection_id"]

        retried = await service.call_tool(
            "create_collection", {"name": "Northstar vendor file"}
        )
        assert _obj(retried)["collection_id"] == collection_id, (
            "an identical request must return the existing collection"
        )

        conflicting = await service.call_tool(
            "create_collection",
            {"name": "somebody else's collection", "slug": "northstar-vendor-file"},
        )
        assert conflicting.is_error, "a conflicting name for an existing slug was accepted"

        # --- put_ruleset: content-idempotent, changed content needs if_match ----------
        installed = await service.call_tool(
            "put_ruleset", {"collection_id": collection_id, "playbook": PLAYBOOK_V1}
        )
        first = _obj(installed)
        assert (first["version"], first["created"]) == (1, True)

        replayed = await service.call_tool(
            "put_ruleset", {"collection_id": collection_id, "playbook": PLAYBOOK_V1}
        )
        again = _obj(replayed)
        assert (again["version"], again["created"]) == (1, False), (
            "identical content must be a no-op at the same version"
        )

        blind_change = await service.call_tool(
            "put_ruleset", {"collection_id": collection_id, "playbook": PLAYBOOK_V2}
        )
        assert blind_change.is_error, "changed content installed without if_match"

        wrong_match = await service.call_tool(
            "put_ruleset",
            {"collection_id": collection_id, "playbook": PLAYBOOK_V2, "if_match": 99},
        )
        assert wrong_match.is_error, "a wrong if_match was accepted"

        changed = await service.call_tool(
            "put_ruleset",
            {"collection_id": collection_id, "playbook": PLAYBOOK_V2, "if_match": 1},
        )
        second = _obj(changed)
        assert (second["version"], second["created"]) == (2, True)

        # --- the workflow runs against exactly what was just configured ---------------
        started = await service.call_tool(
            "start_obligations_run",
            {
                "collection_id": collection_id,
                "idempotency_key": f"msa-{uuid4()}",
                "filename": "msa.txt",
                "text": MSA,
            },
        )
        run_id = _obj(started)["run_id"]
        result = _obj(started)["result"]

        while "report" not in result:
            listed = await reviewer.call_tool("list_review_items", {"run_id": run_id})
            pending = [item for item in _rows(listed) if item["state"] == "pending"]
            assert pending, "the graph paused with nothing for a reviewer to decide"
            decided = await reviewer.call_tool(
                "decide_review_items",
                {
                    "run_id": run_id,
                    "decisions": {item["id"]: "approved" for item in pending},
                    "basis": {item["id"]: _basis_for(item) for item in pending},
                    "idempotency_key": str(uuid4()),
                },
            )
            assert not decided.is_error, decided.content
            result = _obj(decided)

        report = result["report"]
        assert report["status"] == "committed"
        assert "::liability_cap" in report["committed_keys"]
        assert "::payment_due_days" in report["committed_keys"]
        # PLAYBOOK_V2, the version actually installed, is what judged this run.
        assert report["rules"]["rules_expected"] >= 1
        assert report["rules"]["rules_completed"] == report["rules"]["rules_expected"]

        findings = await reviewer.call_tool("list_findings", {"run_id": run_id})
        assert any(row["rule_code"] == "PAY-01" for row in _rows(findings))


async def test_upload_decide_resume_and_read_the_grounded_report_over_mcp() -> None:
    # A fresh ASGI app, not the module-level `mcp_server.app` other tests already drove:
    # its session manager may only be `.run()` once per process lifetime.
    app = mcp_server.mcp.streamable_http_app()
    async with (
        app.router.lifespan_context(app),
        _mcp_client(app, SERVICE_TOKEN) as service,
        _mcp_client(app, REVIEWER_TOKEN) as reviewer,
    ):
        # --- a service credential may feed a document in and read, never decide ------
        created = await service.call_tool("create_collection", {"name": "acme"})
        collection_id = _obj(created)["collection_id"]

        started = await service.call_tool(
            "start_obligations_run",
            {
                "collection_id": collection_id,
                "idempotency_key": f"msa-{uuid4()}",
                "filename": "msa.txt",
                "text": MSA,
            },
        )
        run_id = _obj(started)["run_id"]
        result = _obj(started)["result"]

        denied = await service.call_tool(
            "decide_review_items",
            {"run_id": run_id, "decisions": {}, "idempotency_key": str(uuid4())},
        )
        assert denied.is_error, "a service credential decided a review item"

        # --- the reviewer approves one obligation and rejects the other ---------------
        while "report" not in result:
            listed = await reviewer.call_tool("list_review_items", {"run_id": run_id})
            pending = [item for item in _rows(listed) if item["state"] == "pending"]
            assert pending, "the graph paused with nothing for a reviewer to decide"

            stale = await reviewer.call_tool(
                "decide_review_items",
                {
                    "run_id": run_id,
                    "decisions": {pending[0]["id"]: "approved"},
                    "basis": {pending[0]["id"]: {"version": 999, "content_hash": "wrong"}},
                    "idempotency_key": str(uuid4()),
                },
            )
            assert stale.is_error, "a decision against a wrong basis was applied anyway"

            decisions = {
                item["id"]: (
                    "rejected" if item["target_key"] == "::payment_due_days" else "approved"
                )
                for item in pending
            }
            decided = await reviewer.call_tool(
                "decide_review_items",
                {
                    "run_id": run_id,
                    "decisions": decisions,
                    "basis": {item["id"]: _basis_for(item) for item in pending},
                    "idempotency_key": str(uuid4()),
                },
            )
            assert not decided.is_error, decided.content
            result = _obj(decided)

        report = result["report"]

        # --- the grounded report: one obligation committed, the other rejected --------
        assert report["status"] == "committed"
        assert "::liability_cap" in report["committed_keys"]
        assert "::payment_due_days" not in report["committed_keys"]

        # --- read access the MCP surface adds: status, register, snapshot diff --------
        status = await reviewer.call_tool("get_run_status", {"run_id": run_id})
        assert _obj(status)["status"] == "committed"
        assert _obj(status)["pending_review_items"] == 0

        register = await service.call_tool("list_register", {"collection_id": collection_id})
        keys = {row["key"] for row in _rows(register)}
        assert "liability_cap" in keys
        assert "payment_due_days" not in keys

        diff = await service.call_tool("get_snapshot_diff", {"run_id": run_id})
        changed = {row["key"]: row for row in _rows(diff)}
        assert changed.keys() == {"::liability_cap"}
        assert changed["::liability_cap"]["old_value"] is None
        assert changed["::liability_cap"]["new_value"] == {
            "amount": "$250,000",
            "currency": "USD",
        }


# ---------------------------------------------------------------------------------
# The remaining gaps: binary ingestion, discovering a run nobody started, export with
# its evidence attached, and refusals a caller can actually tell apart.
# ---------------------------------------------------------------------------------

# Same three obligations as `data/sample_data/vendor_msa.txt`, as DOCX paragraphs rather
# than a text file -- the binary format `start_obligations_run` cannot reach at all.
MSA_PARAGRAPHS = [
    "MASTER SERVICES AGREEMENT",
    (
        "This Master Services Agreement is entered into between Acme Buyer LLC "
        "and Northstar Vendor Inc."
    ),
    "Payment is due within 30 calendar days of receipt. Liability is capped at $250,000.",
    "Either party may terminate the agreement with 60 days' written notice.",
]


def _msa_docx_bytes() -> bytes:
    document = docx.Document()
    for paragraph in MSA_PARAGRAPHS:
        document.add_paragraph(paragraph)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# A single blocker rule the 30-day payment term is guaranteed to violate (45 > 30).
# The same rule and document combination `tests/test_review_authority.py` already
# proves reaches all three gates: item-level review, deliverable finding review, then
# the blocker override.
BLOCKING_PLAYBOOK = {
    "name": "Blocking playbook",
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


def _decide_mixed(item: dict) -> str:
    """Reject the one register proposal named `liability_cap`; approve everything
    else -- the other two proposals, and every finding / deliverable-confirmation item
    across every round, which is what upholds the blocker and confirms the deliverable
    so the run actually reaches the override gate instead of stopping short of it."""
    if item["kind"] == "register_update" and item["target_key"].endswith("liability_cap"):
        return "rejected"
    return "approved"


async def test_the_full_lifecycle_drives_over_mcp_alone() -> None:
    """Ingest a binary document, run, discover the run by listing, enumerate items,
    decide at both gates, commit, export -- with no channel but MCP tool calls."""
    app = mcp_server.mcp.streamable_http_app()
    docx_bytes = _msa_docx_bytes()
    async with (
        app.router.lifespan_context(app),
        _mcp_client(app, SERVICE_TOKEN) as service,
        _mcp_client(app, REVIEWER_TOKEN) as reviewer,
    ):
        created = await service.call_tool("create_collection", {"name": "full-lifecycle"})
        collection_id = _obj(created)["collection_id"]

        installed = await service.call_tool(
            "put_ruleset", {"collection_id": collection_id, "playbook": BLOCKING_PLAYBOOK}
        )
        assert _obj(installed)["created"] is True

        # --- ingest a binary document: the gap `start_obligations_run` cannot reach ---
        uploaded = await service.call_tool(
            "upload_obligations_document",
            {
                "collection_id": collection_id,
                "idempotency_key": f"msa-{uuid4()}",
                "filename": "msa.docx",
                "content_base64": base64.b64encode(docx_bytes).decode("ascii"),
                "mime_type": DOCX_MIME,
            },
        )
        assert not uploaded.is_error, uploaded.content
        body = _obj(uploaded)
        run_id = body["run_id"]
        assert "docx" in body["extraction_methods"]
        result = body["result"]

        # --- discover the run by listing: nobody here started it by holding an id ----
        listed = await service.call_tool(
            "list_collection_runs", {"collection_id": collection_id, "status": "running"}
        )
        running = {row["run_id"]: row for row in _rows(listed)}
        assert run_id in running
        assert running[run_id]["trigger"] == "mcp"
        assert running[run_id]["trigger_document_id"] is not None

        # --- enumerate items and decide at both gates: review, then the override ----
        gates_seen: list[str] = []
        while "report" not in result:
            enumerated = await reviewer.call_tool("list_review_items", {"run_id": run_id})
            pending = [item for item in _rows(enumerated) if item["state"] == "pending"]
            if pending:
                gates_seen.append("review")
                decided = await reviewer.call_tool(
                    "decide_review_items",
                    {
                        "run_id": run_id,
                        "decisions": {item["id"]: _decide_mixed(item) for item in pending},
                        "basis": {item["id"]: _basis_for(item) for item in pending},
                        "idempotency_key": str(uuid4()),
                    },
                )
                assert not decided.is_error, decided.content
                result = _obj(decided)
            else:
                gates_seen.append("override")
                overridden = await reviewer.call_tool(
                    "override_blockers",
                    {
                        "run_id": run_id,
                        "override": True,
                        "reason": "counsel signed off",
                        "idempotency_key": str(uuid4()),
                    },
                )
                assert not overridden.is_error, overridden.content
                result = _obj(overridden)

        report = result["report"]
        assert "override" in gates_seen, "the blocker gate was never reached"
        assert report["status"] == "committed"
        assert "::liability_cap" not in report["committed_keys"]
        assert "::payment_due_days" in report["committed_keys"]
        assert "::notice_days" in report["committed_keys"]

        # --- export the register as a self-contained artifact ------------------------
        exported = await service.call_tool(
            "export_register", {"collection_id": collection_id, "run_id": run_id}
        )
        assert not exported.is_error, exported.content
        artifact = _obj(exported)

        assert artifact["run"]["run_id"] == run_id
        assert artifact["run"]["status"] == "committed"
        assert artifact["ruleset"]["name"] == BLOCKING_PLAYBOOK["name"]

        approved_register = await service.call_tool(
            "list_register", {"collection_id": collection_id}
        )
        approved = {row["key"]: row for row in _rows(approved_register)}
        assert approved.keys() == {"payment_due_days", "notice_days"}

        exported_by_key = {row["key"]: row for row in artifact["register"]}
        assert exported_by_key.keys() == approved.keys(), (
            "the export must carry exactly the approved register, nothing rejected"
        )
        for key, row in approved.items():
            assert exported_by_key[key]["value"] == row["value"]
            assert exported_by_key[key]["content_hash"] == row["content_hash"]
            assert exported_by_key[key]["quotes"], f"{key} was exported with no evidence"

        # --- every quote resolves to real offsets in the real document ---------------
        # A document is addressed by the hash of its extracted text, not its raw bytes
        # (two formats holding the same text are the same document) -- see
        # `graph.nodes` line ~665, `sha256=sha256_text(raw["text"])`.
        extracted = await extract_document(
            filename="msa.docx", mime_type=DOCX_MIME, data=docx_bytes
        )
        expected_sha256 = hashlib.sha256(extracted.text.encode("utf-8")).hexdigest()
        for row in artifact["register"]:
            for quote in row["quotes"]:
                assert quote["document_filename"] == "msa.docx"
                assert quote["document_sha256"] == expected_sha256
                block = extracted.blocks[quote["block_index"]]
                assert block.text[quote["char_start"] : quote["char_end"]] == quote["quote"]


async def test_a_service_token_is_refused_at_every_decision_tool() -> None:
    """A service credential authenticates fine everywhere and may decide nowhere."""
    app = mcp_server.mcp.streamable_http_app()
    async with (
        app.router.lifespan_context(app),
        _mcp_client(app, SERVICE_TOKEN) as service,
    ):
        created = await service.call_tool("create_collection", {"name": "service-refused"})
        collection_id = _obj(created)["collection_id"]
        started = await service.call_tool(
            "start_obligations_run",
            {
                "collection_id": collection_id,
                "idempotency_key": f"msa-{uuid4()}",
                "filename": "msa.txt",
                "text": MSA,
            },
        )
        run_id = _obj(started)["run_id"]

        decide_denied = await service.call_tool(
            "decide_review_items",
            {"run_id": run_id, "decisions": {}, "idempotency_key": str(uuid4())},
        )
        assert decide_denied.is_error
        assert _obj_error(decide_denied)["error_type"] == "not_a_reviewer"

        override_denied = await service.call_tool(
            "override_blockers",
            {
                "run_id": run_id,
                "override": True,
                "reason": "nope",
                "idempotency_key": str(uuid4()),
            },
        )
        assert override_denied.is_error
        assert _obj_error(override_denied)["error_type"] == "not_a_reviewer"


def _obj_error(result: CallToolResult) -> dict:
    """A refused tool call's structured `error_type` payload -- see `mcp_server._structured`.

    The text is `ToolManager.call_tool`'s own `"Error executing tool {name}: {exc}"`
    wrapper around it (the MCP SDK's, not this codebase's), so the JSON object is a
    suffix of the string, not the whole of it."""
    text = result.content[0].text
    return json.loads(text[text.index("{") :])


async def test_a_duplicate_decision_call_after_the_run_has_closed_is_refused() -> None:
    """A retried call under the same idempotency key must never apply a second
    decision. This document (no ruleset installed) commits in the single
    `decide_review_items` call below, so by the time a retry of that exact call
    would arrive the run has already closed -- and a decision tool has nowhere
    durable to replay the first response from, so the retry is refused as
    `run_closed` rather than touching the register a second time or doing anything
    else undefined. The register is proven unchanged by the refused retry, and a
    call that reuses the same key for genuinely different decisions is refused too,
    whether or not the run has closed."""
    app = mcp_server.mcp.streamable_http_app()
    async with (
        app.router.lifespan_context(app),
        _mcp_client(app, SERVICE_TOKEN) as service,
        _mcp_client(app, REVIEWER_TOKEN) as reviewer,
    ):
        created = await service.call_tool("create_collection", {"name": "duplicate-call"})
        collection_id = _obj(created)["collection_id"]
        started = await service.call_tool(
            "start_obligations_run",
            {
                "collection_id": collection_id,
                "idempotency_key": f"msa-{uuid4()}",
                "filename": "msa.txt",
                "text": MSA,
            },
        )
        run_id = _obj(started)["run_id"]
        listed = await reviewer.call_tool("list_review_items", {"run_id": run_id})
        pending = [item for item in _rows(listed) if item["state"] == "pending"]
        key = str(uuid4())
        decisions = {item["id"]: "approved" for item in pending}
        basis = {item["id"]: _basis_for(item) for item in pending}

        first = await reviewer.call_tool(
            "decide_review_items",
            {"run_id": run_id, "decisions": decisions, "basis": basis, "idempotency_key": key},
        )
        assert not first.is_error, first.content
        assert _obj(first)["report"]["status"] == "committed"
        register_after_first = await service.call_tool(
            "list_register", {"collection_id": collection_id}
        )

        replayed = await reviewer.call_tool(
            "decide_review_items",
            {"run_id": run_id, "decisions": decisions, "basis": basis, "idempotency_key": key},
        )
        assert replayed.is_error
        assert _obj_error(replayed)["error_type"] == "run_closed"

        register_after_replay = await service.call_tool(
            "list_register", {"collection_id": collection_id}
        )
        assert _rows(register_after_replay) == _rows(register_after_first), (
            "the refused retry must not have touched the register"
        )

        different = await reviewer.call_tool(
            "decide_review_items",
            {
                "run_id": run_id,
                "decisions": {item_id: "rejected" for item_id in decisions},
                "basis": basis,
                "idempotency_key": key,
            },
        )
        assert different.is_error, "a different call reusing the same key was accepted"


async def test_an_item_decided_twice_is_refused() -> None:
    """A second, different decision on an item something else already decided loses
    the compare-and-set and comes back as a distinct `error_type`, not a flattened
    generic failure -- see `ItemAlreadyDecidedError`.

    The graph's own item-level review gate answers every item it is handed in one
    resume and cannot be re-entered for the same items afterward, so the collision
    this proves against -- two callers racing to decide the same row -- is produced
    the way it would actually happen underneath the graph: directly at the repository,
    which is the layer whose compare-and-set this is. The MCP call under test is the
    one that loses the race, and what it gets back is what a real caller needs."""
    app = mcp_server.mcp.streamable_http_app()
    async with (
        app.router.lifespan_context(app),
        _mcp_client(app, SERVICE_TOKEN) as service,
        _mcp_client(app, REVIEWER_TOKEN) as reviewer,
    ):
        created = await service.call_tool("create_collection", {"name": "decided-twice"})
        collection_id = _obj(created)["collection_id"]
        started = await service.call_tool(
            "start_obligations_run",
            {
                "collection_id": collection_id,
                "idempotency_key": f"msa-{uuid4()}",
                "filename": "msa.txt",
                "text": MSA,
            },
        )
        run_id = _obj(started)["run_id"]
        listed = await reviewer.call_tool("list_review_items", {"run_id": run_id})
        pending = [item for item in _rows(listed) if item["state"] == "pending"]
        target = pending[0]
        others = pending[1:]

        # A concurrent decision on `target` lands directly at the repository -- the
        # graph's own checkpoint never learns of it, exactly as if a second reviewer's
        # resume call had won a race this call is about to lose.
        services = await runtime.get_services()
        await services.repository.decide_review_items(
            UUID(run_id), "concurrent-reviewer", {UUID(target["id"]): "approved"}
        )

        # This call decides everything, `target` included, with a verdict that does not
        # match what is already stored for it.
        second = await reviewer.call_tool(
            "decide_review_items",
            {
                "run_id": run_id,
                "decisions": {
                    target["id"]: "rejected",
                    **{item["id"]: "approved" for item in others},
                },
                "basis": {
                    target["id"]: _basis_for(target),
                    **{item["id"]: _basis_for(item) for item in others},
                },
                "idempotency_key": str(uuid4()),
            },
        )
        assert second.is_error
        assert _obj_error(second)["error_type"] == "item_already_decided"

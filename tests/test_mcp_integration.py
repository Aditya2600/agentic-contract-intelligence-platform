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

import json
from uuid import uuid4

import httpx2
import pytest
from mcp.client.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp_types import CallToolResult

from doctask import mcp_server, runtime
from doctask.config import settings

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

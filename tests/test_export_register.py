"""`export_register` must report the ruleset a run was actually judged against.

`pin_ruleset` resolves the collection's active playbook once, at run start, and pins it
into the graph state so a mid-run edit cannot judge one document against two versions.
Nothing durable recorded that choice, though: `export_register` used to re-read
`get_active_ruleset(collection_id)`, which answers "what is active now" -- the wrong
question once the collection has moved on to a newer version than the one the run
actually committed against. This is the regression test for that: run A commits under
v1, the collection advances to v2, and exporting run A must still identify v1.

Both REST (`GET /collections/{id}/runs/{run_id}/export`) and the MCP `export_register`
tool are `runtime.export_register` plus a transport, so proving it once here through
REST covers both -- `test_mcp_integration.py` separately proves the MCP transport calls
the same function.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from doctask import runtime
from doctask.config import settings
from doctask.main import app

CORPUS = Path(__file__).resolve().parent.parent / "data" / "sample_data"
SERVICE_TOKEN = "service-secret"
REVIEWER_TOKEN = "reviewer-secret"

RULESET_V1 = {
    "name": "Buyer Contract Playbook",
    "version": 1,
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

# A genuinely different ruleset, not a re-submission of v1 -- `put_ruleset` short-circuits
# an unchanged playbook rather than minting a new version, so the second install has to
# differ in content for the collection to actually advance.
RULESET_V2 = {
    "name": "Buyer Contract Playbook",
    "version": 1,
    "rules": [
        *RULESET_V1["rules"],
        {
            "code": "LIA-01",
            "severity": "major",
            "scope": "source",
            "text": "Liability cap must not exceed USD 500,000 without legal approval.",
        },
    ],
}


@pytest.fixture(autouse=True)
def tokens(monkeypatch):
    monkeypatch.setattr(settings, "reviewer_tokens", f"{REVIEWER_TOKEN}:alice")
    monkeypatch.setattr(settings, "service_tokens", f"{SERVICE_TOKEN}:ingest-bot")


@pytest.fixture
async def client():
    with TestClient(app) as test_client:
        yield test_client
    await runtime.shutdown_services()


def _service_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {SERVICE_TOKEN}"}


def _reviewer_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {REVIEWER_TOKEN}"}


def _basis_for(item: dict) -> dict:
    """What `/resume` requires a caller to echo back for this item -- read straight off
    the JSON `GET .../review-items` already returned, exactly as a real caller would."""
    if item["kind"] in {"register_update", "conflict", "scope_question"}:
        before = item["payload"].get("before")
        return {
            "version": before.get("version") if before else None,
            "content_hash": before.get("content_hash") if before else None,
        }
    if item["kind"] in {"finding", "deliverable_confirmation"}:
        return {
            "basis_hash": item["payload"].get("basis_hash"),
            "ruleset_hash": item["payload"].get("ruleset_hash"),
        }
    return {}


def _drive_to_commit(client: TestClient, run_id: str, result: dict) -> dict:
    """Approve every pending review item until the run's report appears."""
    while "report" not in result:
        pending = [
            item
            for item in client.get(
                f"/api/runs/{run_id}/review-items", headers=_reviewer_headers()
            ).json()
            if item["state"] == "pending"
        ]
        resumed = client.post(
            f"/api/runs/{run_id}/resume",
            json={
                "decisions": {item["id"]: "approved" for item in pending},
                "basis": {item["id"]: _basis_for(item) for item in pending},
                "idempotency_key": str(uuid4()),
            },
            headers=_reviewer_headers(),
        )
        assert resumed.status_code == 200, resumed.text
        result = resumed.json()
    return result


def _commit_a_run(client: TestClient, collection_id: str) -> str:
    """Drive `vendor_msa.txt` all the way to a commit and return its run id."""
    started = client.post(
        "/api/runs",
        json={
            "collection_id": collection_id,
            "idempotency_key": f"msa-{uuid4()}",
            "document": {
                "filename": "vendor_msa.txt",
                "mime_type": "text/plain",
                "text": (CORPUS / "vendor_msa.txt").read_text(),
            },
        },
        headers=_service_headers(),
    )
    assert started.status_code == 200, started.text
    body = started.json()
    run_id = body["run_id"]
    result = _drive_to_commit(client, run_id, body["result"])

    assert result["report"]["status"] == "committed"
    return run_id


async def test_export_reports_the_ruleset_the_run_actually_committed_under(client) -> None:
    collection_id = client.post(
        "/api/collections", json={"name": "ruleset-pin"}, headers=_service_headers()
    ).json()["collection_id"]

    v1 = client.put(
        f"/api/collections/{collection_id}/ruleset",
        json=RULESET_V1,
        headers=_service_headers(),
    )
    assert v1.status_code == 200, v1.text
    assert v1.json()["version"] == 1

    run_a = _commit_a_run(client, collection_id)

    # The run's own record of what it pinned, independent of the export endpoint --
    # this is what makes the assertion below a check of `export_register` and not a
    # tautology against whatever `get_run` happens to return.
    services = await runtime.get_services()
    run_summary = await services.repository.get_run(UUID(run_a))
    pinned_ruleset = await services.repository.get_ruleset(run_summary.ruleset_id)
    assert pinned_ruleset.version == 1

    # The collection advances to v2.
    v2 = client.put(
        f"/api/collections/{collection_id}/ruleset",
        json=RULESET_V2,
        headers={**_service_headers(), "If-Match": "1"},
    )
    assert v2.status_code == 200, v2.text
    assert v2.json()["version"] == 2
    active = await services.repository.get_active_ruleset(UUID(collection_id))
    assert active.version == 2, "the collection genuinely has to have moved on"

    exported = client.get(
        f"/api/collections/{collection_id}/runs/{run_a}/export", headers=_service_headers()
    )
    assert exported.status_code == 200, exported.text
    artifact = exported.json()

    assert artifact["run"]["run_id"] == run_a
    assert artifact["ruleset"] is not None, "run A pinned a ruleset and must report one"
    assert artifact["ruleset"]["version"] == 1, (
        "export must report the ruleset run A actually committed under, not the "
        "collection's current version"
    )


async def test_export_reports_no_ruleset_when_none_was_ever_pinned(client) -> None:
    """A run over a collection with no playbook pins nothing -- `export` must say so
    honestly rather than reporting whatever ruleset happens to exist by the time someone
    calls export, which would be exactly the bug this file is testing for."""
    collection_id = client.post(
        "/api/collections", json={"name": "no-playbook"}, headers=_service_headers()
    ).json()["collection_id"]

    started = client.post(
        "/api/runs",
        json={
            "collection_id": collection_id,
            "idempotency_key": f"msa-{uuid4()}",
            "document": {
                "filename": "vendor_msa.txt",
                "mime_type": "text/plain",
                "text": (CORPUS / "vendor_msa.txt").read_text(),
            },
        },
        headers=_service_headers(),
    )
    assert started.status_code == 200, started.text
    body = started.json()
    run_id = body["run_id"]
    result = _drive_to_commit(client, run_id, body["result"])
    assert result["report"]["status"] == "committed"

    # A collection is given a playbook only after this run finished.
    client.put(
        f"/api/collections/{collection_id}/ruleset",
        json=RULESET_V1,
        headers=_service_headers(),
    )

    exported = client.get(
        f"/api/collections/{collection_id}/runs/{run_id}/export", headers=_service_headers()
    )
    assert exported.status_code == 200, exported.text
    assert exported.json()["ruleset"] is None

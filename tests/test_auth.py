"""Only an authenticated human can decide.

The model and the automation around it may propose all day. What none of them may do is
move an item out of `pending`, and these tests are the proof at each layer that could be
mistaken for the gate: the credential store, the runtime choke point, the graph gates,
and the HTTP surface.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from langgraph.types import Command
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken

from doctask import mcp_server, runtime
from doctask.auth import (
    REVIEWER,
    SERVICE,
    AuthenticationError,
    AuthorizationError,
    Principal,
    authenticate,
    reviewer_from_payload,
)
from doctask.config import settings
from doctask.graph.builder import build_graph
from doctask.graph.nodes import NodeDependencies
from doctask.llm.fake import FakeLLM
from doctask.main import app
from doctask.mcp_server import DoctaskTokenVerifier
from doctask.repositories.memory import InMemoryRepository

MSA = """MASTER SERVICES AGREEMENT

Payment is due within 30 calendar days of receipt.
"""

REVIEWER_TOKEN = "reviewer-secret"
SERVICE_TOKEN = "service-secret"


@pytest.fixture(autouse=True)
def tokens(monkeypatch):
    monkeypatch.setattr(settings, "reviewer_tokens", f"{REVIEWER_TOKEN}:alice")
    monkeypatch.setattr(settings, "service_tokens", f"{SERVICE_TOKEN}:ingest-bot")


# --------------------------------------------------------------------- credentials


def test_an_unconfigured_deployment_authenticates_nobody(monkeypatch) -> None:
    """Fails closed. Forgetting to set the tokens must not open the gate to everyone."""
    monkeypatch.setattr(settings, "reviewer_tokens", "")
    monkeypatch.setattr(settings, "service_tokens", "")

    for candidate in (REVIEWER_TOKEN, "", None, "anything"):
        with pytest.raises(AuthenticationError):
            authenticate(candidate)


def test_a_token_resolves_to_its_own_identity_and_role() -> None:
    assert authenticate(REVIEWER_TOKEN) == Principal(actor_id="alice", role=REVIEWER)
    assert authenticate(SERVICE_TOKEN) == Principal(actor_id="ingest-bot", role=SERVICE)
    with pytest.raises(AuthenticationError):
        authenticate(REVIEWER_TOKEN + "x")


def test_a_payload_without_an_authenticated_role_is_not_a_decision() -> None:
    """A resume payload that never passed through `authenticate` carries no role, so
    the graph gates cannot be handed a decision by whoever reached them."""
    with pytest.raises(AuthorizationError):
        reviewer_from_payload({"actor_id": "alice", "decisions": {}})
    with pytest.raises(AuthorizationError):
        reviewer_from_payload({"actor_id": "ingest-bot", "actor_role": SERVICE})
    assert reviewer_from_payload({"actor_id": "alice", "actor_role": REVIEWER}).actor_id == "alice"


# ------------------------------------------------------------------ graph gates


async def _run_to_the_review_gate():
    repository = InMemoryRepository()
    graph = build_graph(NodeDependencies(repository=repository, model=FakeLLM()))
    collection_id = await repository.create_collection("acme")
    run_id = uuid4()
    config = {"configurable": {"thread_id": str(run_id)}}
    await graph.ainvoke(
        {
            "run_id": str(run_id),
            "collection_id": str(collection_id),
            "idempotency_key": "msa",
            "input_document": {"filename": "msa.txt", "mime_type": "text/plain", "text": MSA},
            "validation_attempt": 0,
            "status": "running",
        },
        config=config,
    )
    return repository, graph, config, run_id


async def test_the_model_can_propose_but_the_gate_refuses_to_let_it_decide() -> None:
    repository, graph, config, run_id = await _run_to_the_review_gate()
    pending = await repository.list_review_items(run_id)
    assert pending and all(item.state == "pending" for item in pending), (
        "the model's work must land as proposals, not as decisions"
    )
    decisions = {str(item.id): "approved" for item in pending}

    # The three ways an unauthenticated caller could try to answer the gate.
    for forged in (
        {"decisions": decisions},
        {"actor_id": "alice", "decisions": decisions},
        {"actor_id": "ingest-bot", "actor_role": SERVICE, "decisions": decisions},
    ):
        with pytest.raises(AuthorizationError):
            await graph.ainvoke(Command(resume=forged), config=config)

    assert all(
        item.state == "pending" and item.decided_by is None
        for item in await repository.list_review_items(run_id)
    )


async def test_a_stamped_reviewer_decides_and_is_recorded_as_the_decider() -> None:
    repository, graph, config, run_id = await _run_to_the_review_gate()
    pending = await repository.list_review_items(run_id)

    await graph.ainvoke(
        Command(
            resume={
                "decisions": {str(item.id): "approved" for item in pending},
                **Principal(actor_id="alice", role=REVIEWER).as_payload(),
            }
        ),
        config=config,
    )

    # Gate 1's items are decided. Gate 2 has since opened with its own, still pending:
    # the deliverable is a separate question and gets a separate, named decision.
    decided = [item for item in await repository.list_review_items(run_id) if item.id in
               {entry.id for entry in pending}]
    assert {item.state for item in decided} == {"approved"}
    assert {item.decided_by for item in decided} == {"alice"}


# --------------------------------------------------------------- runtime + HTTP


@pytest.fixture
async def client():
    with TestClient(app) as test_client:
        yield test_client
    await runtime.shutdown_services()


def _start_run(client: TestClient) -> str:
    """A service credential is enough to feed a document in: that is the model's half."""
    collection = client.post(
        "/api/collections",
        json={"name": "acme"},
        headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
    )
    assert collection.status_code == 200
    run = client.post(
        "/api/runs",
        json={
            "collection_id": collection.json()["collection_id"],
            "idempotency_key": f"msa-{uuid4()}",
            "document": {"filename": "msa.txt", "mime_type": "text/plain", "text": MSA},
        },
        headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
    )
    assert run.status_code == 200
    return run.json()["run_id"]


def test_the_api_refuses_decisions_without_a_reviewer_credential(client) -> None:
    run_id = _start_run(client)
    items = client.get(
        f"/api/runs/{run_id}/review-items",
        headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
    ).json()
    decisions = {item["id"]: "approved" for item in items}

    anonymous = client.post(f"/api/runs/{run_id}/resume", json={"decisions": decisions})
    assert anonymous.status_code == 401

    wrong_scheme = client.post(
        f"/api/runs/{run_id}/resume",
        json={"decisions": decisions},
        headers={"Authorization": REVIEWER_TOKEN},
    )
    assert wrong_scheme.status_code == 401

    # Authenticates fine, and is still refused: a service is not a human.
    as_service = client.post(
        f"/api/runs/{run_id}/resume",
        json={"decisions": decisions},
        headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
    )
    assert as_service.status_code == 403

    for item in client.get(
        f"/api/runs/{run_id}/review-items",
        headers={"Authorization": f"Bearer {REVIEWER_TOKEN}"},
    ).json():
        assert item["state"] == "pending"
        assert item["decided_by"] is None


def test_the_override_gate_is_reviewer_only(client) -> None:
    run_id = _start_run(client)
    body = {"override": True, "reason": "counsel signed off"}

    assert client.post(f"/api/runs/{run_id}/override", json=body).status_code == 401
    assert (
        client.post(
            f"/api/runs/{run_id}/override",
            json=body,
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
        ).status_code
        == 403
    )


def test_a_decision_is_recorded_against_the_credential_not_the_request(client) -> None:
    """The body has no actor field at all, and an extra one is ignored rather than
    believed: `decided_by` is who authenticated, never who the caller said they were."""
    run_id = _start_run(client)
    items = client.get(
        f"/api/runs/{run_id}/review-items",
        headers={"Authorization": f"Bearer {REVIEWER_TOKEN}"},
    ).json()

    response = client.post(
        f"/api/runs/{run_id}/resume",
        json={
            "decisions": {item["id"]: "approved" for item in items},
            "actor_id": "someone-else",
        },
        headers={"Authorization": f"Bearer {REVIEWER_TOKEN}"},
    )
    assert response.status_code == 200

    decided = client.get(
        f"/api/runs/{run_id}/review-items",
        headers={"Authorization": f"Bearer {REVIEWER_TOKEN}"},
    ).json()
    settled = {item["id"] for item in items}
    assert {row["decided_by"] for row in decided if row["id"] in settled} == {"alice"}
    # Whatever Gate 2 opened afterwards is nobody's decision yet, and says so.
    assert all(
        row["decided_by"] is None for row in decided if row["id"] not in settled
    )


# ------------------------------------------------------------------------- MCP

def test_no_mcp_tool_takes_a_credential_as_an_argument() -> None:
    """The credential belongs to the connection, not to a call.

    A token in a tool argument is echoed into tool-call logs, traces, and the model's
    own context, so a schema that asks for one has already leaked it.
    """
    for tool in asyncio.run(mcp_server.mcp.list_tools()):
        properties = tool.input_schema.get("properties") or {}
        assert not any("token" in name or "secret" in name for name in properties), (
            f"{tool.name} asks a caller for a credential: {sorted(properties)}"
        )


async def test_the_transport_verifier_maps_a_token_to_its_role() -> None:
    verifier = DoctaskTokenVerifier()
    reviewer = await verifier.verify_token(REVIEWER_TOKEN)
    assert reviewer is not None
    assert (reviewer.subject, reviewer.scopes) == ("alice", [REVIEWER])

    service = await verifier.verify_token(SERVICE_TOKEN)
    assert service is not None
    assert (service.subject, service.scopes) == ("ingest-bot", [SERVICE])

    # A bad token is not an exception here: returning None is what makes the transport
    # answer 401 before any tool body runs.
    assert await verifier.verify_token("nope") is None


def test_mcp_tools_read_the_role_off_the_verified_connection(monkeypatch) -> None:
    # Nothing authenticated on this connection at all.
    with pytest.raises(AuthenticationError):
        mcp_server._caller()

    def connect_as(actor_id: str, role: str) -> None:
        auth_context_var.set(
            AuthenticatedUser(
                AccessToken(token="x", client_id=actor_id, subject=actor_id, scopes=[role])
            )
        )

    connect_as("ingest-bot", SERVICE)
    assert mcp_server._caller() == Principal(actor_id="ingest-bot", role=SERVICE)
    with pytest.raises(AuthorizationError):
        mcp_server._reviewer()

    connect_as("alice", REVIEWER)
    assert mcp_server._reviewer() == Principal(actor_id="alice", role=REVIEWER)


async def test_the_mcp_transport_answers_401_before_any_tool_runs() -> None:
    """The bearer check happens in the ASGI stack, so an unauthenticated caller never
    reaches a tool body and never gets to name itself in an argument."""
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    }
    headers = {"accept": "application/json, text/event-stream"}

    async with mcp_server.app.router.lifespan_context(mcp_server.app):
        transport = httpx.ASGITransport(app=mcp_server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as http:
            anonymous = await http.post("/mcp", json=request, headers=headers)
            assert anonymous.status_code == 401
            # RFC 9728: the challenge says where to go and get a token.
            assert "resource_metadata=" in anonymous.headers["www-authenticate"]

            wrong = await http.post(
                "/mcp", json=request, headers={**headers, "authorization": "Bearer nope"}
            )
            assert wrong.status_code == 401

            accepted = await http.post(
                "/mcp",
                json=request,
                headers={**headers, "authorization": f"Bearer {SERVICE_TOKEN}"},
            )
            assert accepted.status_code == 200


# --------------------------------------------------------- the classification gate


async def _to_classification_gate():
    """A document the offline model cannot type, parked at the classification gate."""
    repository = InMemoryRepository()
    graph = build_graph(NodeDependencies(repository=repository, model=FakeLLM()))
    collection_id = await repository.create_collection("acme")
    run_id = uuid4()
    config = {"configurable": {"thread_id": str(run_id)}}
    result = await graph.ainvoke(
        {
            "run_id": str(run_id),
            "collection_id": str(collection_id),
            "idempotency_key": "notice",
            "input_document": {
                "filename": "notice.txt",
                "mime_type": "text/plain",
                "text": "Operations Notice\n\nScheduled maintenance on 31 May 2026.\n",
            },
            "validation_attempt": 0,
            "status": "running",
        },
        config=config,
    )
    assert result["__interrupt__"][0].value["kind"] == "document_classification"
    return repository, graph, config, run_id


async def test_the_classification_gate_refuses_a_type_from_an_unauthenticated_caller() -> None:
    """Filing a document as an amendment is what lets it supersede a committed term, so
    it is a reviewer's decision like any other."""
    _, graph, config, _ = await _to_classification_gate()
    with pytest.raises(AuthorizationError):
        await graph.ainvoke(Command(resume={"document_type": "amendment"}), config=config)


async def test_the_classification_gate_refuses_a_type_that_is_not_a_type() -> None:
    _, graph, config, _ = await _to_classification_gate()
    with pytest.raises(ValueError, match="document_type must be one of"):
        await graph.ainvoke(
            Command(
                resume={
                    "document_type": "whatever-i-say",
                    **Principal(actor_id="alice", role=REVIEWER).as_payload(),
                }
            ),
            config=config,
        )


async def test_a_reviewer_files_the_document_and_is_recorded_as_having_done_it() -> None:
    """The answer is a field, not the payload. Reading the payload itself as the type
    stored the whole resume dict, credential included, as the document's type."""
    repository, graph, config, run_id = await _to_classification_gate()

    await graph.ainvoke(
        Command(
            resume={
                "document_type": "policy",
                **Principal(actor_id="alice", role=REVIEWER).as_payload(),
            }
        ),
        config=config,
    )

    document = next(iter(repository.documents.values()))
    assert document.doc_type == "policy"
    assert any(
        event.stage == "classify_review" and "alice selected document type policy" in event.reason
        for event in await repository.list_events(run_id)
    )

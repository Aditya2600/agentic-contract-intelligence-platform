from __future__ import annotations

from dataclasses import asdict
from uuid import UUID

from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings

from doctask.auth import AuthenticationError, Principal, authenticate, require_reviewer
from doctask.config import settings
from doctask.runtime import (
    decide_reviewed_items,
    get_run_status,
    get_services,
    install_ruleset,
    override_run_blockers,
    rederive_run,
    start_run,
)


class DoctaskTokenVerifier:
    """Authenticate the transport, not the arguments.

    The credential arrives in the `Authorization` header and is verified once per
    request, before any tool body runs. It is deliberately not a tool parameter:
    parameters are echoed into tool-call logs, traces, and model context, and a bearer
    token that turns up in any of those has to be treated as disclosed.
    """

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            principal = authenticate(token)
        except AuthenticationError:
            return None
        # The role travels as a scope, which is what the MCP layer understands. The
        # tools read it back out through `_caller`.
        return AccessToken(
            token=token,
            client_id=principal.actor_id,
            subject=principal.actor_id,
            scopes=[principal.role],
        )


mcp = MCPServer(
    "Vendor Obligations Register",
    token_verifier=DoctaskTokenVerifier(),
    auth=AuthSettings(
        issuer_url=settings.mcp_issuer_url,
        resource_server_url=settings.mcp_resource_server_url,
        # No scope is required to reach a tool. Which role a tool needs is the tool's
        # own business, and only the decision tools need a human.
        required_scopes=[],
    ),
)


def _caller() -> Principal:
    """The authenticated party behind this request, from the verified bearer token."""
    access_token = get_access_token()
    if access_token is None or not access_token.scopes:
        raise AuthenticationError("missing or unknown credential")
    return Principal(
        actor_id=access_token.subject or access_token.client_id,
        role=access_token.scopes[0],
    )


def _reviewer() -> Principal:
    """Deciding is a human act. A service credential authenticates fine and is still
    refused here, which is the whole separation: the model may propose, never dispose."""
    return require_reviewer(_caller())


@mcp.tool()
async def create_collection(name: str, slug: str | None = None) -> dict:
    """Create an isolated vendor-document collection, or return the existing one for
    the same slug -- defaulted from `name` when omitted. The same slug with a
    different name is refused rather than silently renamed."""
    _caller()
    services = await get_services()
    collection_id = await services.repository.create_collection(name, slug=slug)
    return {"collection_id": str(collection_id)}


@mcp.tool()
async def start_obligations_run(
    collection_id: str,
    idempotency_key: str,
    filename: str,
    text: str,
    mime_type: str = "text/plain",
) -> dict:
    """Upload a document and run the obligations-register graph until completion or review."""
    _caller()
    run_id, result = await start_run(
        collection_id=UUID(collection_id),
        idempotency_key=idempotency_key,
        filename=filename,
        mime_type=mime_type,
        text=text,
    )
    return {"run_id": str(run_id), "result": result}


@mcp.tool()
async def rederive_stale_run(
    collection_id: str,
    stale_run_id: str,
    idempotency_key: str,
) -> dict:
    """Redo the proposals a run approved but could not commit because their register
    items moved first (its report said `status: stale`). Starts a new run over the
    document already stored: no upload, no re-extraction, register read as it now
    stands. It stops at its own review gate, so the refused keys get a fresh decision
    rather than a replay of one made against a value that has since changed."""
    _caller()
    run_id, result = await rederive_run(
        collection_id=UUID(collection_id),
        stale_run_id=UUID(stale_run_id),
        idempotency_key=idempotency_key,
    )
    return {"run_id": str(run_id), "result": result}


@mcp.tool()
async def list_review_items(run_id: str) -> list[dict]:
    """Return independent review decisions waiting for an explicit caller action."""
    _caller()
    services = await get_services()
    return [asdict(item) for item in await services.repository.list_review_items(UUID(run_id))]


@mcp.tool()
async def decide_review_items(
    run_id: str,
    decisions: dict[str, str],
    idempotency_key: str,
    basis: dict[str, dict] | None = None,
    document_type: str | None = None,
) -> dict:
    """Explicitly approve or reject each item and resume the paused graph.

    `basis` states, per item id, the `version`/`content_hash` or `basis_hash`/
    `ruleset_hash` that item carried in the `list_review_items` response this decision
    was made from (whichever pair that item's `payload` carries -- an
    `injection_review` item carries neither and needs no entry). An item that has
    moved since is refused rather than decided blind. `idempotency_key` makes a
    retried call with the exact same decisions a no-op instead of a second decision;
    reused with different decisions, it is refused.

    Requires a reviewer credential on the connection; the decision is recorded against
    that reviewer, and there is no parameter for naming someone else."""
    return await decide_reviewed_items(
        UUID(run_id),
        {UUID(item_id): decision for item_id, decision in decisions.items()},
        basis={UUID(item_id): entry for item_id, entry in (basis or {}).items()},
        idempotency_key=idempotency_key,
        principal=_reviewer(),
        document_type=document_type,
    )


@mcp.tool()
async def override_blockers(
    run_id: str, override: bool, idempotency_key: str, reason: str = ""
) -> dict:
    """Answer the blocker gate for a run whose commit is held by an upheld blocker
    finding. `override=false` leaves the run blocked and writes nothing. `override=true`
    requires a reason, which is recorded in the run's event log. Reviewer credential
    only. `idempotency_key` makes a retried call with the same answer a no-op; reused
    with a different answer, it is refused."""
    return await override_run_blockers(
        UUID(run_id),
        override=override,
        reason=reason,
        idempotency_key=idempotency_key,
        principal=_reviewer(),
    )


@mcp.tool(name="get_run_status")
async def get_run_status_tool(run_id: str) -> dict:
    """What a run is doing right now: its status, and how many review items are still
    pending. Reads stored state only -- no lease taken, no graph invoked."""
    _caller()
    status = await get_run_status(UUID(run_id))
    if status is None:
        raise ValueError(f"run {run_id} does not exist")
    return status


@mcp.tool()
async def list_register(collection_id: str) -> list[dict]:
    """Every register row currently standing for a collection, one per agreement-scoped
    obligation key."""
    _caller()
    services = await get_services()
    return [
        asdict(item) for item in await services.repository.list_register(UUID(collection_id))
    ]


@mcp.tool()
async def get_snapshot_diff(run_id: str) -> list[dict]:
    """The register rows this run actually changed: old value, new value, both content
    hashes. Empty for a run that never committed -- blocked, stale, or still open."""
    _caller()
    services = await get_services()
    return [asdict(change) for change in await services.repository.list_run_changes(UUID(run_id))]


@mcp.tool()
async def put_ruleset(collection_id: str, playbook: dict, if_match: int | None = None) -> dict:
    """Install the playbook a collection is evaluated against.

    Content-idempotent: uploading exactly what is already active is a no-op and
    returns it unchanged, at its existing version. Changed content needs `if_match`
    set to the version being replaced -- read off a prior call's `version` field --
    or the call is refused rather than guessing which version it was meant to
    supersede."""
    _caller()
    stored, created = await install_ruleset(UUID(collection_id), playbook, if_match=if_match)
    return {
        "ruleset_id": str(stored.id),
        "version": stored.version,
        "rules": [rule.code for rule in stored.rules],
        "created": created,
    }


@mcp.tool()
async def list_findings(run_id: str) -> list[dict]:
    """Return every rule verdict for a run, including explicit pass rows."""
    _caller()
    services = await get_services()
    return [asdict(finding) for finding in await services.repository.list_findings(UUID(run_id))]


@mcp.tool()
async def get_run_events(run_id: str) -> list[dict]:
    """Return the visible stage, decision, reason, and next-node audit trail."""
    _caller()


    services = await get_services()
    return [asdict(event) for event in await services.repository.list_events(UUID(run_id))]


app = mcp.streamable_http_app()

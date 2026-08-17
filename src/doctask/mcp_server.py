from __future__ import annotations

import base64
import binascii
import json
from dataclasses import asdict
from uuid import UUID

from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings

from doctask.auth import (
    AuthenticationError,
    AuthorizationError,
    Principal,
    authenticate,
    require_reviewer,
)
from doctask.config import settings
from doctask.repositories.base import ItemAlreadyDecidedError
from doctask.runtime import (
    RunBusyError,
    RunClosedError,
    RunNotFoundError,
    StaleDecisionError,
    decide_reviewed_items,
    export_register,
    get_run_cost_report,
    get_run_stages,
    get_run_status,
    get_services,
    ingest_file,
    install_ruleset,
    override_run_blockers,
    rederive_run,
    start_run,
)
from doctask.services.extraction import ExtractionError

# Maps the errors a caller genuinely needs to tell apart to a stable, machine-readable
# `error_type`. `str(exc)` still carries the prose reason -- this only adds a field a
# caller can branch on without parsing English. Deliberately not raised as `MCPError`:
# that would turn a tool-execution failure into a transport-level one, and every
# existing client here (including the reviewer/service test suite) already depends on
# a refused call coming back as `CallToolResult(is_error=True)`, not a raised
# exception from `call_tool` itself.
_ERROR_TYPES: tuple[tuple[type[Exception], str], ...] = (
    (RunNotFoundError, "run_not_found"),
    (RunBusyError, "run_busy"),
    (RunClosedError, "run_closed"),
    (StaleDecisionError, "basis_moved"),
    (ItemAlreadyDecidedError, "item_already_decided"),
    (AuthorizationError, "not_a_reviewer"),
    (ExtractionError, "unreadable_document"),
)


_KNOWN_EXCEPTIONS = tuple(cls for cls, _ in _ERROR_TYPES)


def _structured(exc: Exception) -> Exception:
    """Re-shape a known failure into one whose `str()` is parseable JSON.

    The MCP framework flattens every tool exception to `str(exc)` in a text content
    block and sets `is_error=True` -- see `MCPServer._handle_call_tool`. That is the
    only signal a caller gets, so distinguishing "run does not exist" from "run is
    busy" from "wrong credential" has to happen inside that string, not via exception
    type (which does not survive the wire) or a raised protocol error (which would
    break the existing is_error contract). One layer down, `ToolManager.call_tool`
    also wraps whatever a tool body raises as `ToolError(f"Error executing tool
    {name}: {exc}")`, so the JSON this produces is never the *entire* text content --
    a caller (this module's own tests included) locates the `{` and parses from
    there, the same way it would recover the message from any other tool's error text.

    Callers only pass exceptions of a type listed in `_ERROR_TYPES` -- see
    `_KNOWN_EXCEPTIONS` -- so every other failure propagates with its own type and
    traceback intact rather than being caught here just to be handed back unchanged.
    """
    for cls, error_type in _ERROR_TYPES:
        if isinstance(exc, cls):
            payload: dict[str, object] = {"error_type": error_type, "message": str(exc)}
            if isinstance(exc, RunClosedError):
                payload["run_id"] = str(exc.run_id)
                payload["status"] = exc.status
            return RuntimeError(json.dumps(payload))
    return exc


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
    principal = _caller()
    run_id, result = await start_run(
        collection_id=UUID(collection_id),
        idempotency_key=idempotency_key,
        filename=filename,
        mime_type=mime_type,
        text=text,
        trigger="mcp",
        principal=principal,
    )
    return {"run_id": str(run_id), "result": result}


@mcp.tool()
async def upload_obligations_document(
    collection_id: str,
    idempotency_key: str,
    filename: str,
    content_base64: str,
    mime_type: str = "application/octet-stream",
) -> dict:
    """Upload a PDF or DOCX (or plain text) and run the obligations-register graph
    until completion or review.

    `start_obligations_run` only accepts already-extracted text, which is the whole
    of what a program driving this server can reach unless it also reimplements PDF
    and DOCX extraction itself. This tool is that missing half: `content_base64` is
    the raw file bytes, base64-encoded (the MCP wire format is JSON, which has no
    binary type), and it is decoded and handed to the same extraction-then-run path
    `POST /runs/upload` and the collection watcher already use -- native PDF/DOCX
    reading, OCR fallback for a page that needs it, fail-fast on a page neither can
    read. A file this server cannot read never becomes a run at all."""
    principal = _caller()
    try:
        data = base64.b64decode(content_base64, validate=True)
    except binascii.Error as exc:
        raise ValueError(f"content_base64 is not valid base64: {exc}") from exc
    try:
        run_id, extracted, result = await ingest_file(
            collection_id=UUID(collection_id),
            idempotency_key=idempotency_key,
            filename=filename,
            mime_type=mime_type,
            data=data,
            trigger="mcp",
            principal=principal,
        )
    except _KNOWN_EXCEPTIONS as exc:
        raise _structured(exc) from None
    return {
        "run_id": str(run_id),
        "extraction_methods": sorted(extracted.methods),
        "extraction_warnings": extracted.warnings,
        "blocks": len(extracted.blocks),
        "result": result,
    }


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
    that reviewer, and there is no parameter for naming someone else.

    A run that does not exist, is already busy with another decision, is already
    closed (blocked, committed or failed), or an item whose basis has moved or was
    already decided, each come back as a distinct `error_type` in the refusal rather
    than one flattened error string -- see `_structured`."""
    try:
        return await decide_reviewed_items(
            UUID(run_id),
            {UUID(item_id): decision for item_id, decision in decisions.items()},
            basis={UUID(item_id): entry for item_id, entry in (basis or {}).items()},
            idempotency_key=idempotency_key,
            principal=_reviewer(),
            document_type=document_type,
        )
    except _KNOWN_EXCEPTIONS as exc:
        raise _structured(exc) from None


@mcp.tool()
async def override_blockers(
    run_id: str, override: bool, idempotency_key: str, reason: str = ""
) -> dict:
    """Answer the blocker gate for a run whose commit is held by an upheld blocker
    finding. `override=false` leaves the run blocked and writes nothing. `override=true`
    requires a reason, which is recorded in the run's event log. Reviewer credential
    only. `idempotency_key` makes a retried call with the same answer a no-op; reused
    with a different answer, it is refused.

    A run that does not exist, is already busy, or is already closed comes back as a
    distinct `error_type` -- see `_structured`."""
    try:
        return await override_run_blockers(
            UUID(run_id),
            override=override,
            reason=reason,
            idempotency_key=idempotency_key,
            principal=_reviewer(),
        )
    except _KNOWN_EXCEPTIONS as exc:
        raise _structured(exc) from None


@mcp.tool(name="get_run_status")
async def get_run_status_tool(run_id: str) -> dict:
    """What a run is doing right now: its status, and how many review items are still
    pending. Reads stored state only -- no lease taken, no graph invoked."""
    _caller()
    status = await get_run_status(UUID(run_id))
    if status is None:
        raise _structured(RunNotFoundError(f"run {run_id} does not exist"))
    return status


@mcp.tool()
async def list_collection_runs(collection_id: str, status: str | None = None) -> list[dict]:
    """Every run a collection has, newest first, optionally narrowed to one status.

    The watcher starts runs on its own schedule and every one of them stops at a
    human gate -- so this is how a caller that started nothing finds what is waiting
    for it, e.g. `status="running"` for what the watcher parked at `list_review_items`,
    or `get_run_status` per id to see which of those are actually `awaiting_review`."""
    _caller()
    services = await get_services()
    runs = await services.repository.list_runs(UUID(collection_id), status=status)
    return [
        {
            "run_id": str(run.run_id),
            "collection_id": str(run.collection_id),
            "status": run.status,
            "started_at": run.started_at.isoformat(),
            "ended_at": run.ended_at.isoformat() if run.ended_at else None,
            "trigger": run.trigger,
            "trigger_document_id": (
                str(run.trigger_document_id) if run.trigger_document_id else None
            ),
        }
        for run in runs
    ]


@mcp.tool(name="export_register")
async def export_register_tool(collection_id: str, run_id: str) -> dict:
    """Export a collection's register as a self-contained artifact: every row's value,
    its state, and the quotes that support it -- each with its document, page and
    character offsets -- plus the run this export was taken alongside and the ruleset
    that run was actually pinned to (not whatever is active for the collection now, if
    the playbook has since moved on). Enough for a reader with no database access to
    check any claim against a source file.

    `run_id` must belong to `collection_id`; a caller usually passes the run whose
    commit it just drove to completion."""
    _caller()
    try:
        return await export_register(UUID(collection_id), UUID(run_id))
    except _KNOWN_EXCEPTIONS as exc:
        raise _structured(exc) from None


@mcp.tool(name="get_run_stages")
async def get_run_stages_tool(run_id: str) -> list[dict]:
    """This run's ordered stage history from the exactly-once ledger: what each stage was
    given, what it produced, and how many times it executed. `get_run_status` answers
    "where is it now"; this answers "what has it already done, exactly once" -- which is
    how a caller confirms a crashed-and-resumed run replayed rather than re-did work."""
    _caller()
    try:
        return await get_run_stages(UUID(run_id))
    except _KNOWN_EXCEPTIONS as exc:
        raise _structured(exc) from None


@mcp.tool(name="get_run_cost_report")
async def get_run_cost_report_tool(run_id: str) -> dict:
    """What this run cost and where the time went: total execution time and estimated
    spend, and a per-stage breakdown of time, spend, model, tokens in/out, cache hits
    and external-service calls -- all read back from the run's own event log, callable
    any time after the fact. States which price table version priced it, and reports a
    model with no declared price as unpriced rather than free."""
    _caller()
    try:
        return await get_run_cost_report(UUID(run_id))
    except _KNOWN_EXCEPTIONS as exc:
        raise _structured(exc) from None


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

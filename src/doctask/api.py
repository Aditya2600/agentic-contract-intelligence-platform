from __future__ import annotations

from dataclasses import asdict
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile
from pydantic import BaseModel, Field

from doctask.auth import (
    AuthenticationError,
    AuthorizationError,
    Principal,
    authenticate,
    require_reviewer,
)
from doctask.repositories.base import CollectionConflictError
from doctask.runtime import (
    IdempotencyConflictError,
    RulesetConflictError,
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
from doctask.services.casing import camelize
from doctask.services.extraction import ExtractionError

router = APIRouter()


async def current_principal(authorization: str = Header(default="")) -> Principal:
    """Any authenticated caller: a reviewer, or a service that feeds documents in."""
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=401,
            detail="bearer credential required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        return authenticate(token)
    except AuthenticationError as exc:
        raise HTTPException(
            status_code=401, detail=str(exc), headers={"WWW-Authenticate": "Bearer"}
        ) from exc


async def current_reviewer(
    principal: Annotated[Principal, Depends(current_principal)],
) -> Principal:
    """A human. Nothing a service presents gets past this."""
    try:
        return require_reviewer(principal)
    except AuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


Caller = Annotated[Principal, Depends(current_principal)]
Reviewer = Annotated[Principal, Depends(current_reviewer)]


async def _serialize_collection(services, summary) -> dict:
    """A `CollectionSummary` row plus its active playbook, camelCased for the web
    client. Kept separate from the repository layer, which has no reason to know
    what a ruleset's rules look like rendered as playbook cards."""
    ruleset = await services.repository.get_active_ruleset(summary.id)
    playbook = (
        [
            {
                "rule_code": rule.code,
                "title": rule.text,
                "severity": rule.severity,
                "description": rule.text,
                "enabled": True,
            }
            for rule in ruleset.rules
        ]
        if ruleset is not None
        else []
    )
    return camelize({**asdict(summary), "playbook": playbook})


async def _serialize_run(run) -> dict:
    """A `RunSummary` row enriched with the fields the web client's `Run` type needs
    but the stored row does not carry on its own: current stage and pending-review
    count (derived from events + review items, same as `GET .../status`), spend and
    duration (derived from events + the stage ledger, same as `GET .../cost`)."""
    status = await get_run_status(run.run_id)
    cost = await get_run_cost_report(run.run_id)
    gate_reason = None
    if status is not None and status["status"] in {"blocked", "awaiting_review"}:
        services = await get_services()
        events = await services.repository.list_events(run.run_id)
        gate_reason = events[-1].reason if events else None
    return camelize(
        {
            "id": run.run_id,
            "collection_id": run.collection_id,
            "trigger": run.trigger,
            "status": status["status"] if status is not None else run.status,
            "started_at": run.started_at,
            "duration_ms": cost["total_duration_ms"],
            "cost_usd": cost["total_spend_usd"],
            "current_stage": status["current_stage"] if status is not None else None,
            "gate_reason": gate_reason,
            "price_table_version": cost["price_table_version"],
            "pending_review_count": status["pending_review_items"] if status is not None else 0,
        }
    )


class CollectionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    # Defaults to a normalised form of `name` (see `services.ids.slugify`). Repeating
    # the same slug and name returns the existing collection; the same slug with a
    # different name is refused rather than silently renamed.
    slug: str | None = Field(default=None, min_length=1, max_length=120)


class WatchPathUpdate(BaseModel):
    # None (or omitted) stops the watcher polling this collection. The next sweep
    # simply no longer lists it -- there is nothing else to clean up, because the
    # watcher holds no state of its own for a collection it stops watching.
    watch_path: str | None = None


class DocumentPayload(BaseModel):
    filename: str
    mime_type: str = "text/plain"
    text: str = Field(min_length=1)


class RunCreate(BaseModel):
    collection_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=200)
    document: DocumentPayload


class RederiveCreate(BaseModel):
    collection_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=200)


# Neither body carries an actor. The decider is whoever presented the credential;
# a caller-supplied identity here would be a claim, and `decided_by` is a record.
class ReviewResume(BaseModel):
    decisions: dict[UUID, str] = Field(default_factory=dict)
    # Answers the classification gate instead, when that is the gate the run is parked
    # at. Validated against the allowed types in the node.
    document_type: str | None = None
    # What each decided item's version and basis hash looked like when this caller last
    # read it via GET .../review-items. A mismatch means it moved since, and the
    # decision is refused rather than applied against content nobody actually saw.
    basis: dict[UUID, dict[str, Any]] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=1, max_length=200)


class BlockerOverride(BaseModel):
    override: bool
    # Required when override is true. Checked in the node too, which is the real gate.
    reason: str = ""
    idempotency_key: str = Field(min_length=1, max_length=200)


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/collections")
async def create_collection(
    payload: CollectionCreate, principal: Caller
) -> dict[str, str]:
    services = await get_services()
    try:
        collection_id = await services.repository.create_collection(
            payload.name, slug=payload.slug
        )
    except CollectionConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"collection_id": str(collection_id)}


@router.put("/collections/{collection_id}/watch-path")
async def put_watch_path(
    collection_id: UUID, payload: WatchPathUpdate, principal: Caller
) -> dict:
    """Name (or clear) the directory the collection watcher polls for this collection.

    Takes effect on the watcher's next sweep: `list_watched_collections` is read fresh
    every poll, so nothing here needs to signal a running watcher process directly.
    """
    services = await get_services()
    try:
        await services.repository.set_collection_watch_path(
            collection_id, payload.watch_path
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"collection_id": str(collection_id), "watch_path": payload.watch_path}


@router.get("/collections")
async def list_collections(principal: Caller) -> list[dict]:
    services = await get_services()
    summaries = await services.repository.list_collections()
    return [await _serialize_collection(services, summary) for summary in summaries]


@router.get("/collections/{collection_id}")
async def get_collection(collection_id: UUID, principal: Caller) -> dict:
    services = await get_services()
    summary = await services.repository.get_collection(collection_id)
    if summary is None:
        raise HTTPException(status_code=404, detail=f"collection {collection_id} does not exist")
    return await _serialize_collection(services, summary)


# The web client's `kind` union only names four of the seven real doc types. The other
# three (sow, purchase_order, unknown) are passed through as-is rather than mapped onto
# a wrong bucket -- an unrecognised string is a more honest failure than a fabricated one.
_DOC_TYPE_TO_KIND = {
    "master_agreement": "msa",
    "amendment": "amendment",
    "invoice": "invoice",
    "policy": "policy",
}


@router.get("/collections/{collection_id}/documents")
async def list_documents(collection_id: UUID, principal: Caller) -> list[dict]:
    services = await get_services()
    docs = await services.repository.list_documents(collection_id)
    return [
        camelize(
            {
                "id": doc.id,
                "collection_id": doc.collection_id,
                "filename": doc.filename,
                "kind": _DOC_TYPE_TO_KIND.get(doc.doc_type, doc.doc_type),
                "pages": doc.pages,
                "content_hash": doc.sha256,
                "ingested_at": doc.ingested_at,
            }
        )
        for doc in docs
    ]


@router.get("/collections/{collection_id}/runs")
async def list_collection_runs(collection_id: UUID, principal: Caller) -> list[dict]:
    services = await get_services()
    runs = await services.repository.list_runs(collection_id)
    return [await _serialize_run(run) for run in runs]


@router.get("/runs/{run_id}")
async def get_run(run_id: UUID, principal: Caller) -> dict:
    services = await get_services()
    run = await services.repository.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run {run_id} does not exist")
    return await _serialize_run(run)


@router.post("/runs")
async def create_run(
    payload: RunCreate, principal: Caller
) -> dict:
    run_id, result = await start_run(
        collection_id=payload.collection_id,
        idempotency_key=payload.idempotency_key,
        filename=payload.document.filename,
        mime_type=payload.document.mime_type,
        text=payload.document.text,
        trigger="api",
        principal=principal,
    )
    return {"run_id": str(run_id), "result": result}


@router.post("/runs/upload")
async def upload_run(
    principal: Caller,
    collection_id: Annotated[UUID, Form()],
    idempotency_key: Annotated[str, Form(min_length=1, max_length=200)],
    file: Annotated[UploadFile, File()],
) -> dict:
    """Start a run from a PDF, DOCX or TXT file.

    Extraction happens before the run rather than in the graph so a file this server
    cannot read never becomes a run at all. A page that neither the native extractor nor
    the vision model could read is a 422: ingesting it as empty text would produce a
    register that looks clean because the evidence was missing, which is the one failure
    mode nobody can see from the report.

    The extract-then-start pair lives in `runtime.ingest_file`, which is also what the
    collection watcher drives. This endpoint is that function plus HTTP status codes.
    """
    try:
        run_id, extracted, result = await ingest_file(
            collection_id=collection_id,
            idempotency_key=idempotency_key,
            filename=file.filename or "upload",
            mime_type=file.content_type or "application/octet-stream",
            data=await file.read(),
            trigger="api",
            principal=principal,
        )
    except ExtractionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return {
        "run_id": str(run_id),
        "extraction_methods": sorted(extracted.methods),
        "extraction_warnings": extracted.warnings,
        "blocks": len(extracted.blocks),
        "result": result,
    }


@router.put("/collections/{collection_id}/ruleset")
async def put_ruleset(
    collection_id: UUID,
    payload: dict,
    principal: Caller,
    if_match: Annotated[int | None, Header(alias="If-Match")] = None,
) -> dict:
    """Upload a playbook. Rules are configuration: a new version changes behaviour
    without a code change.

    Content-idempotent: uploading exactly what is already active is a no-op and
    returns it unchanged. Changed content needs `If-Match` set to the version being
    replaced -- read it off a prior response's `version` field -- or a 409 refuses the
    upload rather than guessing which version the caller meant to supersede.
    """
    try:
        stored, created = await install_ruleset(collection_id, payload, if_match=if_match)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RulesetConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "ruleset_id": str(stored.id),
        "name": stored.name,
        "version": stored.version,
        "rules": [rule.code for rule in stored.rules],
        "created": created,
    }


@router.get("/runs/{run_id}/findings")
async def get_findings(
    run_id: UUID, principal: Caller
) -> list[dict]:
    services = await get_services()
    return [asdict(finding) for finding in await services.repository.list_findings(run_id)]


@router.get("/runs/{run_id}/events")
async def get_events(
    run_id: UUID, principal: Caller
) -> list[dict]:
    services = await get_services()
    return [asdict(event) for event in await services.repository.list_events(run_id)]


@router.get("/runs/{run_id}/review-items")
async def get_review_items(
    run_id: UUID, principal: Caller
) -> list[dict]:
    services = await get_services()
    return [asdict(item) for item in await services.repository.list_review_items(run_id)]


@router.post("/runs/{run_id}/rederive")
async def rederive(
    run_id: UUID, payload: RederiveCreate, principal: Caller
) -> dict:
    """Redo the proposals this run approved but could not commit, against the register
    as it now stands. Returns a new run, holding at its own review gate."""
    new_run_id, result = await rederive_run(
        collection_id=payload.collection_id,
        stale_run_id=run_id,
        idempotency_key=payload.idempotency_key,
    )
    return {"run_id": str(new_run_id), "result": result}


@router.post("/runs/{run_id}/resume")
async def resume(
    run_id: UUID, payload: ReviewResume, reviewer: Reviewer
) -> dict:
    try:
        return await decide_reviewed_items(
            run_id,
            payload.decisions,
            basis=payload.basis,
            idempotency_key=payload.idempotency_key,
            principal=reviewer,
            document_type=payload.document_type,
        )
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RunBusyError as exc:
        # Another process holds the run's lease. Retrying is the right response, and it
        # is a different answer from "your decision was rejected".
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (
        RunClosedError,
        IdempotencyConflictError,
        StaleDecisionError,
        KeyError,
        ValueError,
    ) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/runs/{run_id}/override")
async def override_blockers(
    run_id: UUID, payload: BlockerOverride, reviewer: Reviewer
) -> dict:
    """Answer the blocker gate. `override: false` leaves the run blocked and writes
    nothing; `override: true` needs a reason, which lands in the run's event log under
    the reviewer who presented the credential."""
    try:
        return await override_run_blockers(
            run_id,
            override=payload.override,
            reason=payload.reason,
            idempotency_key=payload.idempotency_key,
            principal=reviewer,
        )
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RunBusyError as exc:
        # Another process holds the run's lease. Retrying is the right response, and it
        # is a different answer from "your decision was rejected".
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (RunClosedError, IdempotencyConflictError, KeyError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/runs/{run_id}/status")
async def get_status(run_id: UUID, principal: Caller) -> dict:
    status = await get_run_status(run_id)
    if status is None:
        raise HTTPException(status_code=404, detail=f"run {run_id} does not exist")
    return status


@router.get("/runs/{run_id}/stages")
async def get_stages(run_id: UUID, principal: Caller) -> list[dict]:
    """This run's ordered stage history from the exactly-once ledger: what each stage was
    given, what it produced, and how many times it executed. `GET /runs/{id}/status`
    answers "where is it now"; this answers "what has it already done, exactly once"."""
    try:
        return await get_run_stages(run_id)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/cost")
async def get_cost_report(run_id: UUID, principal: Caller) -> dict:
    """What this run cost and where the time went: see `runtime.get_run_cost_report`."""
    try:
        return await get_run_cost_report(run_id)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/collections/{collection_id}/register")
async def get_register(collection_id: UUID, principal: Caller) -> list[dict]:
    services = await get_services()
    return [asdict(item) for item in await services.repository.list_register(collection_id)]


@router.get("/collections/{collection_id}/runs/{run_id}/export")
async def get_export(collection_id: UUID, run_id: UUID, principal: Caller) -> dict:
    """The collection's register as a self-contained artifact, evidence and all: see
    `runtime.export_register`. The MCP tool of the same name is this same function --
    one implementation, two ways in."""
    try:
        return await export_register(collection_id, run_id)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/changes")
async def get_run_changes(run_id: UUID, principal: Caller) -> list[dict]:
    """The register rows this run actually changed: old value, new value, both hashes.
    Empty for a run that never committed -- blocked, stale, or still open."""
    services = await get_services()
    return [asdict(change) for change in await services.repository.list_run_changes(run_id)]

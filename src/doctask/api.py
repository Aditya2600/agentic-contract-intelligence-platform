from __future__ import annotations

from dataclasses import asdict
from typing import Annotated
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
from doctask.runtime import (
    RunBusyError,
    get_services,
    rederive_run,
    resume_run,
    start_run,
)
from doctask.services.extraction import ExtractionError, extract_document
from doctask.services.rules import parse_ruleset

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


class CollectionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)


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


class BlockerOverride(BaseModel):
    override: bool
    # Required when override is true. Checked in the node too, which is the real gate.
    reason: str = ""


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/collections")
async def create_collection(
    payload: CollectionCreate, principal: Caller
) -> dict[str, str]:
    services = await get_services()
    collection_id = await services.repository.create_collection(payload.name)
    return {"collection_id": str(collection_id)}


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

    Extraction happens here rather than in the graph so a file this server cannot read
    never becomes a run at all. A page that neither the native extractor nor the vision
    model could read is a 422: ingesting it as empty text would produce a register that
    looks clean because the evidence was missing, which is the one failure mode nobody
    can see from the report.
    """
    services = await get_services()
    try:
        extracted = await extract_document(
            filename=file.filename or "upload",
            mime_type=file.content_type or "",
            data=await file.read(),
            ocr=services.model,
        )
    except ExtractionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    run_id, result = await start_run(
        collection_id=collection_id,
        idempotency_key=idempotency_key,
        filename=file.filename or "upload",
        mime_type=file.content_type or "application/octet-stream",
        text=extracted.text,
        blocks=[asdict(block) for block in extracted.blocks],
        extraction_warnings=extracted.warnings,
    )
    return {
        "run_id": str(run_id),
        "extraction_methods": sorted(extracted.methods),
        "extraction_warnings": extracted.warnings,
        "blocks": len(extracted.blocks),
        "result": result,
    }


@router.put("/collections/{collection_id}/ruleset")
async def put_ruleset(
    collection_id: UUID, payload: dict, principal: Caller
) -> dict:
    """Upload a playbook. Rules are configuration: a new version changes behaviour
    without a code change."""
    services = await get_services()
    try:
        ruleset = parse_ruleset(payload, collection_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    stored = await services.repository.put_ruleset(ruleset)
    return {
        "ruleset_id": str(stored.id),
        "name": stored.name,
        "version": stored.version,
        "rules": [rule.code for rule in stored.rules],
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
        return await resume_run(
            run_id,
            {
                "decisions": {str(key): value for key, value in payload.decisions.items()},
                "document_type": payload.document_type,
            },
            principal=reviewer,
        )
    except RunBusyError as exc:
        # Another process holds the run's lease. Retrying is the right response, and it
        # is a different answer from "your decision was rejected".
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/runs/{run_id}/override")
async def override_blockers(
    run_id: UUID, payload: BlockerOverride, reviewer: Reviewer
) -> dict:
    """Answer the blocker gate. `override: false` leaves the run blocked and writes
    nothing; `override: true` needs a reason, which lands in the run's event log under
    the reviewer who presented the credential."""
    try:
        return await resume_run(run_id, payload.model_dump(), principal=reviewer)
    except RunBusyError as exc:
        # Another process holds the run's lease. Retrying is the right response, and it
        # is a different answer from "your decision was rejected".
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

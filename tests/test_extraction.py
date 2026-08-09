"""Reading the file is where a clean report can start out wrong.

Every other stage reasons about text. If a page never made it into that text, nothing
downstream can tell the difference between a contract that says nothing about liability
and a scan nobody could read: both look like absent evidence, and the second one is a
silent failure. So these tests check two things -- that the real demo pack extracts
natively with its provenance intact, and that a page which cannot be read stops the run
instead of quietly becoming an empty page.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pymupdf
import pytest
from fastapi.testclient import TestClient

from doctask import runtime
from doctask.auth import REVIEWER, SERVICE
from doctask.config import settings
from doctask.domain import RegisterKey
from doctask.llm.fake import FakeLLM
from doctask.main import app
from doctask.services.extraction import (
    DOCX,
    GEMMA_VLM,
    NATIVE_PDF,
    TXT,
    ExtractionError,
    extract_document,
)

PACK = Path(__file__).resolve().parents[1] / "realistic_synthetic_demo_pack"

MSA = PACK / "01_Master_Services_Agreement_MSA-2026-014.pdf"
AMENDMENT = PACK / "02_Amendment_No_1_AMD-2026-014-01.pdf"
INVOICE = PACK / "03_Invoice_INV-2026-0417.pdf"
DPA = PACK / "04_Data_Processing_Addendum_DPA-2026-014-A.docx"
NOTICE = PACK / "05_Operational_Notice_OPS-NOTICE-2026-0528.txt"

PDF = "application/pdf"
WORD = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PLAIN = "text/plain"

REVIEWER_TOKEN = "reviewer-secret"
SERVICE_TOKEN = "service-secret"


@pytest.fixture(autouse=True)
def tokens(monkeypatch):
    monkeypatch.setattr(settings, "reviewer_tokens", f"{REVIEWER_TOKEN}:alice")
    monkeypatch.setattr(settings, "service_tokens", f"{SERVICE_TOKEN}:ingest-bot")


async def _extract(path: Path, mime: str, ocr=None):
    return await extract_document(
        filename=path.name, mime_type=mime, data=path.read_bytes(), ocr=ocr
    )


# ------------------------------------------------------------------ the demo pack


# What DEMO_GUIDE.md promises each file says. If native extraction drops a page or
# mangles a number, one of these disappears.
@pytest.mark.parametrize(
    ("path", "mime", "method", "paged", "promised"),
    [
        (MSA, PDF, NATIVE_PDF, True, ["thirty (30) calendar days", "USD 250,000", "sixty (60)"]),
        (AMENDMENT, PDF, NATIVE_PDF, True, ["forty-five (45) calendar days", "ninety (90)"]),
        (INVOICE, PDF, NATIVE_PDF, True, ["NET 10", "18,500.00"]),
        (DPA, WORD, DOCX, False, ["seventy-two (72) hours", "does not modify"]),
        (NOTICE, PLAIN, TXT, False, ["does not amend", "OPS-NOTICE-2026-0528"]),
    ],
    ids=["msa", "amendment", "invoice", "dpa", "notice"],
)
async def test_every_demo_file_extracts_natively(path, mime, method, paged, promised) -> None:
    # ocr=None on purpose: if any of these needed the vision model, that is a
    # regression in native extraction, and it should fail here rather than cost tokens.
    extracted = await _extract(path, mime)

    assert extracted.methods == {method}
    for phrase in promised:
        assert phrase in extracted.text, f"{path.name} lost {phrase!r}"

    pages = {block.page for block in extracted.blocks}
    if paged:
        assert pages == set(range(1, max(p for p in pages if p) + 1))
        assert min(pages) == 1
    else:
        assert pages == {None}

    # The document text is exactly the blocks joined, which is what lets a citation
    # recompute its own offsets.
    assert extracted.text == "\n\n".join(block.text for block in extracted.blocks)
    assert all(block.text.strip() == block.text for block in extracted.blocks)
    assert all("\n\n" not in block.text for block in extracted.blocks)


async def test_the_type_comes_from_the_mime_or_the_extension() -> None:
    by_extension = await extract_document(
        filename="notice.txt", mime_type="application/octet-stream", data=NOTICE.read_bytes()
    )
    assert by_extension.methods == {TXT}

    with pytest.raises(ExtractionError, match="unsupported"):
        await extract_document(filename="scan.tiff", mime_type="image/tiff", data=b"II*\x00")
    with pytest.raises(ExtractionError, match="empty"):
        await extract_document(filename="empty.txt", mime_type=PLAIN, data=b"")


async def test_bytes_that_are_not_utf8_are_refused_rather_than_mangled() -> None:
    """`errors="replace"` would turn a clause into evidence that reads plausibly and
    says something the document never said."""
    with pytest.raises(ExtractionError, match="UTF-8"):
        await extract_document(
            filename="latin.txt", mime_type=PLAIN, data="liability cap: £250,000".encode("latin-1")
        )


# ------------------------------------------------------------- the OCR fallback


def _scanned_pdf() -> bytes:
    """A page with a picture on it and no text layer: what a scan looks like."""
    document = pymupdf.open()
    page = document.new_page()
    page.draw_rect(page.rect, color=(0.1, 0.1, 0.1), fill=(0.85, 0.85, 0.85))
    return document.tobytes()


SCANNED_TEXT = (
    "10. Limitation of Liability\n\n"
    "Aggregate liability will not exceed two hundred fifty thousand U.S. dollars "
    "(USD 250,000)."
)


async def test_a_page_with_no_text_layer_is_read_by_the_vision_model() -> None:
    extracted = await extract_document(
        filename="scan.pdf",
        mime_type=PDF,
        data=_scanned_pdf(),
        ocr=FakeLLM(ocr_pages={1: SCANNED_TEXT}),
    )

    assert extracted.methods == {GEMMA_VLM}, "provenance must say a model read this, not the file"
    assert "USD 250,000" in extracted.text
    assert {block.page for block in extracted.blocks} == {1}


@pytest.mark.parametrize(
    ("ocr", "message"),
    [
        (None, "no OCR model is configured"),
        (FakeLLM(), "nothing legible"),
        (FakeLLM(ocr_pages={1: "�� �� ���"}), "nothing legible"),
    ],
    ids=["no-ocr", "ocr-blank", "ocr-garbled"],
)
async def test_an_unreadable_page_fails_the_document_instead_of_becoming_an_empty_one(
    ocr, message
) -> None:
    """The whole point. An empty page is indistinguishable downstream from a page that
    genuinely says nothing, so it must never be produced."""
    with pytest.raises(ExtractionError, match=message):
        await extract_document(filename="scan.pdf", mime_type=PDF, data=_scanned_pdf(), ocr=ocr)


async def test_an_ocr_call_that_raises_is_not_swallowed() -> None:
    class BrokenOCR:
        async def read_page(self, image_png: bytes, *, page: int) -> str:
            raise RuntimeError("vision model unreachable")

    with pytest.raises(ExtractionError, match="OCR failed"):
        await extract_document(
            filename="scan.pdf", mime_type=PDF, data=_scanned_pdf(), ocr=BrokenOCR()
        )


async def test_a_readable_page_never_pays_for_the_vision_model() -> None:
    class RefusingOCR:
        async def read_page(self, image_png: bytes, *, page: int) -> str:
            raise AssertionError("native extraction was usable; OCR must not be called")

    extracted = await _extract(MSA, PDF, ocr=RefusingOCR())
    assert extracted.methods == {NATIVE_PDF}


# ------------------------------------------------------------------ end to end


@pytest.fixture
async def client():
    with TestClient(app) as test_client:
        yield test_client
    await runtime.shutdown_services()


def _service_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {SERVICE_TOKEN}"}


def _collection(client: TestClient) -> str:
    response = client.post("/api/collections", json={"name": "acme"}, headers=_service_headers())
    assert response.status_code == 200
    return response.json()["collection_id"]


def _upload(client: TestClient, collection_id: str, path: Path, mime: str):
    return client.post(
        "/api/runs/upload",
        data={"collection_id": collection_id, "idempotency_key": f"{path.stem}-{uuid4()}"},
        files={"file": (path.name, path.read_bytes(), mime)},
        headers=_service_headers(),
    )


@pytest.mark.parametrize(
    ("path", "mime", "method"),
    [
        (MSA, PDF, NATIVE_PDF),
        (AMENDMENT, PDF, NATIVE_PDF),
        (INVOICE, PDF, NATIVE_PDF),
        (DPA, WORD, DOCX),
        (NOTICE, PLAIN, TXT),
    ],
    ids=["msa", "amendment", "invoice", "dpa", "notice"],
)
async def test_each_demo_file_uploads_and_reaches_a_human(client, path, mime, method) -> None:
    collection_id = _collection(client)
    response = _upload(client, collection_id, path, mime)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["extraction_methods"] == [method]
    assert body["blocks"] > 0

    services = await runtime.get_services()
    events = await services.repository.list_events(UUID(body["run_id"]))
    stages = [event.stage for event in events]
    parsed = next((event for event in events if event.stage == "parse_blocks"), None)
    if parsed is None:
        # The offline model could not type this document, so it is waiting on a human
        # before it reads any further. That is a stop, not a silent finish.
        assert stages[-1] == "classify", stages
    else:
        assert method in parsed.reason, "the run must record how it read the text it reasoned over"

    # Nothing reaches the register on its own: a run either stops for a human or
    # proposes nothing at all (the DPA and the operational notice carry no obligation
    # this ontology tracks).
    stopped = bool(body["result"].get("__interrupt__"))
    assert stopped or not await services.repository.list_review_items(UUID(body["run_id"]))


async def test_uploaded_blocks_keep_the_page_and_extractor_behind_every_quote(client) -> None:
    collection_id = _collection(client)
    assert _upload(client, collection_id, MSA, PDF).status_code == 200
    services = await runtime.get_services()

    documents = [
        document
        for document in services.repository.documents.values()
        if document.collection_id == UUID(collection_id)
    ]
    assert len(documents) == 1
    blocks = sorted(
        (await services.repository.get_blocks(documents[0].id)).values(), key=lambda b: b.index
    )

    assert blocks, "no blocks stored"
    assert {block.extraction_method for block in blocks} == {NATIVE_PDF}
    assert {block.page for block in blocks} == {1, 2, 3}
    # Offsets still index into the document text, so quotes stay verifiable.
    for block in blocks:
        assert documents[0].text[block.char_start : block.char_end] == block.text
    # The liability cap is on page 2 and a reviewer can be sent straight there.
    cap = next(block for block in blocks if "USD 250,000" in block.text)
    assert cap.page == 2


async def test_the_pipeline_reads_the_real_contract_numbers(client) -> None:
    """Extraction is only worth anything if the numbers survive into proposals."""
    collection_id = _collection(client)
    run_id = _upload(client, collection_id, MSA, PDF).json()["run_id"]

    items = client.get(
        f"/api/runs/{run_id}/review-items", headers={"Authorization": f"Bearer {REVIEWER_TOKEN}"}
    ).json()
    proposed = {
        RegisterKey.parse(item["target_key"]).key: item["payload"]["after"]["value"]
        for item in items
        if item["kind"] == "register_update"
    }
    assert proposed["payment_due_days"]["days"] == 30
    assert proposed["notice_days"]["days"] == 60
    assert proposed["liability_cap"]["amount"] == "$250,000"


async def test_an_unreadable_upload_produces_no_run_at_all(client) -> None:
    """A 422 and nothing else. If this ever became a run, its report would show a clean
    register and no findings -- the failure would look like a result."""
    collection_id = _collection(client)
    services = await runtime.get_services()
    runs_before = len(services.repository.runs)

    response = client.post(
        "/api/runs/upload",
        data={"collection_id": collection_id, "idempotency_key": f"scan-{uuid4()}"},
        files={"file": ("scan.pdf", _scanned_pdf(), PDF)},
        headers=_service_headers(),
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "page 1" in detail and "no readable text" in detail
    assert len(services.repository.runs) == runs_before
    assert not services.repository.documents


def test_upload_needs_a_credential(client) -> None:
    response = client.post(
        "/api/runs/upload",
        data={"collection_id": str(uuid4()), "idempotency_key": "x"},
        files={"file": (NOTICE.name, NOTICE.read_bytes(), PLAIN)},
    )
    assert response.status_code == 401


def test_a_reviewer_credential_is_not_needed_to_feed_documents_in(client) -> None:
    """Ingestion is the model's half of the split: a service may propose, never decide."""
    collection_id = _collection(client)
    assert _upload(client, collection_id, NOTICE, PLAIN).status_code == 200
    assert SERVICE != REVIEWER

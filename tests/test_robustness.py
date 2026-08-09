"""What the extractor does with PDFs that are hard to read.

Two outcomes are acceptable for every file here, and a third is not. The extractor may
read the page, or it may refuse the document. What it may never do is return a page it
did not really read, because a missing clause and an absent obligation are the same
thing to every stage downstream: the rules find nothing to object to and the report says
so. Anything the extractor was unsure of therefore travels with the run and makes
`clean` false, whatever the verdicts say.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from doctask import runtime
from doctask.config import settings
from doctask.llm.fake import FakeLLM
from doctask.main import app
from doctask.services.extraction import (
    GEMMA_VLM,
    NATIVE_PDF,
    ExtractionError,
    extract_document,
)
from tests.robustness_corpus import (
    LIABILITY_CLAUSE,
    NOTICE_CLAUSE,
    PAYMENT_CLAUSE,
    corrupt_pdf,
    empty_page_pdf,
    encrypted_pdf,
    multi_column_pdf,
    readable_pdf,
    rotated_pdf,
    scanned_pdf,
    table_heavy_pdf,
    truncated_pdf,
)

PDF = "application/pdf"
SERVICE_TOKEN = "service-secret"


@pytest.fixture(autouse=True)
def tokens(monkeypatch):
    monkeypatch.setattr(settings, "reviewer_tokens", "reviewer-secret:alice")
    monkeypatch.setattr(settings, "service_tokens", f"{SERVICE_TOKEN}:ingest-bot")


async def _extract(data: bytes, ocr=None, name: str = "case.pdf"):
    return await extract_document(filename=name, mime_type=PDF, data=data, ocr=ocr)


# ------------------------------------------------------------------ read it properly


async def test_a_readable_page_is_read_with_no_caveats() -> None:
    """The control. If this ever warns, the warnings below prove nothing."""
    extracted = await _extract(readable_pdf())
    assert extracted.warnings == []
    assert extracted.methods == {NATIVE_PDF}
    for clause in (PAYMENT_CLAUSE, LIABILITY_CLAUSE, NOTICE_CLAUSE):
        assert _flat(clause) in _flat(extracted.text)


def _flat(text: str) -> str:
    """Compare prose without caring where the renderer wrapped the lines."""
    return " ".join(text.split())


@pytest.mark.parametrize("rotation", [90, 180, 270])
async def test_a_rotated_page_is_still_read_and_says_so(rotation) -> None:
    extracted = await _extract(rotated_pdf(rotation))
    assert _flat(PAYMENT_CLAUSE) in _flat(extracted.text)
    assert _flat(LIABILITY_CLAUSE) in _flat(extracted.text)
    assert any(f"rotated {rotation}" in warning for warning in extracted.warnings)


async def test_two_columns_are_read_one_after_the_other_not_interleaved() -> None:
    """Ordering blocks by vertical position alone splices the columns together: the
    right column's first line sits level with the left column's first line, so every
    clause comes out cut through by a clause it has nothing to do with, and neither can
    be quoted verbatim by anything downstream."""
    extracted = await _extract(multi_column_pdf())
    flat = _flat(extracted.text)

    assert _flat(PAYMENT_CLAUSE) in flat, "the left column's clause was broken up"
    assert _flat(LIABILITY_CLAUSE) in flat, "the right column's clause was broken up"
    assert flat.index("LEFT COLUMN") < flat.index("RIGHT COLUMN")
    # And the left column is finished before the right one starts.
    assert flat.index("This column continues down the page.") < flat.index("RIGHT COLUMN")


async def test_a_table_heavy_page_is_read_as_rows_not_sent_to_a_model() -> None:
    """A fee schedule is mostly digits and separators, which reads as mojibake to a
    letter-density check. Judging it that way sends a perfectly legible table to OCR, or
    fails the upload over a page that extracted correctly."""

    class RefusingOCR:
        async def read_page(self, image_png: bytes, *, page: int) -> str:
            raise AssertionError("a legible table must not be sent to the vision model")

    extracted = await _extract(table_heavy_pdf(), ocr=RefusingOCR())
    assert extracted.methods == {NATIVE_PDF}
    # Cells keep their row, so an amount can still be tied to what it is an amount for.
    assert "TOTAL DUE | 18,500.00" in extracted.text
    assert "Managed Kubernetes | 1 | 12,000.00 | 12,000.00" in extracted.text
    assert any("table" in warning for warning in extracted.warnings)


async def test_a_scanned_page_is_read_by_the_model_and_marked_as_such() -> None:
    extracted = await _extract(scanned_pdf(), ocr=FakeLLM(ocr_pages={1: LIABILITY_CLAUSE}))
    assert extracted.methods == {GEMMA_VLM}
    assert any("vision model" in warning for warning in extracted.warnings)


async def test_an_owner_locked_file_opens_and_reports_that_it_was_encrypted() -> None:
    extracted = await _extract(encrypted_pdf())
    assert _flat(PAYMENT_CLAUSE) in _flat(extracted.text)
    assert any("encrypted" in warning for warning in extracted.warnings)


async def test_a_repaired_file_says_it_was_repaired() -> None:
    """MuPDF rebuilds a damaged file and reads it. Nothing inside can say what the
    rebuild dropped, so the run is told it happened."""
    extracted = await _extract(truncated_pdf(), ocr=FakeLLM(ocr_pages={1: LIABILITY_CLAUSE}))
    assert any("repaired" in warning for warning in extracted.warnings)


# --------------------------------------------------------------------- or refuse it


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (corrupt_pdf(), "unreadable PDF"),
        (encrypted_pdf(user_password="secret"), "password protected"),
        (empty_page_pdf(), "no readable text"),
        (scanned_pdf(), "no readable text"),
        (b"", "empty"),
    ],
    ids=["corrupt", "password-locked", "empty-page", "scanned-without-ocr", "zero-bytes"],
)
async def test_a_file_that_cannot_be_read_is_refused_not_ingested_blank(data, message) -> None:
    with pytest.raises(ExtractionError, match=message):
        await _extract(data)


# --------------------------------------------- uncertainty reaches the report


@pytest.fixture
async def client():
    with TestClient(app) as test_client:
        yield test_client
    await runtime.shutdown_services()


PLAYBOOK = {
    "name": "Robustness playbook",
    "version": 1,
    "rules": [
        {
            "code": "PAY-01",
            "severity": "major",
            "scope": "both",
            "keys": ["payment_due_days"],
            "text": "Payment terms must allow at least 30 calendar days before undisputed "
            "amounts are due.",
        }
    ],
}


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {SERVICE_TOKEN}"}


def _collection(client: TestClient) -> str:
    collection_id = client.post(
        "/api/collections", json={"name": "robustness"}, headers=_headers()
    ).json()["collection_id"]
    assert (
        client.put(
            f"/api/collections/{collection_id}/ruleset", json=PLAYBOOK, headers=_headers()
        ).status_code
        == 200
    )
    return collection_id


def _upload(client: TestClient, collection_id: str, data: bytes, name: str) -> dict:
    response = client.post(
        "/api/runs/upload",
        data={"collection_id": collection_id, "idempotency_key": f"{name}-{uuid4()}"},
        files={"file": (f"{name}.pdf", data, PDF)},
        headers=_headers(),
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _finish(client: TestClient, body: dict) -> dict:
    """Approve everything a human is asked about, and return the run's report."""
    run_id = body["run_id"]
    result = body["result"]
    reviewer = {"Authorization": "Bearer reviewer-secret"}
    services = await runtime.get_services()
    while "report" not in result:
        # Over HTTP the interrupt arrives as JSON, not as the Interrupt object.
        interrupt = result["__interrupt__"][0]
        gate = (interrupt["value"] if isinstance(interrupt, dict) else interrupt.value)["kind"]
        if gate == "document_classification":
            payload = {"document_type": "master_agreement"}
        elif gate == "blocker_override":
            payload = {"override": False, "reason": ""}
        else:
            pending = [
                item
                for item in await services.repository.list_review_items(UUID(run_id))
                if item.state == "pending"
            ]
            payload = {"decisions": {str(item.id): "approved" for item in pending}}
        endpoint = "override" if gate == "blocker_override" else "resume"
        response = client.post(f"/api/runs/{run_id}/{endpoint}", json=payload, headers=reviewer)
        assert response.status_code == 200, response.text
        result = response.json()
    return result["report"]


async def test_a_clean_run_is_possible_when_nothing_was_uncertain(client) -> None:
    """The other half of the claim below. A control that can never be clean proves
    nothing about a rule that forces `clean` false."""
    collection_id = _collection(client)
    body = _upload(client, collection_id, readable_pdf(), "readable")
    assert body["extraction_warnings"] == []

    report = await _finish(client, body)
    assert report["rules"]["extraction_certain"] is True
    assert report["rules"]["clean"] is True, report["rules"]


@pytest.mark.parametrize(
    ("name", "build"),
    [
        ("rotated", rotated_pdf),
        ("table", table_heavy_pdf),
        ("encrypted", encrypted_pdf),
    ],
    ids=["rotated", "table-heavy", "encrypted"],
)
async def test_extraction_uncertainty_forces_clean_false(client, name, build) -> None:
    """Every rule can pass and the run still cannot call itself clean.

    These pages were read, and read in a way that might not be exactly what the file
    says. A `clean` that cannot see the difference is the failure this pipeline exists
    to prevent: it reports an absence of findings as an absence of problems.
    """
    collection_id = _collection(client)
    body = _upload(client, collection_id, build(), name)
    assert body["extraction_warnings"], "this case is supposed to be an uncertain read"

    report = await _finish(client, body)
    rules = report["rules"]
    assert rules["violation"] == 0, "the point is a run with nothing wrong in it"
    assert rules["extraction_certain"] is False
    assert rules["extraction_warnings"] == body["extraction_warnings"]
    assert rules["clean"] is False


async def test_a_rederived_run_inherits_the_original_reads_uncertainty() -> None:
    """A re-derivation re-reads nothing: no extractor runs, so nothing in that run can
    rediscover that the page was rotated or transcribed by a model. Starting it with an
    empty warning list would let the retry of an uncertain document report
    `extraction_certain: true` and claim a clean result the first run could not."""
    from doctask.domain import Document, ReviewItem, RunEvent
    from doctask.graph.nodes import UNCERTAIN_PREFIX, NodeDependencies, make_nodes
    from doctask.repositories.memory import InMemoryRepository

    repository = InMemoryRepository()
    collection_id = await repository.create_collection("rederive")
    document, _ = await repository.put_document(
        Document(
            collection_id=collection_id,
            filename="rotated.pdf",
            mime_type=PDF,
            text=PAYMENT_CLAUSE,
            sha256="sha-rotated",
        )
    )
    source_run = uuid4()
    await repository.add_event(
        RunEvent(
            run_id=source_run,
            stage="ingest",
            decision="uncertain",
            reason=UNCERTAIN_PREFIX + "page 1 is rotated 90 degrees; page 2 was read by a "
            "vision model, not from the file",
            next_node="classify",
            error_class="extraction",
        )
    )
    await repository.add_review_items(
        [
            ReviewItem(
                run_id=source_run,
                kind="register_update",
                target_key="payment_due_days",
                state="approved",
                payload={
                    "document_id": str(document.id),
                    "after": {"content_hash": "never-landed"},
                },
            )
        ]
    )

    nodes = make_nodes(NodeDependencies(repository=repository, model=FakeLLM()))
    command = await nodes["rederive"](
        {
            "run_id": str(uuid4()),
            "collection_id": str(collection_id),
            "rederive_from_run": str(source_run),
        }
    )
    assert command.update["extraction_warnings"] == [
        "page 1 is rotated 90 degrees",
        "page 2 was read by a vision model, not from the file",
    ]


async def test_the_reason_a_run_could_not_be_clean_is_in_the_event_log(client) -> None:
    """Not only in the report. A run is audited from its events."""
    collection_id = _collection(client)
    body = _upload(client, collection_id, rotated_pdf(), "rotated")

    services = await runtime.get_services()
    events = await services.repository.list_events(UUID(body["run_id"]))
    uncertain = [event for event in events if event.decision == "uncertain"]
    assert len(uncertain) == 1
    assert "rotated 90" in uncertain[0].reason
    assert uncertain[0].error_class == "extraction"

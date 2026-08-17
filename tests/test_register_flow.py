"""End-to-end proof of the incremental register, offline.

Same corpus as `make demo`: MSA, then the amendment, then the contradicting invoice.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest
from langgraph.types import Command

from doctask.domain import RegisterKey
from doctask.graph.builder import build_graph
from doctask.graph.nodes import NodeDependencies
from doctask.llm.fake import FakeLLM
from doctask.repositories.memory import InMemoryRepository

CORPUS = Path(__file__).resolve().parent.parent / "data" / "sample_data"

# This corpus is one unnamed agreement, so every register row lives in the "" bucket and
# the obligation key alone still identifies it. The scoped form is what the graph passes
# around, so the tests translate rather than assuming either shape.
def scoped(key: str) -> str:
    return RegisterKey("", key).text


def obligation(target_key: str) -> str:
    return RegisterKey.parse(target_key).key


class Harness:
    def __init__(self) -> None:
        self.repository = InMemoryRepository()
        self.graph = build_graph(
            NodeDependencies(repository=self.repository, model=FakeLLM())
        )

    async def run(self, filename: str, decide) -> dict:
        """Ingest one document, then decide every review item it produced."""
        run_id = uuid4()
        config = {"configurable": {"thread_id": str(run_id)}}
        result = await self.graph.ainvoke(
            {
                "run_id": str(run_id),
                "collection_id": str(self.collection_id),
                "idempotency_key": filename,
                "input_document": {
                    "filename": filename,
                    "mime_type": "text/plain",
                    "text": (CORPUS / filename).read_text(),
                },
                "validation_attempt": 0,
                "status": "running",
            },
            config=config,
        )
        # Two human gates: register proposals first, then deliverable findings.
        while "report" not in result:
            pending = [
                item
                for item in await self.repository.list_review_items(run_id)
                if item.state == "pending"
            ]
            result = await self.graph.ainvoke(
                Command(
                    resume={
                        "actor_id": "reviewer-1",
                        "actor_role": "reviewer",
                        "decisions": {str(item.id): decide(item) for item in pending},
                    }
                ),
                config=config,
            )
        self.last_items = await self.repository.list_review_items(run_id)
        return result["report"]

    async def register(self) -> dict[str, object]:
        return {
            item.key: item for item in await self.repository.list_register(self.collection_id)
        }


@pytest.fixture
async def harness():
    harness = Harness()
    harness.collection_id = await harness.repository.create_collection("acme")
    harness.last_items = []
    return harness


async def test_msa_creates_grounded_register_items(harness) -> None:
    report = await harness.run("vendor_msa.txt", lambda item: "approved")

    register = await harness.register()
    assert sorted(register) == ["liability_cap", "notice_days", "payment_due_days"]
    assert register["payment_due_days"].value == {"days": 30, "anchor": "receipt"}
    assert all(item.state == "supported" for item in register.values())
    assert all(item.citation_fact_ids for item in register.values())
    assert report["open_conflicts"] == []


async def test_amendment_touches_only_its_own_keys(harness) -> None:
    await harness.run("vendor_msa.txt", lambda item: "approved")
    before = await harness.register()

    report = await harness.run(
        "amendment_1.txt",
        lambda item: "approved" if obligation(item.target_key) == "payment_due_days" else "rejected",
    )

    proposed = {
        obligation(item.target_key)
        for item in harness.last_items
        if item.kind in {"register_update", "conflict"}
    }
    assert proposed == {"payment_due_days", "notice_days"}
    # liability_cap is in the affected set through the citation index, is re-derived,
    # and produces no proposal because nothing about it changed.
    assert scoped("liability_cap") in report["affected_keys"]
    assert scoped("liability_cap") in report["unchanged_keys"]

    after = await harness.register()
    assert after["liability_cap"].content_hash == before["liability_cap"].content_hash
    assert after["liability_cap"].version == 1
    assert after["payment_due_days"].value == {"days": 45, "anchor": "receipt"}
    assert after["payment_due_days"].version == 2
    # The rejected supersession leaves both the value and the conflict alone.
    assert after["notice_days"].value == {"days": 60}
    assert after["notice_days"].version == 1

    conflicts = await harness.repository.list_conflicts(harness.collection_id, state=None)
    assert {(obligation(c.key), c.kind, c.state) for c in conflicts} == {
        ("payment_due_days", "supersession_candidate", "accepted"),
        ("notice_days", "supersession_candidate", "open"),
    }


async def test_an_invoices_own_payment_terms_do_not_contradict_the_contract(harness) -> None:
    """NET 10 on an invoice is an operational term, not a competing contract term.

    The invoice is issued under the agreement; it does not amend it. Filing its billing
    terms as a rival answer to the same question opened a conflict on every invoice a
    vendor ever sent and marked the contract's own term `disputed` -- the register said
    the parties disagreed about payment when the only disagreement was that someone
    billed on the wrong terms.
    """
    await harness.run("vendor_msa.txt", lambda item: "approved")
    await harness.run("invoice_001.txt", lambda item: "approved")

    proposals = {obligation(item.target_key): item for item in harness.last_items}
    assert "payment_due_days" not in proposals  # nothing to decide; nothing changed
    assert proposals["invoice_amount_due"].payload["conflict"] is None

    register = await harness.register()
    assert register["payment_due_days"].value == {"days": 30, "anchor": "receipt"}
    assert register["payment_due_days"].state == "supported"
    assert register["payment_due_days"].version == 1
    assert register["invoice_amount_due"].value == {"amount": "$18,500.00", "currency": "USD"}
    assert await harness.repository.list_conflicts(harness.collection_id, state=None) == []

    # The invoice's term is not dropped: it is stored, scoped, and citable. It simply is
    # not evidence about what the contract says.
    facts = await harness.repository.get_active_facts(
        harness.collection_id, ["payment_due_days"]
    )
    assert {fact.value["days"]: fact.scope.obligation_scope for fact in facts} == {
        30: "contractual",
        10: "operational",
    }
    # Only the contract term grounds the register item.
    assert len(register["payment_due_days"].citation_fact_ids) == 1


async def test_injected_instructions_cannot_approve_anything(harness) -> None:
    await harness.run("vendor_msa.txt", lambda item: "approved")
    report = await harness.run("invoice_001.txt", lambda item: "rejected")

    assert report["injection_flag"] is True
    assert all(item.payload["force_review"] for item in harness.last_items)
    assert all(item.state == "rejected" for item in harness.last_items)
    register = await harness.register()
    assert "invoice_amount_due" not in register
    assert register["payment_due_days"].state == "supported"


async def test_reuploading_the_same_document_spends_nothing(harness) -> None:
    await harness.run("vendor_msa.txt", lambda item: "approved")
    before = await harness.register()

    report = await harness.run("vendor_msa.txt", lambda item: "approved")

    assert report["status"] == "duplicate_noop"
    assert harness.last_items == []
    after = await harness.register()
    assert {key: item.content_hash for key, item in after.items()} == {
        key: item.content_hash for key, item in before.items()
    }
    stages = [event.stage for event in await harness.repository.list_events(UUID(report["run_id"]))]
    assert "extract_facts" not in stages

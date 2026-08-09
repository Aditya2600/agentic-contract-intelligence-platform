"""Two numbers under one register key are only in conflict when they answer one question.

Every test here is a false conflict the system used to open, or the one real ambiguity it
used to resolve by guessing. The corpus is a collection holding two unrelated master
agreements, an amendment aimed at one of them, an invoice issued under one of them, and an
amendment that names none of them.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest
from langgraph.types import Command

from doctask.domain import FactScope, RegisterKey
from doctask.graph.builder import build_graph
from doctask.graph.nodes import NodeDependencies
from doctask.llm.fake import FakeLLM
from doctask.repositories.memory import InMemoryRepository
from doctask.services.relations import agreement_ref, detect_relations, self_declared_ref
from doctask.services.scoping import obligation_scope_for, parties_in, scope_for

CORPUS = Path(__file__).resolve().parent.parent / "sample_data"

ALPHA = "msa_alpha.txt"
BETA = "msa_beta.txt"
AMENDMENT = "amendment_alpha.txt"
INVOICE = "invoice_alpha.txt"
AMBIGUOUS = "amendment_ambiguous.txt"

ALPHA_ID = "MSA-2024-001"
BETA_ID = "MSA-2024-002"


def scoped(agreement_id: str, key: str) -> str:
    return RegisterKey(agreement_id, key).text


class Harness:
    def __init__(self) -> None:
        self.repository = InMemoryRepository()
        self.graph = build_graph(NodeDependencies(repository=self.repository, model=FakeLLM()))

    async def run(self, filename: str, decide=lambda item: "approved") -> dict:
        run_id = uuid4()
        self.run_id = run_id
        self.gates: list[str] = []
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
        while "report" not in result:
            self.gates.append(result["__interrupt__"][0].value["kind"])
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

    async def register(self) -> dict:
        """Keyed the way the register is keyed: one row per agreement per obligation."""
        return {
            item.register_key.text: item
            for item in await self.repository.list_register(self.collection_id)
        }

    async def conflicts(self) -> list:
        return await self.repository.list_conflicts(self.collection_id, state=None)

    async def facts(self, key: str) -> list:
        return await self.repository.get_active_facts(self.collection_id, [key])


@pytest.fixture
async def harness():
    harness = Harness()
    harness.collection_id = await harness.repository.create_collection("acme")
    harness.last_items = []
    return harness


# ------------------------------------------------------------------ reading the claims


def _blocks(filename: str):
    """The blocks the graph would build, without running it."""
    from doctask.domain import Block
    from doctask.services.hashing import sha256_text

    text = (CORPUS / filename).read_text()
    return [
        Block(
            document_id=uuid4(),
            index=index,
            text=paragraph.strip(),
            text_sha256=sha256_text(paragraph.strip()),
            char_start=0,
            char_end=len(paragraph),
        )
        for index, paragraph in enumerate(text.split("\n\n"))
        if paragraph.strip()
    ]


def test_an_agreement_declares_its_own_identifier_and_an_invoice_does_not() -> None:
    """An invoice quoting an agreement number is naming someone else's agreement.

    Reading it as the invoice's own identity would file the invoice's billing terms as
    that agreement's contract terms, which is the mistake with the worst blast radius
    here: it makes an operational number authoritative over a negotiated one.
    """
    assert self_declared_ref(_blocks(ALPHA), doc_type="master_agreement") == "MSA-2024-001"
    assert self_declared_ref(_blocks(BETA), doc_type="master_agreement") == "MSA-2024-002"
    assert self_declared_ref(_blocks(INVOICE), doc_type="invoice") is None
    assert self_declared_ref(_blocks(AMENDMENT), doc_type="amendment") is None
    # The invoice still names the agreement it is issued under; that is a relation.
    assert agreement_ref((CORPUS / INVOICE).read_text()) == "MSA-2024-001"


def test_every_relation_carries_the_sentence_that_claims_it() -> None:
    relations = detect_relations(_blocks(AMENDMENT))

    kinds = {relation.kind for relation in relations}
    assert {"amends", "supersedes"} <= kinds
    assert all(relation.target_ref == "MSA-2024-001" for relation in relations)
    assert all(relation.block_id is not None and relation.quote for relation in relations)
    # The claim itself is quoted, not summarised: a relation nobody can re-read is not
    # evidence, and which agreement it names is what scopes every fact in this document.
    assert any(
        "amends Agreement No. MSA-2024-001" in relation.quote for relation in relations
    )

    # An invoice is issued under an agreement; it does not change one.
    invoice_kinds = {relation.kind for relation in detect_relations(_blocks(INVOICE))}
    assert "governed_by" in invoice_kinds
    assert "amends" not in invoice_kinds and "supersedes" not in invoice_kinds

    # An amendment that names no agreement still claims the relation, with no target.
    ambiguous = detect_relations(_blocks(AMBIGUOUS))
    assert {relation.kind for relation in ambiguous} >= {"amends", "supersedes"}
    assert all(relation.target_ref == "" for relation in ambiguous)


def test_an_operational_document_becomes_contractual_only_on_amendment_evidence() -> None:
    assert obligation_scope_for("invoice", amends=False) == "operational"
    assert obligation_scope_for("purchase_order", amends=False) == "operational"
    # An invoice that says it amends the agreement is making a contract claim, and gets
    # judged as one -- by a human, at the conflict gate.
    assert obligation_scope_for("invoice", amends=True) == "contractual"
    assert obligation_scope_for("master_agreement", amends=False) == "contractual"


def test_scope_reads_parties_dates_clause_and_conditions_from_the_text() -> None:
    text = (CORPUS / ALPHA).read_text()
    scope = scope_for(
        doc_type="master_agreement",
        document_text=text,
        block_text="Section 4.3 Payment is due within 30 calendar days of receipt "
        "unless disputed in good faith.",
        quote="Payment is due within 30 calendar days",
        agreement_id="MSA-2024-001",
        amends=False,
    )

    assert scope.agreement_id == "MSA-2024-001"
    assert scope.parties == ("Acme Buyer LLC", "Northstar Vendor Inc")
    assert scope.effective_from == "2024-01-15"
    assert scope.clause == "4.3"
    assert scope.conditions == ("unless disputed in good faith",)
    assert scope.obligation_scope == "contractual"
    assert parties_in("no parties named here") == ()


def test_comparability_ignores_clause_and_dates_but_not_scope_or_conditions() -> None:
    base = FactScope(agreement_id="A", parties=("X", "Y"))

    # An amendment's clause 1 replaces the MSA's clause 4.3, on a later date. Requiring
    # either to match would make every amendment incomparable with what it amends.
    assert base.comparable_to(
        FactScope(agreement_id="A", parties=("X", "Y"), clause="1", effective_from="2025-01-01")
    )
    assert not base.comparable_to(FactScope(agreement_id="B", parties=("X", "Y")))
    assert not base.comparable_to(
        FactScope(agreement_id="A", parties=("X", "Y"), obligation_scope="operational")
    )
    assert not base.comparable_to(
        FactScope(agreement_id="A", parties=("X", "Y"), conditions=("if paid by ach",))
    )
    # Unknown on either side compares: a collection whose documents declare nothing is
    # the ordinary single-agreement case, and refusing to compare there hides real
    # conflicts rather than preventing false ones.
    assert base.comparable_to(FactScope())
    assert FactScope().comparable_to(base)


# ----------------------------------------------------------------- end to end, in order


async def test_two_unrelated_agreements_keep_separate_payment_terms(harness) -> None:
    """NET 30 in one company's MSA is not a rival answer to NET 45 in another's.

    Not opening a conflict was only half the fix. While the register was keyed by
    obligation alone, both agreements still wrote the same row, so Beta's upload
    overwrote Alpha's term -- with no conflict raised, because two terms in different
    agreements correctly are not one. Silent, and worse than the false conflict.
    """
    await harness.run(ALPHA)
    report = await harness.run(BETA)

    assert report["agreement_id"] == BETA_ID
    assert await harness.conflicts() == []

    register = await harness.register()
    assert register[scoped(ALPHA_ID, "payment_due_days")].value["days"] == 30
    assert register[scoped(BETA_ID, "payment_due_days")].value["days"] == 45
    assert {item.state for item in register.values()} == {"supported"}
    # Beta's upload created a row; it did not version Alpha's.
    assert register[scoped(ALPHA_ID, "payment_due_days")].version == 1
    assert register[scoped(BETA_ID, "payment_due_days")].version == 1

    facts = await harness.facts("payment_due_days")
    assert {fact.value["days"]: fact.scope.agreement_id for fact in facts} == {
        30: ALPHA_ID,
        45: BETA_ID,
    }
    # Beta proposed its own row, against no incumbent -- there was nothing of Beta's
    # there before, and Alpha's row is not Beta's to move.
    proposal = next(
        item
        for item in harness.last_items
        if item.target_key == scoped(BETA_ID, "payment_due_days")
    )
    assert proposal.payload["before"] is None
    assert proposal.payload["after"]["value"] == {"days": 45, "anchor": "receipt"}


async def test_amending_one_agreement_leaves_the_other_rows_byte_identical(harness) -> None:
    """The proof that a register row belongs to one agreement: amend Alpha, hash Beta.

    A content hash and a version are the two things a downstream reader trusts to mean
    "this did not change". If amending Alpha moves either of Beta's, then Beta's row was
    never Beta's, and every incremental-update guarantee in the system is decorative.
    """
    await harness.run(ALPHA)
    await harness.run(BETA)
    beta_before = (await harness.register())[scoped(BETA_ID, "payment_due_days")]

    report = await harness.run(AMENDMENT)

    register = await harness.register()
    alpha = register[scoped(ALPHA_ID, "payment_due_days")]
    beta = register[scoped(BETA_ID, "payment_due_days")]

    assert alpha.value["days"] == 60  # the amendment landed where it was aimed
    assert alpha.version == 2
    assert beta.value == beta_before.value
    assert beta.content_hash == beta_before.content_hash
    assert beta.version == beta_before.version == 1
    # And the run says so itself, rather than leaving it to be diffed after the fact.
    assert scoped(BETA_ID, "payment_due_days") not in report["affected_keys"]
    assert report["register_by_agreement"][BETA_ID] == [
        {
            "key": "payment_due_days",
            "value": beta_before.value,
            "state": beta_before.state,
            "version": 1,
            "content_hash": beta_before.content_hash,
        }
    ]


async def test_an_amendment_replaces_the_agreement_it_names_and_no_other(harness) -> None:
    """The fallback aimed every amendment at whichever agreement was uploaded last.

    With two agreements in the collection, an amendment to the January MSA rewrote a term
    in the June one. The amendment names its target; the named target wins over recency.
    """
    await harness.run(ALPHA)
    await harness.run(BETA)
    report = await harness.run(AMENDMENT)

    assert report["agreement_id"] == "MSA-2024-001"
    conflicts = await harness.conflicts()
    assert [(c.key, c.kind) for c in conflicts] == [
        (scoped(ALPHA_ID, "payment_due_days"), "supersession_candidate")
    ]

    facts = {
        fact.value["days"]: fact for fact in await harness.facts("payment_due_days")
    }
    # The superseded term and the one that replaced it are both from MSA-2024-001.
    superseded = next(c for c in conflicts)
    assert facts[30].id == superseded.fact_a_id
    assert facts[60].id == superseded.fact_b_id
    # The June agreement was never in the comparison.
    assert facts[45].scope.agreement_id == "MSA-2024-002"

    document = await harness.repository.get_document(UUID(report["document_id"]))
    assert harness.repository.supersedes[document.id] == facts[30].document_id


async def test_an_invoices_terms_are_operational_and_open_no_conflict(harness) -> None:
    await harness.run(ALPHA)
    report = await harness.run(INVOICE)

    assert report["agreement_id"] == "MSA-2024-001"
    assert {relation["kind"] for relation in report["relations"]} == {
        "governed_by",
        "references",
    }
    assert await harness.conflicts() == []

    register = await harness.register()
    # The contract term stands, untouched and unversioned. The invoice's own amount is
    # an operational key with no contractual counterpart, so it lands normally.
    assert register[scoped(ALPHA_ID, "payment_due_days")].value == {
        "days": 30,
        "anchor": "receipt",
    }
    assert register[scoped(ALPHA_ID, "payment_due_days")].state == "supported"
    assert register[scoped(ALPHA_ID, "payment_due_days")].version == 1
    assert register[scoped(ALPHA_ID, "invoice_amount_due")].value == {
        "amount": "$9,750.00",
        "currency": "USD",
    }

    scopes = {
        fact.value["days"]: fact.scope.obligation_scope
        for fact in await harness.facts("payment_due_days")
    }
    assert scopes == {30: "contractual", 10: "operational"}


async def test_an_amendment_that_names_no_agreement_escalates_to_a_human(harness) -> None:
    """The one case that must not be resolved quietly.

    Two agreements are on file and the amendment names neither. Which one it amends
    decides which term it replaces, and nothing in the text says. Picking the most recent
    would rewrite a term in an agreement the document may never have been about.
    """
    await harness.run(ALPHA)
    await harness.run(BETA)
    report = await harness.run(AMBIGUOUS, decide=lambda item: "rejected")

    assert report["agreement_id"] is None
    # One gate: this collection has no playbook, so no rule was evaluated and there is no
    # result for anyone to confirm. The scope question is the whole of the review.
    assert harness.gates == ["item_level_review"]
    item = next(item for item in harness.last_items if item.kind == "scope_question")
    assert item.target_key == scoped("", "payment_due_days")
    assert item.payload["conflict"]["kind"] == "ambiguous_scope"
    rationale = item.payload["conflict"]["rationale"]
    assert ALPHA_ID in rationale and BETA_ID in rationale
    assert "names no agreement" in rationale
    # A question, not a proposal: there is no value attached to approve by accident.
    assert "after" not in item.payload

    # Neither agreement moved, and the 90-day term became nobody's value -- not Alpha's,
    # not Beta's, and not a third row of its own.
    register = await harness.register()
    payment_rows = {
        key: item for key, item in register.items() if key.endswith("::payment_due_days")
    }
    assert sorted(payment_rows) == [
        scoped(ALPHA_ID, "payment_due_days"),
        scoped(BETA_ID, "payment_due_days"),
    ]
    assert {item.value["days"] for item in payment_rows.values()} == {30, 45}
    assert [c.kind for c in await harness.conflicts()] == ["ambiguous_scope"]


def test_a_document_that_denies_amending_is_not_read_as_an_amendment() -> None:
    """The word "amendment" is not a claim to amend anything.

    A reference header cites one; an invoice saying it "is not signed as an amendment"
    denies the relation in as many words. Both used to register as `amends`, which made
    the invoice's billing terms contractual and put NET 10 in front of a human as a rival
    to the negotiated term -- the exact false conflict this module exists to prevent.
    """
    from doctask.domain import Block
    from doctask.services.hashing import sha256_text

    def relations(text: str) -> set[str]:
        block = Block(
            document_id=uuid4(),
            index=0,
            text=text,
            text_sha256=sha256_text(text),
            char_start=0,
            char_end=len(text),
        )
        return {relation.kind for relation in detect_relations([block])}

    assert "amends" not in relations("MSA-2026-014 / Amendment No. 1")
    assert "amends" not in relations(
        "Contractual note: This invoice is an operational billing document and is not "
        "signed as an amendment to the MSA."
    )
    # A real claim still lands, whether it is stated with the verb or with the noun
    # beside supersession language.
    assert "amends" in relations("This Amendment amends Agreement No. MSA-2024-001.")
    assert "amends" in relations("This Amendment replaces the payment provision.")


def test_an_agreement_identifier_needs_a_label_that_is_a_whole_word() -> None:
    """`no\\.?` matched the "no" inside "notices", so "Contract notices" read as
    agreement "TICES" and named every register row in the collection after it. A wrong
    identifier is worse than none: it is a real agreement scope to everything downstream.
    """
    assert agreement_ref("Contract notices\nlegal@example.com") is None
    assert agreement_ref("15. Entire Agreement\n\nNORTHSTAR RETAIL") is None
    assert agreement_ref("Agreement No. MSA-2024-001") == "MSA-2024-001"
    assert agreement_ref("Contract Reference: ACME/2024/17") == "ACME/2024/17"

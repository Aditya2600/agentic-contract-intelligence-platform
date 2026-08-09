from uuid import uuid4

from doctask.domain import FactScope, StoredFact
from doctask.services.derivation import derive_key
from doctask.services.supersession import supersession_evidence, supersession_evidence_near


def _fact(
    value,
    *,
    document_id,
    doc_type="master_agreement",
    supersedes_id=None,
    quote="q",
    scope=None,
):
    return StoredFact(
        id=uuid4(),
        key="payment_due_days",
        value=value,
        fingerprint=f"fp-{value['days']}-{doc_type}",
        quote=quote,
        document_id=document_id,
        doc_type=doc_type,
        supersedes_id=supersedes_id,
        scope=scope or FactScope(),
    )


def test_agreeing_sources_merge_citations_without_duplication() -> None:
    msa, sow = uuid4(), uuid4()
    facts = [
        _fact({"days": 30}, document_id=msa),
        _fact({"days": 30}, document_id=sow, doc_type="sow"),
    ]

    derivation = derive_key("payment_due_days", facts, new_document_id=sow)

    assert derivation.state == "supported"
    assert derivation.value == {"days": 30}
    assert derivation.citation_fact_ids == [facts[0].id, facts[1].id]
    assert derivation.citation_fingerprints == sorted(
        {facts[0].fingerprint, facts[1].fingerprint}
    )
    assert derivation.conflict is None

    # The same sentence quoted twice is one citation, not two.
    repeated = facts + [_fact({"days": 30}, document_id=msa)]
    assert derive_key("payment_due_days", repeated).citation_fingerprints == (
        derivation.citation_fingerprints
    )


def test_explicit_amendment_language_creates_a_supersession_proposal() -> None:
    msa, amendment = uuid4(), uuid4()
    facts = [
        _fact({"days": 30}, document_id=msa),
        _fact({"days": 45}, document_id=amendment, doc_type="amendment", supersedes_id=msa),
    ]

    derivation = derive_key(
        "payment_due_days",
        facts,
        new_document_id=amendment,
        supersession_evidence="This amendment replaces the payment provision.",
    )

    assert derivation.value == {"days": 45}
    assert derivation.state == "supported"
    assert derivation.conflict is not None
    assert derivation.conflict.kind == "supersession_candidate"
    assert derivation.conflict.fact_a_id == facts[0].id
    assert derivation.conflict.fact_b_id == facts[1].id
    # Only the superseding evidence grounds the new value.
    assert derivation.citation_fact_ids == [facts[1].id]


def test_contradiction_keeps_the_incumbent_value_and_opens_a_conflict() -> None:
    msa, invoice = uuid4(), uuid4()
    facts = [
        _fact({"days": 30}, document_id=msa),
        _fact({"days": 10}, document_id=invoice, doc_type="invoice"),
    ]

    derivation = derive_key("payment_due_days", facts, new_document_id=invoice)

    assert derivation.value == {"days": 30}  # the invoice never overrides the contract
    assert derivation.state == "disputed"
    assert derivation.conflict is not None
    assert derivation.conflict.kind == "contradiction"
    assert set(derivation.citation_fact_ids) == {facts[0].id, facts[1].id}


def test_supersession_needs_a_link_to_the_document_it_replaces() -> None:
    msa, amendment = uuid4(), uuid4()
    facts = [
        _fact({"days": 30}, document_id=msa),
        # Amendment language, but pointing at some other agreement.
        _fact({"days": 45}, document_id=amendment, doc_type="amendment", supersedes_id=uuid4()),
    ]

    derivation = derive_key(
        "payment_due_days",
        facts,
        new_document_id=amendment,
        supersession_evidence="This amendment replaces the payment provision.",
    )

    assert derivation.conflict is not None
    assert derivation.conflict.kind == "contradiction"
    assert derivation.value == {"days": 30}


# ------------------------------------------------------- scope decides what can conflict


def test_an_operational_term_is_evidence_beside_the_contract_not_against_it() -> None:
    """An invoice billing NET 10 under a NET 30 contract is a billing problem.

    Treated as a rival answer to the same question it marked the contract term
    `disputed` and opened a conflict on every invoice a vendor ever sent.
    """
    msa, invoice = uuid4(), uuid4()
    contract = _fact({"days": 30}, document_id=msa, scope=FactScope(agreement_id="A"))
    billed = _fact(
        {"days": 10},
        document_id=invoice,
        doc_type="invoice",
        scope=FactScope(agreement_id="A", obligation_scope="operational"),
    )

    derivation = derive_key("payment_due_days", [contract, billed], new_document_id=invoice)

    assert derivation.value == {"days": 30}
    assert derivation.state == "supported"
    assert derivation.conflict is None
    # Excluded, not lost: the fact stays stored and the exclusion is stated.
    assert derivation.citation_fact_ids == [contract.id]
    assert "out of scope" in derivation.reason
    assert "operational" in derivation.reason


def test_terms_from_two_different_agreements_are_not_in_conflict() -> None:
    alpha, beta = uuid4(), uuid4()
    facts = [
        _fact({"days": 30}, document_id=alpha, scope=FactScope(agreement_id="MSA-1")),
        _fact({"days": 45}, document_id=beta, scope=FactScope(agreement_id="MSA-2")),
    ]

    derivation = derive_key("payment_due_days", facts, new_document_id=beta)

    assert derivation.conflict is None
    assert derivation.state == "supported"
    assert "MSA-1" in derivation.reason


def test_a_conditional_term_is_a_different_promise_from_an_unconditional_one() -> None:
    """Absent conditions means unconditional, not unknown, so these do not compare."""
    msa = uuid4()
    facts = [
        _fact({"days": 30}, document_id=msa, scope=FactScope(agreement_id="A")),
        _fact(
            {"days": 10},
            document_id=msa,
            scope=FactScope(agreement_id="A", conditions=("if paid by ach",)),
        ),
    ]

    assert derive_key("payment_due_days", facts, new_document_id=msa).conflict is None


def test_an_amendment_that_names_no_agreement_among_several_is_escalated() -> None:
    """The caller partitions by agreement, so an unnamed amendment arrives in the unnamed
    bucket, told which agreements it could have meant. Supersession language is present
    and the link resolved -- but which agreement it amends is a guess, so no value is
    derived at all, in either agreement's row or a third one of its own."""
    amendment = uuid4()
    facts = [
        _fact({"days": 90}, document_id=amendment, doc_type="amendment", supersedes_id=uuid4()),
    ]

    derivation = derive_key(
        "payment_due_days",
        facts,
        new_document_id=amendment,
        supersession_evidence="This Amendment replaces the payment provision.",
        ambiguous_among=("MSA-1", "MSA-2"),
    )

    assert derivation.conflict is not None
    assert derivation.conflict.kind == "ambiguous_scope"
    assert derivation.state == "ambiguous"
    assert derivation.value is None
    assert derivation.agreement_id == ""
    assert "MSA-1" in derivation.conflict.rationale
    assert "MSA-2" in derivation.conflict.rationale


def test_one_named_agreement_is_not_ambiguous() -> None:
    """The escalation is about a choice between agreements, not about naming none.

    A collection holding one agreement resolves an unnamed document to it, so nothing
    reaches here ambiguous, and a single-agreement collection behaves as it always did.
    """
    msa, amendment = uuid4(), uuid4()
    facts = [
        _fact({"days": 30}, document_id=msa, scope=FactScope(agreement_id="MSA-1")),
        _fact({"days": 45}, document_id=amendment, doc_type="amendment", supersedes_id=msa),
    ]

    derivation = derive_key(
        "payment_due_days",
        facts,
        new_document_id=amendment,
        supersession_evidence="This Amendment replaces the payment provision.",
        agreement_id="MSA-1",
        ambiguous_among=("MSA-1",),
    )

    assert derivation.conflict.kind == "supersession_candidate"
    assert derivation.value == {"days": 45}
    assert derivation.agreement_id == "MSA-1"


def test_an_operational_term_needs_no_agreement_to_be_committed() -> None:
    """Only a contractual value is withheld when the agreement is unresolved.

    An invoice total is the invoice's own fact. Blocking it on which contract the invoice
    belongs to would hold up a number that no contract ever states.
    """
    invoice = uuid4()
    fact = _fact(
        {"days": 10},
        document_id=invoice,
        doc_type="invoice",
        scope=FactScope(obligation_scope="operational"),
    )

    derivation = derive_key(
        "payment_due_days",
        [fact],
        new_document_id=invoice,
        ambiguous_among=("MSA-1", "MSA-2"),
    )

    assert derivation.state == "supported"
    assert derivation.value == {"days": 10}


def test_no_evidence_is_missing_rather_than_invented() -> None:
    derivation = derive_key("liability_cap", [])
    assert derivation.value is None
    assert derivation.state == "missing"


def test_supersession_language_detection() -> None:
    text = (
        "FIRST AMENDMENT\n\nThis amendment replaces the payment provision.\n\n"
        "The termination notice period is revised to 30 days' written notice.\n"
    )
    assert supersession_evidence(text) == "This amendment replaces the payment provision."
    # The sentence carrying the quote wins over the document-level claim.
    assert supersession_evidence_near(text, "30 days' written notice") == (
        "The termination notice period is revised to 30 days' written notice."
    )
    assert supersession_evidence("Payment is due within 30 calendar days.") is None
    assert supersession_evidence_near(text, "not in this document") is None


AMENDMENT = """1. Amendment to Payment Terms

Section 4.3 of the Agreement is deleted in its entirety and replaced with the following:

"4.3 Undisputed invoice amounts are due forty-five (45) calendar days after Customer's
receipt of a correct invoice."
"""


def test_an_amendment_written_the_way_amendments_are_written_claims_supersession() -> None:
    """Real amendments say "is deleted in its entirety and replaced with the following",
    not "replaces". Matching only the present tense read this as an ordinary
    disagreement between two documents and kept the superseded term."""
    evidence = supersession_evidence(AMENDMENT)
    assert evidence is not None
    assert "replaced" in evidence


def test_a_quoted_replacement_clause_is_tied_to_the_sentence_that_introduced_it() -> None:
    """The new term is inside the quotation; the claim to replace anything is the line
    above it. A key-specific lookup that only reads the quote's own sentence finds no
    supersession language and silently downgrades the amendment to a contradiction."""
    near = supersession_evidence_near(AMENDMENT, "forty-five (45) calendar days")
    assert near is not None
    assert "deleted in its entirety" in near
    assert "forty-five (45) calendar days" in near

    # Still key-specific: a term this document does not touch gets no evidence.
    assert supersession_evidence_near(AMENDMENT, "not a quote from this document") is None


def test_a_settled_supersession_is_not_reopened_by_a_later_upload() -> None:
    """The amendment won two runs ago. An invoice does not put that back in play.

    The challenger falls back to the newest authoritative fact when the run's own
    document contributes none -- which is exactly what an invoice does. With the
    superseded term still in the comparison, that fallback re-ran the MSA-versus-
    amendment argument a human had already closed, and the register flipped back to the
    superseded number and marked itself `disputed`.
    """
    msa, amendment, invoice = uuid4(), uuid4(), uuid4()
    facts = [
        _fact({"days": 30}, document_id=msa),
        _fact({"days": 45}, document_id=amendment, doc_type="amendment", supersedes_id=msa),
        _fact(
            {"days": 10},
            document_id=invoice,
            doc_type="invoice",
            scope=FactScope(obligation_scope="operational"),
        ),
    ]

    derivation = derive_key("payment_due_days", facts, new_document_id=invoice)

    assert derivation.value == {"days": 45}
    assert derivation.state == "supported"
    assert derivation.conflict is None

    # The run that proposes the supersession still sees both sides: that argument is the
    # proposal, and it is a human's to settle.
    proposing = derive_key(
        "payment_due_days",
        facts[:2],
        new_document_id=amendment,
        supersession_evidence="This amendment replaces the payment provision.",
    )
    assert proposing.conflict.kind == "supersession_candidate"

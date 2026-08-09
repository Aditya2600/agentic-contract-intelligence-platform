-- ============================================================================
-- 12. DOCUMENT RELATIONS AND FACT SCOPE
-- ============================================================================

-- Which agreement a term belongs to decides whether two terms are in conflict.
-- Before this, every fact under one register key was an answer to one global
-- question, so two unrelated MSAs argued with each other, an invoice restating
-- its own billing terms contradicted the contract it was issued under, and an
-- amendment aimed at one clause was compared against every clause.
--
-- A relation is a claim a document makes, so it is stored the way a fact is:
-- with the sentence that claims it and the block that sentence came from. An
-- unresolved target is kept, not dropped -- an amendment to an agreement nobody
-- uploaded is precisely the case a human has to be shown.
CREATE TABLE document_relations (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id        UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    kind               TEXT NOT NULL,   -- amends|governed_by|supersedes|references
    -- The agreement as this document names it, e.g. 'MSA-2024-001'. Empty when the
    -- document claims a relation without naming what it relates to, which is the
    -- ambiguity the conflict gate exists to escalate.
    target_ref         TEXT NOT NULL DEFAULT '',
    target_document_id UUID REFERENCES documents(id) ON DELETE SET NULL,
    evidence_quote     TEXT NOT NULL,
    block_id           UUID REFERENCES document_blocks(id) ON DELETE CASCADE,
    quote_start        INT  NOT NULL DEFAULT 0,
    quote_end          INT  NOT NULL DEFAULT 0,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),

    CHECK (kind IN ('amends', 'governed_by', 'supersedes', 'references')),
    CHECK (length(evidence_quote) > 0),
    -- Replay-safe: the same claim re-read from the same block is one relation.
    UNIQUE (document_id, kind, target_ref, block_id, quote_start)
);

CREATE INDEX document_relations_target_idx
    ON document_relations (target_document_id, kind);

-- The agreement a document *is*, when it declares one ('Agreement No. MSA-2024-001').
-- Only agreements carry this: an invoice quoting that number is naming someone
-- else's agreement, not claiming to be it.
ALTER TABLE documents ADD COLUMN agreement_ref TEXT;

CREATE INDEX documents_agreement_ref_idx
    ON documents (collection_id, agreement_ref) WHERE agreement_ref IS NOT NULL;

-- What a fact is a fact *about*: agreement_id, clause, parties, effective_from,
-- effective_to, conditions, obligation_scope. One JSONB column rather than seven
-- typed ones because nothing here is queried relationally -- comparability is
-- decided in `FactScope.comparable_to`, and a scope read back must equal the scope
-- written or the comparison is meaningless.
ALTER TABLE facts ADD COLUMN scope JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE INDEX facts_agreement_idx ON facts ((scope ->> 'agreement_id'));

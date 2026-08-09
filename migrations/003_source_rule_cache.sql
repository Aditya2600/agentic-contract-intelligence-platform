-- ============================================================================
-- 11. SOURCE-RULE EVALUATION CACHE
-- ============================================================================

-- Re-uploading a document used to short-circuit before the source stage, so a
-- playbook edited between the two uploads was never applied to the bytes it was
-- edited for. The stage now always runs; this table is what keeps the second
-- upload from paying for it twice.
--
-- The key is the whole of what a source verdict depends on: the document bytes,
-- the exact playbook, and the evaluator that judged them. Anything else -- a new
-- ruleset, a bumped evaluator -- is a miss, and a miss re-evaluates. A row here
-- is never a verdict, only a pointer to the run whose explicit verdicts stand.
CREATE TABLE source_rule_cache (
    cache_key         TEXT PRIMARY KEY,
    collection_id     UUID NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    document_sha256   TEXT NOT NULL,
    ruleset_hash      TEXT NOT NULL,   -- '' when the collection has no playbook
    evaluator_version TEXT NOT NULL,
    run_id            UUID NOT NULL,   -- the run holding the findings to reuse
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX source_rule_cache_document_idx
    ON source_rule_cache (collection_id, document_sha256);

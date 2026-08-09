-- Immutable evidence spans on facts.
--
-- `quote`, `quote_start` and `quote_end` locate a fact inside a block row. That is enough
-- to re-read it while the database stands, and not enough to prove anything about it
-- afterwards: block ids are assigned per insert, so a reimport of the same bytes produces
-- the same text under different ids and nothing can be checked across the two.
--
-- The span is the same location in coordinates that belong to the bytes: which document
-- (by content hash), which block of it (by position), which characters, what those
-- characters hash to, and which parser produced them. `facts.fact_fingerprint` is a hash
-- over exactly this plus the key and value, which is what register content hashes are
-- built from -- never `facts.id`.

ALTER TABLE facts
    ADD COLUMN IF NOT EXISTS evidence JSONB NOT NULL DEFAULT '{}'::jsonb;

-- The same claim read from the same characters of the same document by the same parser is
-- one fact, whichever run found it. Partial: rows predating this column carry '{}'.
CREATE INDEX IF NOT EXISTS facts_evidence_quote_idx
    ON facts ((evidence ->> 'quote_sha256'))
 WHERE evidence ? 'quote_sha256';

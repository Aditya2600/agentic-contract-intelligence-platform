-- Agreement-scoped register keys.
--
-- register_items was UNIQUE (collection_id, key): one `payment_due_days` for the whole
-- collection. Two agreements in one collection therefore shared a row, and the second
-- one's term overwrote the first's -- silently, because facts in different agreements are
-- correctly not a conflict, so nothing was escalated and nothing was reported. The
-- agreement is part of a register row's identity, not metadata hanging off it.
--
-- '' is the unnamed bucket: a collection that names no agreement at all keeps exactly the
-- behaviour it had. It is NOT NULL so the unique constraint actually holds -- NULLs are
-- distinct from each other in a Postgres unique index, which would let the same unnamed
-- key be inserted twice.

ALTER TABLE register_items
    ADD COLUMN IF NOT EXISTS agreement_id TEXT NOT NULL DEFAULT '';

ALTER TABLE register_items DROP CONSTRAINT IF EXISTS register_items_collection_id_key_key;

ALTER TABLE register_items
    ADD CONSTRAINT register_items_collection_agreement_key_key
        UNIQUE (collection_id, agreement_id, key);

-- How the register is read back: every row of one agreement, in obligation order.
CREATE INDEX IF NOT EXISTS register_items_agreement_idx
    ON register_items (collection_id, agreement_id, key);

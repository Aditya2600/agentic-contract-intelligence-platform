-- A human decision is a record beside the system's verdict, never a rewrite of it.
--
-- `findings.state` was one mutable field doing two jobs: it started at 'proposed' and the
-- commit set it to 'approved'. A dismissal had nowhere to go, so a reviewer who judged a
-- violation inapplicable left no trace at all -- the finding stayed 'proposed' forever,
-- indistinguishable from one nobody had looked at, and `verdict` was the only thing on the
-- row that still said anything. Neither state told you who decided, or when.
--
-- `verdict` is now explicitly the immutable system verdict. `review_decision` is what a
-- human did about it, with the identity and the timestamp that make it an audit record
-- rather than a flag.

ALTER TABLE findings RENAME COLUMN state TO review_decision;

ALTER TABLE findings ALTER COLUMN review_decision SET DEFAULT 'pending';

UPDATE findings SET review_decision = CASE review_decision
    WHEN 'approved' THEN 'upheld'
    WHEN 'rejected' THEN 'dismissed'
    ELSE 'pending'
END;

ALTER TABLE findings
    ADD CONSTRAINT findings_review_decision_check
        CHECK (review_decision IN ('pending', 'upheld', 'dismissed'));

ALTER TABLE findings ADD COLUMN IF NOT EXISTS decided_by TEXT;
ALTER TABLE findings ADD COLUMN IF NOT EXISTS decided_at TIMESTAMPTZ;

-- Dismissing an adverse verdict is a judgement about one run's evidence. Reusing it
-- forever would quietly promote it to policy, so the finding is flagged for re-evaluation
-- and the source-rule cache entry that would have served it back is dropped.
ALTER TABLE findings
    ADD COLUMN IF NOT EXISTS recheck_required BOOLEAN NOT NULL DEFAULT false;

CREATE INDEX IF NOT EXISTS findings_recheck_idx
    ON findings (run_id) WHERE recheck_required;

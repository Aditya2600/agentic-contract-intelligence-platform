-- Exactly-once, made checkable rather than assumed.
--
-- Every node in this graph was already written to be replay-safe: fingerprints, unique
-- keys, ON CONFLICT DO NOTHING. That is a property of each write, and it is not the same
-- as a record of what ran. After a SIGKILL there was no way to ask "did this stage
-- complete, and with what result" -- only to re-run it and hope the idempotency held.
--
-- The ledger is that record. One row per (run, stage, input), carrying the hash of what
-- the stage was given and the hash of what it produced. For the commit it is written
-- inside the same transaction as the register writes, which is what makes "this run
-- already committed this exact basis" a fact rather than an inference.

CREATE TABLE IF NOT EXISTS run_stage_ledger (
    run_id      UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    stage       TEXT NOT NULL,

    -- What the stage was asked to do. A replay with the same input is the same work; a
    -- replay with a different input is different work under the same stage name, and the
    -- two must not be confused -- `retry_extract` re-enters `extract_facts` on purpose.
    input_hash  TEXT NOT NULL,

    -- What it produced. Comparing this across a replay is how "the same stage ran twice
    -- and agreed" is told apart from "it ran twice and disagreed", which is the failure
    -- no amount of ON CONFLICT protects against.
    output_hash TEXT,
    status      TEXT NOT NULL DEFAULT 'completed',
    attempts    INT  NOT NULL DEFAULT 1,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (run_id, stage, input_hash),
    CHECK (status IN ('started', 'completed'))
);

CREATE INDEX IF NOT EXISTS run_stage_ledger_run_idx ON run_stage_ledger (run_id, stage);


-- A lease, so two resume requests cannot drive one LangGraph thread at once.
--
-- `thread_id = run_id` makes resume addressable by anyone who knows the run. Two callers
-- resuming the same run concurrently -- a retrying HTTP client, a watcher and a human,
-- two replicas -- both read the same checkpoint and both execute the same nodes. The
-- writes are idempotent, so the register survives it; the human gates do not, because
-- two processes can each interrupt and each be answered.
--
-- The lease is compare-and-set: acquiring is an UPDATE whose WHERE clause is the
-- precondition, so exactly one caller sees a row back. It expires, because a process that
-- is SIGKILLed holding a lease must not lock its own run out forever -- the whole point
-- of the ledger is that resuming after a crash is safe.
ALTER TABLE runs ADD COLUMN IF NOT EXISTS lease_owner      TEXT;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS runs_lease_idx ON runs (id) WHERE lease_owner IS NOT NULL;

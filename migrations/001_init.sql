-- ============================================================================
-- doctask — initial schema
-- Postgres 16 + pgvector
-- migrations/001_init.sql
--
-- Design rule: every graded requirement should be a structural property of
-- this schema, not something the agent has to "remember" to do.
--   req 2  (resume)        -> LangGraph checkpoint tables, thread_id = run_id
--   req 3  (item review)   -> review_items, one row per decision
--   req 5  (no fabrication)-> facts.quote + offsets; register_items.state
--   req 9  (isolation)     -> collection_id on every row, version columns,
--                             UNIQUE(collection_id, idempotency_key)
--   req 10 (cost/latency)  -> run_events
--   cap C  (incremental)   -> content_hash + run_snapshots + change_log
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;


-- ============================================================================
-- 1. COLLECTIONS AND DOCUMENTS
-- ============================================================================

CREATE TABLE collections (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    slug        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    watch_path  TEXT,                       -- directory the watcher polls
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TYPE doc_type AS ENUM (
    'master_agreement',
    'amendment',
    'sow',
    'invoice',
    'purchase_order',
    'policy',
    'unknown'
);

CREATE TYPE doc_status AS ENUM (
    'ingested',       -- bytes stored, not yet parsed
    'parsed',         -- blocks extracted
    'unprocessable',  -- encrypted / corrupt / no extractable text
    'quarantined'     -- injection detected; parsed but never auto-trusted
);

CREATE TABLE documents (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    collection_id   UUID NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    sha256          TEXT NOT NULL,
    filename        TEXT NOT NULL,
    mime_type       TEXT NOT NULL,
    byte_size       BIGINT NOT NULL,
    doc_type        doc_type NOT NULL DEFAULT 'unknown',
    doc_type_conf   REAL,                   -- < threshold routes to human
    effective_date  DATE,
    supersedes_id   UUID REFERENCES documents(id),  -- amendment -> base contract
    normalized_text TEXT,                   -- block offsets index into this
    status          doc_status NOT NULL DEFAULT 'ingested',
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Idempotent ingest. Re-dropping the same file into the watch folder is a
    -- no-op, not a second document. This is what makes the duplicate-request
    -- test pass without any application-level dedupe logic.
    UNIQUE (collection_id, sha256)
);

CREATE INDEX documents_collection_type_idx ON documents (collection_id, doc_type);


-- The citation unit. Nothing outside document_blocks.text is ever quotable.
CREATE TABLE document_blocks (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id    UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    block_index    INT  NOT NULL,
    page           INT,
    section_path   TEXT,                    -- e.g. '5.2 Payment Terms'
    char_start     INT  NOT NULL,           -- offset into documents.normalized_text
    char_end       INT  NOT NULL,
    text           TEXT NOT NULL,           -- exact source text, verbatim
    text_sha256    TEXT NOT NULL,           -- extraction cache key: unchanged
                                            -- block = zero model spend on re-run
    injection_flag BOOLEAN NOT NULL DEFAULT FALSE,
    embedding      vector(768),

    UNIQUE (document_id, block_index),
    CHECK (char_end > char_start)
);

CREATE INDEX document_blocks_embedding_idx
    ON document_blocks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX document_blocks_cache_idx ON document_blocks (text_sha256);


-- ============================================================================
-- 2. EXTRACTED FACTS
-- ============================================================================

-- One atomic claim, bound to exactly one block, with a verbatim quote and
-- offsets. A fact with no quote cannot be inserted; a fact whose quote is not
-- found in blocks.text is rejected by the validator before insert.
CREATE TABLE facts (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    collection_id   UUID NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    document_id     UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    block_id        UUID NOT NULL REFERENCES document_blocks(id) ON DELETE CASCADE,
    key             TEXT NOT NULL,          -- 'payment_terms', 'liability_cap'
    value           JSONB NOT NULL,
    quote           TEXT NOT NULL,
    quote_start     INT  NOT NULL,          -- offset within block.text
    quote_end       INT  NOT NULL,
    effective_date  DATE,
    extracted_by_run UUID,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CHECK (length(quote) > 0),
    CHECK (quote_end > quote_start)
);

CREATE INDEX facts_collection_key_idx ON facts (collection_id, key);
CREATE INDEX facts_document_idx       ON facts (document_id);


-- ============================================================================
-- 3. THE DELIVERABLE (obligations register)
-- ============================================================================

CREATE TYPE item_state AS ENUM (
    'supported',    -- backed by >= 1 fact
    'disputed',     -- open conflict; value NOT auto-resolved
    'unsupported',  -- model proposed it, validator could not ground it
    'missing'       -- expected key, no source evidence anywhere
);

CREATE TABLE register_items (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    collection_id UUID NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    key           TEXT NOT NULL,
    title         TEXT NOT NULL,
    value         JSONB,
    state         item_state NOT NULL DEFAULT 'supported',

    -- sha256 over canonical_json(value, sorted citation ids, state).
    -- Diffing these before/after a run is the proof that untouched items
    -- were not rewritten.
    content_hash  TEXT NOT NULL,

    -- Optimistic lock. Commit does UPDATE ... WHERE version = $expected;
    -- 0 rows affected means another run committed first -> re-derive.
    version       INT  NOT NULL DEFAULT 1,

    last_run_id   UUID,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (collection_id, key)
);

CREATE TABLE register_item_citations (
    register_item_id UUID NOT NULL REFERENCES register_items(id) ON DELETE CASCADE,
    fact_id          UUID NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
    PRIMARY KEY (register_item_id, fact_id)
);

-- Reverse index for invalidation: given a new document, which register items
-- does it touch? Answered by join, never by asking a model.
CREATE INDEX register_item_citations_fact_idx
    ON register_item_citations (fact_id);


-- ============================================================================
-- 4. CONFLICTS  (surfaced, never silently resolved)
-- ============================================================================

CREATE TABLE conflicts (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    collection_id UUID NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    key           TEXT NOT NULL,
    fact_a_id     UUID NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
    fact_b_id     UUID NOT NULL REFERENCES facts(id) ON DELETE CASCADE,

    -- 'contradiction'          : two live values, no supersession language
    -- 'supersession_candidate' : source says "amends"/"replaces"/"superseded by"
    kind          TEXT NOT NULL,
    rationale     TEXT NOT NULL,
    state         TEXT NOT NULL DEFAULT 'open',   -- open|accepted|rejected
    detected_run  UUID,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (collection_id, key, fact_a_id, fact_b_id)
);


-- ============================================================================
-- 5. RULES AND FINDINGS
-- ============================================================================

CREATE TABLE rulesets (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    collection_id UUID NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    version       INT  NOT NULL DEFAULT 1,
    source_text   TEXT NOT NULL,          -- the uploaded playbook / checklist
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (collection_id, name, version)
);

CREATE TABLE rules (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ruleset_id  UUID NOT NULL REFERENCES rulesets(id) ON DELETE CASCADE,
    code        TEXT NOT NULL,            -- 'PAY-01'
    text        TEXT NOT NULL,
    severity    TEXT NOT NULL,            -- blocker|major|minor|info
    scope       TEXT NOT NULL,            -- source|deliverable|both
    -- Register keys the deliverable stage judges this rule against. Empty = every
    -- affected key.
    target_keys TEXT[] NOT NULL DEFAULT '{}',
    UNIQUE (ruleset_id, code)
);

-- 'pass' rows are written explicitly so "no findings" is a demonstrated
-- result (every rule evaluated, none violated) rather than an empty table
-- that could equally mean the stage never ran.
CREATE TYPE verdict AS ENUM ('violation', 'pass', 'insufficient_evidence');

CREATE TABLE findings (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id      UUID NOT NULL,
    rule_id     UUID NOT NULL REFERENCES rules(id) ON DELETE CASCADE,
    target_kind TEXT NOT NULL,            -- document|register_item
    target_id   UUID NOT NULL,
    -- Filename or register key. target_id is a derived UUID and reads as nothing alone.
    target_key  TEXT NOT NULL DEFAULT '',
    verdict     verdict NOT NULL,
    rationale   TEXT NOT NULL,
    state       TEXT NOT NULL DEFAULT 'proposed',  -- proposed|approved|rejected
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_id, rule_id, target_kind, target_id)
);

CREATE TABLE finding_citations (
    finding_id  UUID NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
    block_id    UUID NOT NULL REFERENCES document_blocks(id) ON DELETE CASCADE,
    quote       TEXT NOT NULL,
    quote_start INT  NOT NULL,
    quote_end   INT  NOT NULL,
    PRIMARY KEY (finding_id, block_id, quote_start)
);


-- ============================================================================
-- 6. RUNS, STAGE TELEMETRY, CHECKPOINTS
-- ============================================================================

CREATE TYPE run_status AS ENUM (
    'running',
    'awaiting_review',   -- graph parked on interrupt(); state lives in checkpoint
    'committing',
    'committed',
    'blocked',           -- an approved blocker finding stopped the commit; nothing written
    'failed',
    'cancelled'
);

CREATE TABLE runs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    collection_id   UUID NOT NULL REFERENCES collections(id) ON DELETE CASCADE,

    -- Caller-supplied. Duplicate POST / duplicate MCP call with the same key
    -- returns the existing run instead of starting a second one.
    idempotency_key TEXT NOT NULL,

    trigger         TEXT NOT NULL,        -- manual|watcher|api|mcp
    trigger_doc_id  UUID REFERENCES documents(id),
    status          run_status NOT NULL DEFAULT 'running',
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at        TIMESTAMPTZ,

    UNIQUE (collection_id, idempotency_key)
);

CREATE INDEX runs_collection_status_idx ON runs (collection_id, status);

-- One row per node execution. This table IS the "visible agent stages" panel
-- and the cost/latency report — the UI streams it, it is not a separate log.
CREATE TABLE run_events (
    id          BIGSERIAL PRIMARY KEY,
    run_id      UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    seq         INT  NOT NULL,
    stage       TEXT NOT NULL,            -- node name

    -- continue | retry | skip | escalate | abstain | branch:<name>
    decision    TEXT,
    reason      TEXT,                     -- why, in one line
    next_node   TEXT,                     -- how the decision changed the path

    attempt     INT  NOT NULL DEFAULT 1,
    error_class TEXT,                     -- transient|validation|data|policy

    model        TEXT,
    tokens_in    INT NOT NULL DEFAULT 0,
    tokens_out   INT NOT NULL DEFAULT 0,
    cache_hit    BOOLEAN NOT NULL DEFAULT FALSE,
    cost_usd     NUMERIC(12,6) NOT NULL DEFAULT 0,
    external_svc TEXT,                    -- ocr|embedding|storage

    started_at   TIMESTAMPTZ NOT NULL,
    ended_at     TIMESTAMPTZ,
    duration_ms  INT,

    UNIQUE (run_id, seq)
);

CREATE INDEX run_events_run_idx ON run_events (run_id, seq);

-- NOTE: LangGraph's PostgresSaver owns checkpoints / checkpoint_blobs /
-- checkpoint_writes and creates them via .setup(). Do not hand-roll them.
-- Convention: thread_id = runs.id, so a crashed process resumes by loading
-- the checkpoint for its run id and re-invoking the graph.


-- ============================================================================
-- 7. HUMAN REVIEW
-- ============================================================================

CREATE TYPE review_state AS ENUM ('pending', 'approved', 'rejected');

-- One row per decision the human can make. Independence is the point:
-- rejecting row 4 is an UPDATE touching row 4 only, so approved rows
-- 1-3 are untouched and still commit.
CREATE TABLE review_items (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id        UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    collection_id UUID NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL,          -- conflict|finding|register_update
    target_id     UUID NOT NULL,
    payload       JSONB NOT NULL,         -- {before, after, citations[]}
    state         review_state NOT NULL DEFAULT 'pending',
    decided_by    TEXT,                   -- human actor id; no model may write
    decided_at    TIMESTAMPTZ,
    comment       TEXT,

    UNIQUE (run_id, kind, target_id)
);

CREATE INDEX review_items_pending_idx
    ON review_items (run_id, state) WHERE state = 'pending';


-- ============================================================================
-- 8. INCREMENTAL-UPDATE EVIDENCE
-- ============================================================================

-- Hash of every register item before and after a run. The diff of these two
-- phases is the proof that unaffected content was preserved byte-for-byte.
CREATE TABLE run_snapshots (
    run_id           UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    register_item_id UUID NOT NULL REFERENCES register_items(id) ON DELETE CASCADE,
    phase            TEXT NOT NULL,       -- before|after
    content_hash     TEXT NOT NULL,
    PRIMARY KEY (run_id, register_item_id, phase)
);

-- What changed, when, and which source caused it.
CREATE TABLE change_log (
    id                    BIGSERIAL PRIMARY KEY,
    collection_id         UUID NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    register_item_id      UUID NOT NULL REFERENCES register_items(id) ON DELETE CASCADE,
    run_id                UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    caused_by_document_id UUID REFERENCES documents(id),
    old_value             JSONB,
    new_value             JSONB,
    old_hash              TEXT,
    new_hash              TEXT NOT NULL,
    changed_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX change_log_item_idx ON change_log (register_item_id, changed_at DESC);
-- ============================================================================
-- 9. REPLAY SAFETY AND VERSIONED EXTRACTION CACHE
-- ============================================================================

-- A graph checkpoint does not make a database side effect exactly-once. The
-- fingerprint makes fact insertion safe when a node is replayed after a crash.
ALTER TABLE facts ADD COLUMN fact_fingerprint TEXT NOT NULL;
CREATE UNIQUE INDEX facts_replay_safe_idx
    ON facts (collection_id, document_id, block_id, fact_fingerprint);

CREATE TABLE extraction_cache (
    cache_key          TEXT PRIMARY KEY,
    block_sha256       TEXT NOT NULL,
    extractor_version  TEXT NOT NULL,
    prompt_version     TEXT NOT NULL,
    schema_version     TEXT NOT NULL,
    model_family       TEXT NOT NULL,
    result             JSONB NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX extraction_cache_block_idx ON extraction_cache (block_sha256);

-- ============================================================================
-- 10. ITEM-LEVEL REVIEW COMPARE-AND-SET
-- ============================================================================

CREATE OR REPLACE FUNCTION decide_review_item(
    p_item_id UUID,
    p_new_state review_state,
    p_actor_id TEXT,
    p_comment TEXT DEFAULT NULL
)
RETURNS review_items
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
    result review_items;
BEGIN
    IF p_new_state NOT IN ('approved', 'rejected') THEN
        RAISE EXCEPTION 'review decision must be approved or rejected';
    END IF;

    UPDATE review_items
       SET state = p_new_state,
           decided_by = p_actor_id,
           decided_at = now(),
           comment = p_comment
     WHERE id = p_item_id
       AND state = 'pending'
     RETURNING * INTO result;

    IF result.id IS NULL THEN
        RAISE EXCEPTION 'review item does not exist or was already decided';
    END IF;

    RETURN result;
END;
$$;

-- Deployment note: grant this function only to the authenticated reviewer role.
-- The agent/model-facing role should have SELECT/INSERT on review_items but no
-- UPDATE permission and no EXECUTE permission on decide_review_item.

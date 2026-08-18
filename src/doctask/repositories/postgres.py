from __future__ import annotations

from datetime import timedelta
from uuid import NAMESPACE_OID, UUID, uuid5

from psycopg import errors
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from doctask.domain import (
    REGISTER_KEY_SEPARATOR,
    Block,
    CollectionSummary,
    CommitResult,
    Conflict,
    Document,
    DocumentRelation,
    DocumentSummary,
    EvidenceSpan,
    FactCandidate,
    FactScope,
    Finding,
    FindingCitation,
    RegisterChange,
    RegisterItem,
    RegisterKey,
    ReviewItem,
    Rule,
    Ruleset,
    RunEvent,
    RunSummary,
    StageRecord,
    StoredFact,
    WatchedCollection,
)
from doctask.repositories.base import (
    CollectionConflictError,
    ItemAlreadyDecidedError,
    stale_proposal,
)
from doctask.services.hashing import register_content_hash
from doctask.services.ids import slugify


def review_target_id(kind: str, target_key: str) -> UUID:
    """review_items.target_id is a UUID, register keys are text.

    A deterministic uuid5 keeps UNIQUE(run_id, kind, target_id) meaning
    "one decision per key per run", which is what makes re-assembly replay-safe.
    """
    return uuid5(NAMESPACE_OID, f"{kind}:{target_key}")


class PostgresRepository:
    """Production repository boundary.

    Every method is one short explicit transaction. The migration already owns the
    constraints; this class only has to speak to them honestly.
    """

    def __init__(self, database_url: str) -> None:
        self.pool = AsyncConnectionPool(
            database_url, open=False, kwargs={"row_factory": dict_row}
        )

    async def open(self) -> None:
        await self.pool.open()

    async def close(self) -> None:
        await self.pool.close()

    # ------------------------------------------------------------------ collections

    async def create_collection(self, name: str, *, slug: str | None = None) -> UUID:
        slug = slug or slugify(name)
        if not slug:
            raise ValueError("collection needs a name or an explicit slug")
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """
                INSERT INTO collections(slug, name) VALUES (%s, %s)
                ON CONFLICT (slug) DO NOTHING
                RETURNING id
                """,
                (slug, name),
            )
            row = await cur.fetchone()
            if row is not None:
                return row["id"]
            cur = await conn.execute(
                "SELECT id, name FROM collections WHERE slug = %s", (slug,)
            )
            existing = await cur.fetchone()
            assert existing is not None
            if existing["name"] != name:
                raise CollectionConflictError(
                    f"collection slug {slug!r} already names {existing['name']!r}"
                )
            return existing["id"]

    async def set_collection_watch_path(
        self, collection_id: UUID, watch_path: str | None
    ) -> None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "UPDATE collections SET watch_path = %s WHERE id = %s RETURNING id",
                (watch_path or None, collection_id),
            )
            if await cur.fetchone() is None:
                raise KeyError(f"collection {collection_id} does not exist")

    async def list_watched_collections(self) -> list[WatchedCollection]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT id, name, watch_path FROM collections
                 WHERE watch_path IS NOT NULL AND watch_path <> ''
                 ORDER BY slug
                """
            )
            rows = await cur.fetchall()
        return [
            WatchedCollection(id=row["id"], name=row["name"], watch_path=row["watch_path"])
            for row in rows
        ]

    _COLLECTION_SUMMARY_QUERY = """
        SELECT
            c.id, c.name, c.slug, c.watch_path,
            COUNT(DISTINCT d.id) AS document_count,
            COUNT(DISTINCT ri.id) AS register_row_count,
            COUNT(DISTINCT r.id) FILTER (
                WHERE r.status IN ('running', 'awaiting_review', 'committing', 'blocked')
            ) AS open_run_count,
            GREATEST(MAX(d.ingested_at), MAX(r.started_at), MAX(r.ended_at)) AS last_activity_at
        FROM collections c
        LEFT JOIN documents d ON d.collection_id = c.id
        LEFT JOIN register_items ri ON ri.collection_id = c.id
        LEFT JOIN runs r ON r.collection_id = c.id
        {where}
        GROUP BY c.id
        ORDER BY c.name
    """

    @staticmethod
    def _collection_summary(row: dict) -> CollectionSummary:
        return CollectionSummary(
            id=row["id"],
            name=row["name"],
            slug=row["slug"],
            document_count=row["document_count"],
            register_row_count=row["register_row_count"],
            open_run_count=row["open_run_count"],
            last_activity_at=row["last_activity_at"],
            watch_path=row["watch_path"],
        )

    async def list_collections(self) -> list[CollectionSummary]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(self._COLLECTION_SUMMARY_QUERY.format(where=""))
            rows = await cur.fetchall()
        return [self._collection_summary(row) for row in rows]

    async def get_collection(self, collection_id: UUID) -> CollectionSummary | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                self._COLLECTION_SUMMARY_QUERY.format(where="WHERE c.id = %s"),
                (collection_id,),
            )
            row = await cur.fetchone()
        return self._collection_summary(row) if row is not None else None

    async def list_documents(self, collection_id: UUID) -> list[DocumentSummary]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT
                    d.id, d.collection_id, d.filename, d.doc_type, d.sha256, d.ingested_at,
                    GREATEST(
                        COUNT(DISTINCT b.page) FILTER (WHERE b.page IS NOT NULL), 1
                    ) AS pages
                FROM documents d
                LEFT JOIN document_blocks b ON b.document_id = d.id
                WHERE d.collection_id = %s
                GROUP BY d.id
                ORDER BY d.ingested_at DESC
                """,
                (collection_id,),
            )
            rows = await cur.fetchall()
        return [
            DocumentSummary(
                id=row["id"],
                collection_id=row["collection_id"],
                filename=row["filename"],
                doc_type=row["doc_type"],
                sha256=row["sha256"],
                ingested_at=row["ingested_at"],
                pages=row["pages"],
            )
            for row in rows
        ]

    # ------------------------------------------------------------------ runs

    async def create_run(
        self,
        *,
        collection_id: UUID,
        run_id: UUID,
        idempotency_key: str,
        trigger: str = "api",
    ) -> tuple[UUID, bool]:
        """Return (run_id, duplicate). A repeated idempotency key returns the first run."""
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """
                INSERT INTO runs (id, collection_id, idempotency_key, trigger)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (collection_id, idempotency_key) DO NOTHING
                RETURNING id
                """,
                (run_id, collection_id, idempotency_key, trigger),
            )
            row = await cur.fetchone()
            if row is not None:
                return row["id"], False
            cur = await conn.execute(
                "SELECT id FROM runs WHERE collection_id = %s AND idempotency_key = %s",
                (collection_id, idempotency_key),
            )
            existing = await cur.fetchone()
            assert existing is not None
            return existing["id"], True

    async def find_run_by_idempotency_key(
        self, collection_id: UUID, idempotency_key: str
    ) -> UUID | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT id FROM runs WHERE collection_id = %s AND idempotency_key = %s",
                (collection_id, idempotency_key),
            )
            row = await cur.fetchone()
        return row["id"] if row is not None else None

    async def block_run(self, run_id: UUID, reason: str) -> None:
        """Park the run as blocked. Nothing was committed, so the register is untouched;
        this is what makes blocked runs findable instead of looking merely unfinished."""
        async with self.pool.connection() as conn:
            await conn.execute(
                "UPDATE runs SET status = 'blocked', ended_at = now() WHERE id = %s",
                (run_id,),
            )

    async def fail_run(self, run_id: UUID, reason: str) -> None:
        """Park the run as failed. Its input never became a document, so there is nothing
        to resume; the row stands as the record that this input was tried and could not
        be read."""
        async with self.pool.connection() as conn:
            await conn.execute(
                "UPDATE runs SET status = 'failed', ended_at = now() WHERE id = %s",
                (run_id,),
            )

    async def set_run_trigger_document(self, run_id: UUID, document_id: UUID) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "UPDATE runs SET trigger_doc_id = %s WHERE id = %s",
                (document_id, run_id),
            )

    async def set_run_ruleset(self, run_id: UUID, ruleset_id: UUID) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "UPDATE runs SET ruleset_id = %s WHERE id = %s",
                (ruleset_id, run_id),
            )

    # ------------------------------------------------------------------ documents

    async def put_document(self, document: Document) -> tuple[Document, bool]:
        byte_size = len(document.text.encode("utf-8"))
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """
                INSERT INTO documents
                    (id, collection_id, sha256, filename, mime_type, byte_size,
                     doc_type, doc_type_conf, normalized_text, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'parsed')
                ON CONFLICT (collection_id, sha256) DO NOTHING
                RETURNING id
                """,
                (
                    document.id,
                    document.collection_id,
                    document.sha256,
                    document.filename,
                    document.mime_type,
                    byte_size,
                    document.doc_type,
                    document.doc_type_confidence,
                    document.text,
                ),
            )
            row = await cur.fetchone()
            if row is not None:
                return document, False

            cur = await conn.execute(
                """
                SELECT id, filename, mime_type, normalized_text, sha256,
                       doc_type, doc_type_conf, status, agreement_ref
                  FROM documents
                 WHERE collection_id = %s AND sha256 = %s
                """,
                (document.collection_id, document.sha256),
            )
            existing = await cur.fetchone()
            assert existing is not None
            return (
                Document(
                    collection_id=document.collection_id,
                    filename=existing["filename"],
                    mime_type=existing["mime_type"],
                    text=existing["normalized_text"] or "",
                    sha256=existing["sha256"],
                    id=existing["id"],
                    doc_type=existing["doc_type"],
                    doc_type_confidence=existing["doc_type_conf"],
                    quarantined=existing["status"] == "quarantined",
                    agreement_ref=existing["agreement_ref"],
                ),
                True,
            )

    async def get_document(self, document_id: UUID) -> Document | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT id, collection_id, filename, mime_type, normalized_text, sha256,
                       doc_type, doc_type_conf, status, agreement_ref
                  FROM documents
                 WHERE id = %s
                """,
                (document_id,),
            )
            row = await cur.fetchone()
        if row is None:
            return None
        return Document(
            collection_id=row["collection_id"],
            filename=row["filename"],
            mime_type=row["mime_type"],
            text=row["normalized_text"] or "",
            sha256=row["sha256"],
            id=row["id"],
            doc_type=row["doc_type"],
            doc_type_confidence=row["doc_type_conf"],
            quarantined=row["status"] == "quarantined",
            agreement_ref=row["agreement_ref"],
        )

    async def set_document_type(
        self, document_id: UUID, doc_type: str, confidence: float | None
    ) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "UPDATE documents SET doc_type = %s, doc_type_conf = %s WHERE id = %s",
                (doc_type, confidence, document_id),
            )

    async def link_supersession(self, document_id: UUID, supersedes_id: UUID) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "UPDATE documents SET supersedes_id = %s WHERE id = %s",
                (supersedes_id, document_id),
            )

    async def put_document_relations(self, relations: list[DocumentRelation]) -> None:
        if not relations:
            return
        async with self.pool.connection() as conn:
            for relation in relations:
                await conn.execute(
                    """
                    INSERT INTO document_relations
                        (id, document_id, kind, target_ref, target_document_id,
                         evidence_quote, block_id, quote_start, quote_end)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (document_id, kind, target_ref, block_id, quote_start)
                    DO NOTHING
                    """,
                    (
                        relation.id,
                        relation.document_id,
                        relation.kind,
                        relation.target_ref,
                        relation.target_document_id,
                        relation.evidence_quote,
                        relation.block_id,
                        relation.quote_start,
                        relation.quote_end,
                    ),
                )

    async def list_document_relations(self, document_id: UUID) -> list[DocumentRelation]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT id, document_id, kind, target_ref, target_document_id,
                       evidence_quote, block_id, quote_start, quote_end
                  FROM document_relations
                 WHERE document_id = %s
                 ORDER BY quote_start
                """,
                (document_id,),
            )
            rows = await cur.fetchall()
        return [
            DocumentRelation(
                document_id=row["document_id"],
                kind=row["kind"],
                target_ref=row["target_ref"],
                evidence_quote=row["evidence_quote"],
                block_id=row["block_id"],
                quote_start=row["quote_start"],
                quote_end=row["quote_end"],
                target_document_id=row["target_document_id"],
                id=row["id"],
            )
            for row in rows
        ]

    async def documents_by_agreement_ref(self, collection_id: UUID) -> dict[str, UUID]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT agreement_ref, id FROM documents
                 WHERE collection_id = %s AND agreement_ref IS NOT NULL
                 ORDER BY ingested_at
                """,
                (collection_id,),
            )
            rows = await cur.fetchall()
        return {row["agreement_ref"]: row["id"] for row in rows}

    async def set_agreement_ref(self, document_id: UUID, agreement_ref: str) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "UPDATE documents SET agreement_ref = %s WHERE id = %s",
                (agreement_ref, document_id),
            )

    async def find_supersession_target(
        self, collection_id: UUID, exclude_document_id: UUID
    ) -> UUID | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT id FROM documents
                 WHERE collection_id = %s AND id <> %s
                   AND doc_type IN ('master_agreement', 'sow')
                 ORDER BY ingested_at DESC LIMIT 1
                """,
                (collection_id, exclude_document_id),
            )
            row = await cur.fetchone()
        return row["id"] if row else None

    # ------------------------------------------------------------------ blocks

    async def put_blocks(self, blocks: list[Block]) -> None:
        """Replay-safe. On re-run the persisted block id wins and is written back
        into the caller's objects, so citations keep pointing at one row."""
        if not blocks:
            return
        async with self.pool.connection() as conn:
            for block in blocks:
                cur = await conn.execute(
                    """
                    INSERT INTO document_blocks
                        (id, document_id, block_index, char_start, char_end,
                         text, text_sha256, injection_flag, page, extraction_method)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (document_id, block_index)
                    DO UPDATE SET injection_flag = EXCLUDED.injection_flag
                    RETURNING id
                    """,
                    (
                        block.id,
                        block.document_id,
                        block.index,
                        block.char_start,
                        block.char_end,
                        block.text,
                        block.text_sha256,
                        block.injection_flag,
                        block.page,
                        block.extraction_method,
                    ),
                )
                row = await cur.fetchone()
                assert row is not None
                block.id = row["id"]

    async def get_blocks(self, document_id: UUID) -> dict[UUID, Block]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT id, document_id, block_index, text, text_sha256,
                       char_start, char_end, injection_flag, page, extraction_method
                  FROM document_blocks
                 WHERE document_id = %s
                 ORDER BY block_index
                """,
                (document_id,),
            )
            rows = await cur.fetchall()
        return {
            row["id"]: Block(
                document_id=row["document_id"],
                index=row["block_index"],
                text=row["text"],
                text_sha256=row["text_sha256"],
                char_start=row["char_start"],
                char_end=row["char_end"],
                id=row["id"],
                injection_flag=row["injection_flag"],
                page=row["page"],
                extraction_method=row["extraction_method"],
            )
            for row in rows
        }

    async def get_blocks_by_ids(self, block_ids: list[UUID]) -> dict[UUID, Block]:
        if not block_ids:
            return {}
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT id, document_id, block_index, text, text_sha256,
                       char_start, char_end, injection_flag, page, extraction_method
                  FROM document_blocks
                 WHERE id = ANY(%s)
                """,
                (list(block_ids),),
            )
            rows = await cur.fetchall()
        return {
            row["id"]: Block(
                document_id=row["document_id"],
                index=row["block_index"],
                text=row["text"],
                text_sha256=row["text_sha256"],
                char_start=row["char_start"],
                char_end=row["char_end"],
                id=row["id"],
                injection_flag=row["injection_flag"],
                page=row["page"],
                extraction_method=row["extraction_method"],
            )
            for row in rows
        }

    # ------------------------------------------------------------------ facts

    async def put_facts(
        self, collection_id: UUID, facts: list[FactCandidate]
    ) -> list[FactCandidate]:
        if not facts:
            return []
        async with self.pool.connection() as conn:
            for fact in facts:
                cur = await conn.execute(
                    """
                    INSERT INTO facts
                        (collection_id, document_id, block_id, key, value,
                         quote, quote_start, quote_end, fact_fingerprint, scope, evidence)
                    SELECT %s, b.document_id, b.id, %s, %s, %s, %s, %s, %s, %s, %s
                      FROM document_blocks b
                     WHERE b.id = %s
                    ON CONFLICT (collection_id, document_id, block_id, fact_fingerprint)
                    DO UPDATE SET key = EXCLUDED.key, scope = EXCLUDED.scope,
                                  evidence = EXCLUDED.evidence
                    RETURNING id
                    """,
                    (
                        collection_id,
                        fact.key,
                        Jsonb(fact.value),
                        fact.quote,
                        fact.quote_start,
                        fact.quote_end,
                        fact.fingerprint,
                        Jsonb(fact.scope.as_dict()),
                        Jsonb(fact.evidence.as_dict() if fact.evidence else {}),
                        fact.block_id,
                    ),
                )
                row = await cur.fetchone()
                # DO UPDATE (not DO NOTHING) so a replayed insert still returns the
                # stored id; conflicts and citations reference facts by id.
                if row is not None:
                    fact.id = row["id"]
        return facts

    async def get_active_facts(self, collection_id: UUID, keys: list[str]) -> list[StoredFact]:
        if not keys:
            return []
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT f.id, f.key, f.value, f.fact_fingerprint, f.quote, f.scope,
                       f.evidence, f.document_id, f.block_id, d.doc_type, d.supersedes_id
                  FROM facts f
                  JOIN documents d ON d.id = f.document_id
                 WHERE f.collection_id = %s AND f.key = ANY(%s)
                 ORDER BY d.ingested_at, f.created_at
                """,
                (collection_id, list(keys)),
            )
            rows = await cur.fetchall()
        return [
            StoredFact(
                id=row["id"],
                key=row["key"],
                value=row["value"],
                fingerprint=row["fact_fingerprint"],
                quote=row["quote"],
                document_id=row["document_id"],
                block_id=row["block_id"],
                doc_type=row["doc_type"],
                supersedes_id=row["supersedes_id"],
                scope=FactScope.from_dict(row["scope"]),
                evidence=EvidenceSpan.from_dict(row["evidence"]),
            )
            for row in rows
        ]

    async def get_facts_by_ids(self, fact_ids: list[UUID]) -> dict[UUID, StoredFact]:
        if not fact_ids:
            return {}
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT f.id, f.key, f.value, f.fact_fingerprint, f.quote, f.scope,
                       f.evidence, f.document_id, f.block_id, d.doc_type, d.supersedes_id
                  FROM facts f
                  JOIN documents d ON d.id = f.document_id
                 WHERE f.id = ANY(%s)
                """,
                (list(fact_ids),),
            )
            rows = await cur.fetchall()
        return {
            row["id"]: StoredFact(
                id=row["id"],
                key=row["key"],
                value=row["value"],
                fingerprint=row["fact_fingerprint"],
                quote=row["quote"],
                document_id=row["document_id"],
                block_id=row["block_id"],
                doc_type=row["doc_type"],
                supersedes_id=row["supersedes_id"],
                scope=FactScope.from_dict(row["scope"]),
                evidence=EvidenceSpan.from_dict(row["evidence"]),
            )
            for row in rows
        }

    async def affected_register_keys(
        self, collection_id: UUID, scoped_keys: list[str], document_id: UUID
    ) -> list[str]:
        """New scoped keys plus every register row still citing the superseded document.

        The second half is the reverse citation index doing the invalidation work,
        so no model is ever asked which items a new document touches. An invalidated row
        contributes its own agreement, so amending one agreement cannot pull a different
        agreement's row into this run's affected set.
        """
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT DISTINCT ri.agreement_id || %s || ri.key AS scoped
                  FROM register_items ri
                  JOIN register_item_citations ric ON ric.register_item_id = ri.id
                  JOIN facts f ON f.id = ric.fact_id
                 WHERE ri.collection_id = %s
                   AND f.document_id = (SELECT supersedes_id FROM documents WHERE id = %s)
                """,
                (REGISTER_KEY_SEPARATOR, collection_id, document_id),
            )
            rows = await cur.fetchall()
        return sorted(set(scoped_keys) | {row["scoped"] for row in rows})

    async def get_register_items(
        self, collection_id: UUID, scoped_keys: list[str]
    ) -> dict[str, RegisterItem]:
        if not scoped_keys:
            return {}
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT ri.collection_id, ri.id, ri.key, ri.agreement_id, ri.value, ri.state,
                       ri.content_hash, ri.version,
                       COALESCE(
                           array_agg(ric.fact_id) FILTER (WHERE ric.fact_id IS NOT NULL),
                           '{}'
                       ) AS fact_ids,
                       COALESCE(
                           array_agg(f.fact_fingerprint)
                               FILTER (WHERE f.fact_fingerprint IS NOT NULL),
                           '{}'
                       ) AS fingerprints
                  FROM register_items ri
                  LEFT JOIN register_item_citations ric ON ric.register_item_id = ri.id
                  LEFT JOIN facts f ON f.id = ric.fact_id
                 WHERE ri.collection_id = %s
                   AND ri.agreement_id || %s || ri.key = ANY(%s)
                 GROUP BY ri.id
                """,
                (collection_id, REGISTER_KEY_SEPARATOR, list(scoped_keys)),
            )
            rows = await cur.fetchall()
        return {
            RegisterKey(row["agreement_id"], row["key"]).text: RegisterItem(
                collection_id=row["collection_id"],
                key=row["key"],
                value=row["value"],
                state=row["state"],
                content_hash=row["content_hash"],
                version=row["version"],
                id=row["id"],
                citation_fact_ids=list(row["fact_ids"]),
                citation_fingerprints=sorted(row["fingerprints"]),
                agreement_id=row["agreement_id"],
            )
            for row in rows
        }

    # ------------------------------------------------------------------ conflicts

    async def put_conflicts(self, conflicts: list[Conflict]) -> list[Conflict]:
        if not conflicts:
            return []
        stored: list[Conflict] = []
        async with self.pool.connection() as conn:
            for conflict in conflicts:
                cur = await conn.execute(
                    """
                    INSERT INTO conflicts
                        (id, collection_id, key, fact_a_id, fact_b_id,
                         kind, rationale, state, detected_run)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (collection_id, key, fact_a_id, fact_b_id)
                    DO UPDATE SET rationale = EXCLUDED.rationale
                    RETURNING id, state
                    """,
                    (
                        conflict.id,
                        conflict.collection_id,
                        conflict.key,
                        conflict.fact_a_id,
                        conflict.fact_b_id,
                        conflict.kind,
                        conflict.rationale,
                        conflict.state,
                        conflict.detected_run,
                    ),
                )
                row = await cur.fetchone()
                assert row is not None
                conflict.id = row["id"]
                conflict.state = row["state"]
                stored.append(conflict)
        return stored

    async def list_conflicts(
        self, collection_id: UUID, *, state: str | None = "open"
    ) -> list[Conflict]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT id, collection_id, key, fact_a_id, fact_b_id,
                       kind, rationale, state, detected_run
                  FROM conflicts
                 WHERE collection_id = %s AND (%s::text IS NULL OR state = %s)
                 ORDER BY created_at
                """,
                (collection_id, state, state),
            )
            rows = await cur.fetchall()
        return [
            Conflict(
                collection_id=row["collection_id"],
                key=row["key"],
                fact_a_id=row["fact_a_id"],
                fact_b_id=row["fact_b_id"],
                kind=row["kind"],
                rationale=row["rationale"],
                id=row["id"],
                state=row["state"],
                detected_run=row["detected_run"],
            )
            for row in rows
        ]

    # ------------------------------------------------------------------ events

    # ------------------------------------------------------------------ rules

    async def put_ruleset(self, ruleset: Ruleset) -> Ruleset:
        """Store a playbook version. Re-uploading the same name and version is a no-op
        that returns the stored rules, so rule ids stay stable across runs."""
        async with self.pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                """
                INSERT INTO rulesets (collection_id, name, version, source_text)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (collection_id, name, version) DO NOTHING
                RETURNING id
                """,
                (ruleset.collection_id, ruleset.name, ruleset.version, ruleset.source_text),
            )
            row = await cur.fetchone()
            if row is None:
                cur = await conn.execute(
                    """
                    SELECT id FROM rulesets
                     WHERE collection_id = %s AND name = %s AND version = %s
                    """,
                    (ruleset.collection_id, ruleset.name, ruleset.version),
                )
                row = await cur.fetchone()
                assert row is not None
            ruleset.id = row["id"]

            for rule in ruleset.rules:
                cur = await conn.execute(
                    """
                    INSERT INTO rules (ruleset_id, code, text, severity, scope, target_keys)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (ruleset_id, code)
                    DO UPDATE SET text = EXCLUDED.text,
                                  severity = EXCLUDED.severity,
                                  scope = EXCLUDED.scope,
                                  target_keys = EXCLUDED.target_keys
                    RETURNING id
                    """,
                    (
                        ruleset.id,
                        rule.code,
                        rule.text,
                        rule.severity,
                        rule.scope,
                        rule.target_keys,
                    ),
                )
                stored = await cur.fetchone()
                assert stored is not None
                rule.id = stored["id"]
        return ruleset

    async def get_active_ruleset(self, collection_id: UUID) -> Ruleset | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT id, collection_id, name, version, source_text FROM rulesets
                 WHERE collection_id = %s
                 ORDER BY version DESC, created_at DESC
                 LIMIT 1
                """,
                (collection_id,),
            )
            head = await cur.fetchone()
        return await self._ruleset_from(head) if head else None

    async def get_ruleset(self, ruleset_id: UUID) -> Ruleset | None:
        """Fetch one pinned playbook by id, so a run keeps evaluating against the version
        it started with even after a newer one is uploaded."""
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT id, collection_id, name, version, source_text FROM rulesets WHERE id = %s",
                (ruleset_id,),
            )
            head = await cur.fetchone()
        return await self._ruleset_from(head) if head else None

    async def _ruleset_from(self, head: dict) -> Ruleset:
        collection_id = head["collection_id"]
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT id, code, text, severity, scope, target_keys FROM rules
                 WHERE ruleset_id = %s ORDER BY code
                """,
                (head["id"],),
            )
            rows = await cur.fetchall()
        return Ruleset(
            collection_id=collection_id,
            name=head["name"],
            version=head["version"],
            source_text=head["source_text"],
            id=head["id"],
            rules=[
                Rule(
                    code=row["code"],
                    text=row["text"],
                    severity=row["severity"],
                    scope=row["scope"],
                    target_keys=list(row["target_keys"] or []),
                    id=row["id"],
                )
                for row in rows
            ],
        )

    async def put_findings(self, findings: list[Finding]) -> list[Finding]:
        if not findings:
            return []
        async with self.pool.connection() as conn, conn.transaction():
            for finding in findings:
                cur = await conn.execute(
                    """
                    INSERT INTO findings
                        (run_id, rule_id, target_kind, target_id, target_key,
                         verdict, rationale)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (run_id, rule_id, target_kind, target_id) DO NOTHING
                    RETURNING id
                    """,
                    (
                        finding.run_id,
                        finding.rule_id,
                        finding.target_kind,
                        finding.target_id,
                        finding.target_key,
                        finding.system_verdict,
                        finding.rationale,
                    ),
                )
                row = await cur.fetchone()
                if row is None:
                    # Replayed stage: adopt the row already recorded, citations included.
                    cur = await conn.execute(
                        """
                        SELECT id FROM findings
                         WHERE run_id = %s AND rule_id = %s
                           AND target_kind = %s AND target_id = %s
                        """,
                        (
                            finding.run_id,
                            finding.rule_id,
                            finding.target_kind,
                            finding.target_id,
                        ),
                    )
                    row = await cur.fetchone()
                    assert row is not None
                    finding.id = row["id"]
                    continue

                finding.id = row["id"]
                for citation in finding.citations:
                    await conn.execute(
                        """
                        INSERT INTO finding_citations
                            (finding_id, block_id, quote, quote_start, quote_end)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT DO NOTHING
                        """,
                        (
                            finding.id,
                            citation.block_id,
                            citation.quote,
                            citation.quote_start,
                            citation.quote_end,
                        ),
                    )
        return findings

    async def list_findings(self, run_id: UUID) -> list[Finding]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT f.id, f.run_id, f.rule_id, r.code, r.severity, f.target_kind,
                       f.target_id, f.target_key, f.verdict, f.rationale,
                       f.review_decision, f.decided_by, f.recheck_required
                  FROM findings f
                  JOIN rules r ON r.id = f.rule_id
                 WHERE f.run_id = %s
                 ORDER BY f.target_kind, f.target_key, r.code
                """,
                (run_id,),
            )
            rows = await cur.fetchall()
            cur = await conn.execute(
                """
                SELECT c.finding_id, c.block_id, c.quote, c.quote_start, c.quote_end
                  FROM finding_citations c
                  JOIN findings f ON f.id = c.finding_id
                 WHERE f.run_id = %s
                """,
                (run_id,),
            )
            citation_rows = await cur.fetchall()

        citations: dict[UUID, list[FindingCitation]] = {}
        for row in citation_rows:
            citations.setdefault(row["finding_id"], []).append(
                FindingCitation(
                    block_id=row["block_id"],
                    quote=row["quote"],
                    quote_start=row["quote_start"],
                    quote_end=row["quote_end"],
                )
            )
        return [
            Finding(
                run_id=row["run_id"],
                rule_id=row["rule_id"],
                rule_code=row["code"],
                target_kind=row["target_kind"],
                target_id=row["target_id"],
                target_key=row["target_key"],
                system_verdict=row["verdict"],
                rationale=row["rationale"],
                severity=row["severity"],
                citations=citations.get(row["id"], []),
                review_decision=row["review_decision"],
                decided_by=row["decided_by"],
                recheck_required=row["recheck_required"],
                id=row["id"],
            )
            for row in rows
        ]

    async def record_finding_decisions(
        self, run_id: UUID, actor_id: str, decisions: dict[UUID, str]
    ) -> list[Finding]:
        """Stamp what a human decided, beside the verdict rather than over it.

        `verdict` is never in the SET list. A reviewer may disagree with a finding; a
        reviewer may not edit what the evaluation concluded, and no path in this system
        writes that column after the row is inserted.
        """
        if not decisions:
            return []
        async with self.pool.connection() as conn, conn.transaction():
            for finding_id, decision in decisions.items():
                if decision not in {"upheld", "dismissed"}:
                    raise ValueError(f"invalid review decision: {decision}")
                cur = await conn.execute(
                    """
                    UPDATE findings
                       SET review_decision = %s, decided_by = %s, decided_at = now(),
                           recheck_required = (%s = 'dismissed' AND verdict <> 'pass')
                     WHERE id = %s AND run_id = %s
                    RETURNING id
                    """,
                    (decision, actor_id, decision, finding_id, run_id),
                )
                if await cur.fetchone() is None:
                    raise ValueError(f"finding {finding_id} does not belong to run {run_id}")
        return await self.list_findings(run_id)

    async def drop_source_rule_cache(self, cache_key: str) -> None:
        """Forget that this document was already judged.

        A dismissed source finding is a judgement about one run's evidence. Leaving the
        cache entry in place would serve the dismissed verdict back to every later upload
        of the same bytes, which quietly promotes one reviewer's call to standing policy.
        """
        async with self.pool.connection() as conn:
            await conn.execute("DELETE FROM source_rule_cache WHERE cache_key = %s", (cache_key,))

    async def get_source_rule_cache(self, cache_key: str) -> UUID | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT run_id FROM source_rule_cache WHERE cache_key = %s", (cache_key,)
            )
            row = await cur.fetchone()
        return row["run_id"] if row else None

    async def put_source_rule_cache(
        self,
        cache_key: str,
        *,
        collection_id: UUID,
        run_id: UUID,
        document_sha256: str,
        ruleset_hash: str,
        evaluator_version: str,
    ) -> None:
        async with self.pool.connection() as conn:
            # First writer wins: a later run that re-evaluated the same triple reached
            # the same verdicts, so repointing the key would gain nothing.
            await conn.execute(
                """
                INSERT INTO source_rule_cache
                    (cache_key, collection_id, document_sha256, ruleset_hash,
                     evaluator_version, run_id)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (cache_key) DO NOTHING
                """,
                (
                    cache_key,
                    collection_id,
                    document_sha256,
                    ruleset_hash,
                    evaluator_version,
                    run_id,
                ),
            )

    # ------------------------------------------------------- exactly-once bookkeeping

    async def record_stage(
        self,
        run_id: UUID,
        stage: str,
        *,
        input_hash: str,
        output_hash: str | None = None,
        status: str = "completed",
    ) -> None:
        async with self.pool.connection() as conn:
            await self._record_stage(
                conn, run_id, stage, input_hash, output_hash=output_hash, status=status
            )

    @staticmethod
    async def _record_stage(
        conn,
        run_id: UUID,
        stage: str,
        input_hash: str,
        *,
        output_hash: str | None,
        status: str,
    ) -> None:
        """Upsert on the given connection, so a caller inside a transaction stays in it.

        `COALESCE(output_hash, EXCLUDED.output_hash)` keeps the first execution's result:
        the ledger records what the run produced, and a second execution silently
        rewriting that is precisely the thing it exists to make visible. `attempts`
        counts the replays instead.
        """
        await conn.execute(
            """
            INSERT INTO run_stage_ledger (run_id, stage, input_hash, output_hash, status)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (run_id, stage, input_hash) DO UPDATE
               SET attempts = run_stage_ledger.attempts + CASE
                       -- Finishing an execution that already announced itself is not a
                       -- second execution. Anything else is: a stage re-entered after a
                       -- crash, or re-run from a replayed checkpoint.
                       WHEN run_stage_ledger.status = 'started'
                        AND EXCLUDED.status = 'completed' THEN 0
                       ELSE 1
                   END,
                   status = EXCLUDED.status,
                   output_hash = COALESCE(run_stage_ledger.output_hash, EXCLUDED.output_hash),
                   updated_at = now()
            """,
            (run_id, stage, input_hash, output_hash, status),
        )

    async def stage_record(
        self, run_id: UUID, stage: str, input_hash: str
    ) -> StageRecord | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT run_id, stage, input_hash, output_hash, status, attempts
                  FROM run_stage_ledger
                 WHERE run_id = %s AND stage = %s AND input_hash = %s
                """,
                (run_id, stage, input_hash),
            )
            row = await cur.fetchone()
        return StageRecord(**row) if row else None

    async def list_stages(self, run_id: UUID) -> list[StageRecord]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT run_id, stage, input_hash, output_hash, status, attempts
                  FROM run_stage_ledger WHERE run_id = %s ORDER BY created_at, stage
                """,
                (run_id,),
            )
            rows = await cur.fetchall()
        return [StageRecord(**row) for row in rows]

    async def acquire_run_lease(self, run_id: UUID, owner: str, *, ttl_seconds: int) -> bool:
        """Compare-and-set: the WHERE clause is the precondition, and one caller wins.

        Free, expired, or already ours are all takeable. Held by someone else and still
        live is the only refusal -- and it has to expire, because a process SIGKILLed
        holding a lease must not lock its own run out of the resume that would recover it.
        """
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """
                UPDATE runs
                   SET lease_owner = %s,
                       lease_expires_at = now() + make_interval(secs => %s)
                 WHERE id = %s
                   AND (lease_owner IS NULL OR lease_owner = %s OR lease_expires_at < now())
                RETURNING id
                """,
                (owner, ttl_seconds, run_id, owner),
            )
            return await cur.fetchone() is not None

    async def release_run_lease(self, run_id: UUID, owner: str) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                """
                UPDATE runs SET lease_owner = NULL, lease_expires_at = NULL
                 WHERE id = %s AND lease_owner = %s
                """,
                (run_id, owner),
            )

    async def add_event(self, event: RunEvent) -> None:
        # ponytail: seq via MAX+1 is safe because a run's nodes execute serially.
        # Parallel fan-out inside one run would need a per-run sequence instead.
        started_at = event.created_at - timedelta(milliseconds=event.duration_ms)
        async with self.pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO run_events
                    (run_id, seq, stage, decision, reason, next_node, error_class,
                     tokens_in, tokens_out, cost_usd, model, cache_hit, external_svc,
                     started_at, ended_at, duration_ms)
                SELECT %s, COALESCE(MAX(seq), 0) + 1, %s, %s, %s, %s, %s,
                       %s, %s, %s, %s, %s, %s, %s, %s, %s
                  FROM run_events WHERE run_id = %s
                """,
                (
                    event.run_id,
                    event.stage,
                    event.decision,
                    event.reason,
                    event.next_node,
                    event.error_class,
                    event.tokens_in,
                    event.tokens_out,
                    event.cost_usd,
                    event.model,
                    event.cache_hit,
                    event.external_service,
                    started_at,
                    event.created_at,
                    event.duration_ms,
                    event.run_id,
                ),
            )

    async def list_events(self, run_id: UUID) -> list[RunEvent]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT run_id, stage, decision, reason, next_node, error_class,
                       tokens_in, tokens_out, cost_usd, model, cache_hit, external_svc,
                       ended_at, duration_ms
                  FROM run_events WHERE run_id = %s ORDER BY seq
                """,
                (run_id,),
            )
            rows = await cur.fetchall()
        return [
            RunEvent(
                run_id=row["run_id"],
                stage=row["stage"],
                decision=row["decision"] or "",
                reason=row["reason"] or "",
                next_node=row["next_node"] or "",
                error_class=row["error_class"],
                model=row["model"],
                cache_hit=row["cache_hit"],
                external_service=row["external_svc"],
                duration_ms=row["duration_ms"] or 0,
                tokens_in=row["tokens_in"],
                tokens_out=row["tokens_out"],
                cost_usd=float(row["cost_usd"]),
                created_at=row["ended_at"],
            )
            for row in rows
        ]

    # ------------------------------------------------------------------ review

    async def add_review_items(self, items: list[ReviewItem]) -> None:
        if not items:
            return
        async with self.pool.connection() as conn:
            for item in items:
                target_id = review_target_id(item.kind, item.target_key)
                payload = {**item.payload, "target_key": item.target_key}
                cur = await conn.execute(
                    """
                    INSERT INTO review_items
                        (id, run_id, collection_id, kind, target_id, payload)
                    SELECT %s, r.id, r.collection_id, %s, %s, %s
                      FROM runs r WHERE r.id = %s
                    ON CONFLICT (run_id, kind, target_id) DO NOTHING
                    RETURNING id
                    """,
                    (item.id, item.kind, target_id, Jsonb(payload), item.run_id),
                )
                row = await cur.fetchone()
                if row is not None:
                    continue
                # Replayed node: adopt the id already persisted for this decision.
                cur = await conn.execute(
                    """
                    SELECT id FROM review_items
                     WHERE run_id = %s AND kind = %s AND target_id = %s
                    """,
                    (item.run_id, item.kind, target_id),
                )
                existing = await cur.fetchone()
                if existing is None:
                    raise ValueError(f"run {item.run_id} does not exist")
                item.id = existing["id"]

    async def list_review_items(self, run_id: UUID) -> list[ReviewItem]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT id, run_id, kind, payload, state, decided_by, comment
                  FROM review_items WHERE run_id = %s ORDER BY kind, target_id
                """,
                (run_id,),
            )
            rows = await cur.fetchall()
        return [
            ReviewItem(
                run_id=row["run_id"],
                kind=row["kind"],
                target_key=row["payload"].get("target_key", ""),
                payload=row["payload"],
                id=row["id"],
                state=row["state"],
                decided_by=row["decided_by"],
                comment=row["comment"],
            )
            for row in rows
        ]

    async def decide_review_items(
        self, run_id: UUID, actor_id: str, decisions: dict[UUID, str]
    ) -> list[ReviewItem]:
        def as_item(row: dict) -> ReviewItem:
            return ReviewItem(
                run_id=row["run_id"],
                kind=row["kind"],
                target_key=row["payload"].get("target_key", ""),
                payload=row["payload"],
                id=row["id"],
                state=row["state"],
                decided_by=row["decided_by"],
                comment=row["comment"],
            )

        decided: list[ReviewItem] = []
        async with self.pool.connection() as conn:
            for item_id, decision in decisions.items():
                if decision not in {"approved", "rejected"}:
                    raise ValueError(f"invalid decision: {decision}")
                cur = await conn.execute(
                    "SELECT * FROM review_items WHERE id = %s", (item_id,)
                )
                owner = await cur.fetchone()
                if owner is None:
                    raise ValueError(f"review item {item_id} does not exist")
                if owner["run_id"] != run_id:
                    raise ValueError("review item belongs to a different run")
                if owner["state"] != "pending":
                    # A crash between this write and its checkpoint replays the node.
                    # The same actor re-sending the same decision is that replay, not a
                    # second decision: return what stands. Anything else lost the CAS.
                    if owner["state"] == decision and owner["decided_by"] == actor_id:
                        decided.append(as_item(owner))
                        continue
                    raise ItemAlreadyDecidedError(f"review item {item_id} was already decided")
                try:
                    cur = await conn.execute(
                        "SELECT * FROM decide_review_item(%s, %s, %s, NULL)",
                        (item_id, decision, actor_id),
                    )
                    row = await cur.fetchone()
                except errors.RaiseException as exc:
                    # The compare-and-set lost: another actor decided this row first.
                    raise ItemAlreadyDecidedError(
                        f"review item {item_id} was already decided"
                    ) from exc
                assert row is not None
                decided.append(as_item(row))
        return decided

    # ------------------------------------------------------------------ commit

    async def commit_approved(
        self, collection_id: UUID, run_id: UUID, *, basis_hash: str
    ) -> CommitResult:
        """One transaction: read approvals, then lock, write the register, ledger it.

        The advisory lock is deliberately *not* taken at the top. It serialises every
        commit in the collection, so everything under it is time other runs spend
        waiting: reading this run's own approved review items needs no lock at all, and
        holding one across that read made the critical section longer than the work.
        It is acquired immediately before the first canonical-register read and released
        with the transaction.
        """
        result = CommitResult()
        committed = result.committed
        async with self.pool.connection() as conn, conn.transaction():
            # Already committed this exact deliverable. A replay must return what was
            # written, not write it again: the per-key hash comparison below would
            # usually no-op, but "usually" is not what exactly-once means, and a
            # concurrent commit in between is the case where it stops being true.
            cur = await conn.execute(
                """
                SELECT output_hash FROM run_stage_ledger
                 WHERE run_id = %s AND stage = 'commit_approved' AND input_hash = %s
                   AND status = 'completed'
                """,
                (run_id, basis_hash),
            )
            if await cur.fetchone() is not None:
                await self._record_stage(
                    conn, run_id, "commit_approved", basis_hash,
                    output_hash=None, status="completed",
                )
                result.committed = await self._committed_by(conn, collection_id, run_id)
                return result

            # A finding's decision was recorded at the gate that made it, by
            # `record_finding_decisions`. Writing it here too would mean a run that never
            # reached commit -- blocked, stale, refused -- lost every decision a human had
            # already made, which is the opposite of an audit trail.

            cur = await conn.execute(
                """
                SELECT id, payload FROM review_items
                 WHERE run_id = %s AND state = 'approved'
                   AND kind IN ('register_update', 'conflict')
                 ORDER BY target_id
                """,
                (run_id,),
            )
            approved = await cur.fetchall()

            # From here on the canonical register is being read and written, so this is
            # where the collection has to be serialised -- and no earlier.
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))", (str(collection_id),)
            )
            await self._snapshot(conn, collection_id, run_id, "before")

            for review in approved:
                payload = review["payload"]
                after = payload["after"]
                # The register row's identity is (agreement, obligation). A review item
                # addresses it by its text form, which is what the graph proposed against.
                scoped = RegisterKey.parse(payload.get("target_key") or "")
                key = scoped.key
                value = after["value"]
                state = after.get("state", "supported")
                fingerprints = after.get("citation_fingerprints", [])
                fact_ids = [UUID(str(fact_id)) for fact_id in after.get("citation_fact_ids", [])]
                content_hash = register_content_hash(
                    value=value, evidence_fingerprints=fingerprints, state=state
                )

                cur = await conn.execute(
                    """
                    SELECT id, value, content_hash, version, last_run_id
                      FROM register_items
                     WHERE collection_id = %s AND agreement_id = %s AND key = %s
                    FOR UPDATE
                    """,
                    (collection_id, scoped.agreement_id, key),
                )
                existing = await cur.fetchone()

                if existing is not None and existing["content_hash"] == content_hash:
                    # Either a replayed commit or a genuine no-op proposal. Both mean
                    # the stored item is already correct: no rewrite, no version bump.
                    committed.append(
                        RegisterItem(
                            collection_id=collection_id,
                            key=key,
                            value=existing["value"],
                            state=state,
                            content_hash=content_hash,
                            version=existing["version"],
                            id=existing["id"],
                            agreement_id=scoped.agreement_id,
                        )
                    )
                    await self._resolve_conflict(conn, payload)
                    continue

                stale = stale_proposal(
                    scoped.text,
                    payload,
                    actual_version=existing["version"] if existing else None,
                    actual_hash=existing["content_hash"] if existing else None,
                )
                if stale is not None:
                    # The item moved after this proposal was derived from it. Writing
                    # now would erase whatever landed in between, and the human who
                    # approved this never saw that value: refuse and report.
                    result.stale.append(stale)
                    continue

                if existing is None:
                    cur = await conn.execute(
                        """
                        INSERT INTO register_items
                            (collection_id, agreement_id, key, title, value, state,
                             content_hash, version, last_run_id)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, 1, %s)
                        RETURNING id, version
                        """,
                        (
                            collection_id,
                            scoped.agreement_id,
                            key,
                            payload.get("title", scoped.label()),
                            Jsonb(value),
                            state,
                            content_hash,
                            run_id,
                        ),
                    )
                else:
                    cur = await conn.execute(
                        """
                        UPDATE register_items
                           SET value = %s, state = %s, content_hash = %s,
                               version = version + 1, last_run_id = %s, updated_at = now()
                         WHERE id = %s AND version = %s
                        RETURNING id, version
                        """,
                        (
                            Jsonb(value),
                            state,
                            content_hash,
                            run_id,
                            existing["id"],
                            existing["version"],
                        ),
                    )
                row = await cur.fetchone()
                if row is None:
                    raise ValueError(
                        f"register item {scoped.text} changed under this run; "
                        "re-derive instead of overwriting"
                    )

                await self._link_citations(conn, row["id"], fact_ids)
                await self._resolve_conflict(conn, payload)
                await conn.execute(
                    """
                    INSERT INTO change_log
                        (collection_id, register_item_id, run_id, caused_by_document_id,
                         old_value, new_value, old_hash, new_hash)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        collection_id,
                        row["id"],
                        run_id,
                        payload.get("document_id"),
                        Jsonb(existing["value"]) if existing else None,
                        Jsonb(value),
                        existing["content_hash"] if existing else None,
                        content_hash,
                    ),
                )
                committed.append(
                    RegisterItem(
                        collection_id=collection_id,
                        key=key,
                        value=value,
                        state=state,
                        content_hash=content_hash,
                        version=row["version"],
                        id=row["id"],
                        citation_fact_ids=fact_ids,
                        citation_fingerprints=list(fingerprints),
                        agreement_id=scoped.agreement_id,
                    )
                )

            await self._snapshot(conn, collection_id, run_id, "after")
            await conn.execute(
                "UPDATE runs SET status = 'committed', ended_at = now() WHERE id = %s",
                (run_id,),
            )
            # Same transaction as the register writes above. That is the whole guarantee:
            # there is no window in which the register has moved and the ledger does not
            # know, so a process killed mid-commit either did all of it or none of it.
            await self._record_stage(
                conn,
                run_id,
                "commit_approved",
                basis_hash,
                output_hash=register_content_hash(
                    value={"committed": sorted(item.register_key.text for item in committed)},
                    evidence_fingerprints=[],
                    state="committed",
                ),
                status="completed",
            )
        return result

    @staticmethod
    async def _committed_by(conn, collection_id: UUID, run_id: UUID) -> list[RegisterItem]:
        """The register rows this run's approvals are about, as they stand now."""
        cur = await conn.execute(
            """
            SELECT ri.collection_id, ri.id, ri.agreement_id, ri.key, ri.value, ri.state,
                   ri.content_hash, ri.version
              FROM register_items ri
              JOIN review_items rv
                ON rv.run_id = %s AND rv.state = 'approved'
               AND rv.kind IN ('register_update', 'conflict')
               AND (rv.payload ->> 'target_key') = ri.agreement_id || %s || ri.key
             WHERE ri.collection_id = %s
             ORDER BY ri.agreement_id, ri.key
            """,
            (run_id, REGISTER_KEY_SEPARATOR, collection_id),
        )
        return [
            RegisterItem(
                collection_id=row["collection_id"],
                key=row["key"],
                value=row["value"],
                state=row["state"],
                content_hash=row["content_hash"],
                version=row["version"],
                id=row["id"],
                agreement_id=row["agreement_id"],
            )
            for row in await cur.fetchall()
        ]

    @staticmethod
    async def _snapshot(conn, collection_id: UUID, run_id: UUID, phase: str) -> None:
        await conn.execute(
            """
            INSERT INTO run_snapshots (run_id, register_item_id, phase, content_hash)
            SELECT %s, id, %s, content_hash FROM register_items WHERE collection_id = %s
            ON CONFLICT (run_id, register_item_id, phase) DO NOTHING
            """,
            (run_id, phase, collection_id),
        )

    @staticmethod
    async def _link_citations(conn, register_item_id: UUID, fact_ids: list[UUID]) -> None:
        # Citations are replaced wholesale: a superseded value must stop citing the
        # fact it replaced, otherwise the reverse invalidation index stays dirty.
        await conn.execute(
            "DELETE FROM register_item_citations WHERE register_item_id = %s",
            (register_item_id,),
        )
        for fact_id in fact_ids:
            await conn.execute(
                """
                INSERT INTO register_item_citations (register_item_id, fact_id)
                VALUES (%s, %s) ON CONFLICT DO NOTHING
                """,
                (register_item_id, fact_id),
            )

    @staticmethod
    async def _resolve_conflict(conn, payload: dict) -> None:
        """An approved proposal closes the conflict it came from. A rejected one is
        never read here, so the conflict stays open rather than silently resolved."""
        conflict = payload.get("conflict")
        if not conflict:
            return
        await conn.execute(
            "UPDATE conflicts SET state = 'accepted' WHERE id = %s AND state = 'open'",
            (UUID(str(conflict["id"])),),
        )

    async def list_register(self, collection_id: UUID) -> list[RegisterItem]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT collection_id, agreement_id, key, value, state,
                       content_hash, version
                  FROM register_items WHERE collection_id = %s
                 ORDER BY agreement_id, key
                """,
                (collection_id,),
            )
            rows = await cur.fetchall()
        return [
            RegisterItem(
                collection_id=row["collection_id"],
                key=row["key"],
                value=row["value"],
                state=row["state"],
                content_hash=row["content_hash"],
                version=row["version"],
                agreement_id=row["agreement_id"],
            )
            for row in rows
        ]

    async def get_run(self, run_id: UUID) -> RunSummary | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT id, collection_id, status, started_at, ended_at,
                       "trigger", trigger_doc_id, ruleset_id
                  FROM runs WHERE id = %s
                """,
                (run_id,),
            )
            row = await cur.fetchone()
        if row is None:
            return None
        return RunSummary(
            run_id=row["id"],
            collection_id=row["collection_id"],
            status=row["status"],
            started_at=row["started_at"],
            ended_at=row["ended_at"],
            trigger=row["trigger"],
            trigger_document_id=row["trigger_doc_id"],
            ruleset_id=row["ruleset_id"],
        )

    async def list_run_changes(self, run_id: UUID) -> list[RegisterChange]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT cl.register_item_id, ri.agreement_id, ri.key,
                       cl.old_value, cl.new_value, cl.old_hash, cl.new_hash, cl.changed_at
                  FROM change_log cl
                  JOIN register_items ri ON ri.id = cl.register_item_id
                 WHERE cl.run_id = %s
                 ORDER BY cl.changed_at
                """,
                (run_id,),
            )
            rows = await cur.fetchall()
        return [
            RegisterChange(
                run_id=run_id,
                register_item_id=row["register_item_id"],
                key=RegisterKey(row["agreement_id"], row["key"]).text,
                old_value=row["old_value"],
                new_value=row["new_value"],
                old_hash=row["old_hash"],
                new_hash=row["new_hash"],
                changed_at=row["changed_at"],
            )
            for row in rows
        ]

    async def list_runs(
        self, collection_id: UUID, *, status: str | None = None
    ) -> list[RunSummary]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT id, collection_id, status, started_at, ended_at,
                       "trigger", trigger_doc_id, ruleset_id
                  FROM runs
                 WHERE collection_id = %s AND (%s::text IS NULL OR status = %s)
                 ORDER BY started_at DESC
                """,
                (collection_id, status, status),
            )
            rows = await cur.fetchall()
        return [
            RunSummary(
                run_id=row["id"],
                collection_id=row["collection_id"],
                status=row["status"],
                started_at=row["started_at"],
                ended_at=row["ended_at"],
                trigger=row["trigger"],
                trigger_document_id=row["trigger_doc_id"],
                ruleset_id=row["ruleset_id"],
            )
            for row in rows
        ]

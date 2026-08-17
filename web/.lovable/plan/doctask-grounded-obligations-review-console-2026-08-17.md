# Doctask — Grounded Obligations Review Console

A dense, legal-tech review console for contract obligations where every value traces to a verbatim quote and every change needs explicit human approval. Frontend only, driven by typed mock fixtures behind a single env switch.

## Design language

- Muted neutral palette (the Doctask design system already in `src/styles.css`), color reserved strictly for state: violation/rejected (danger), disputed/unsupported (warning), approved/supported (success), neutral for missing/pending.
- Tight vertical rhythm, 12–13px table text, tabular numerals, monospace for hashes, register keys, IDs, char offsets.
- Fixed left sidebar (navy) + top breadcrumb bar; no decorative hero imagery anywhere.

## Screens and routes

| Route | Screen |
| --- | --- |
| `/` | Collections table (name, docs, register rows, open runs, last activity) |
| `/collections/$collectionId` | Collection detail with tabs: Register, Documents, Runs, Playbook |
| `/collections/$collectionId/runs/$runId` | Run detail (stage timeline + cost panel) |
| `/collections/$collectionId/runs/$runId/review` | Review queue |
| `/collections/$collectionId/runs/$runId/findings` | Findings table |
| `/collections/$collectionId/documents/$documentId` | Document viewer (split pane) |

Build order: shell + routing + Collections, then Register, then Run Detail, then Review Queue, Findings, Document Viewer.

### Register
Grouped rows per agreement (`(no agreement named)` group for unscoped), columns: obligation key, value, state badge, citation count, version, last changed, truncated `content_hash` in mono. Expanding a row lists every citing quote with filename, page, char offsets and the verbatim text with the grounding span highlighted. Disputed rows render rival values side by side with their sources, left unresolved. Filters: agreement, state, obligation key.

### Run detail
Vertical stage timeline; each stage shows name, duration, decision (continue / retry / skip / escalate / abstain / branch) with reason text. Branch decisions render a visible fork; retried stages list every attempt with error class. Header shows current stage while running and a prominent banner when parked at a human gate. Collapsible cost panel: total compute time, total spend, price-table version, per-stage time/spend/model/tokens/cache hits/external calls; null cost renders `unpriced`, never `$0`.

### Review queue
One independently decidable card per pending item across all seven kinds. Each card: before value, after value, backing citations (quote, document, page, offsets), plus rule code / severity / verdict / rationale for findings. Independent Approve and Reject with optional comment; per-item selection state in Zustand so a mixed set is normal. Sticky summary bar with approved / rejected / undecided counts; submit disabled while anything is undecided. `scope_question` cards have no after value — they present agreement options and are answered. Upheld blocker findings expose a separate override action gated behind a typed reason (React Hook Form + Zod).

### Findings
Every rule verdict for a run including explicit passes so the denominator is visible: rule code, severity, target, verdict, review decision, who decided. Dismissed adverse verdicts show a recheck-required marker.

### Document viewer
Resizable split pane. Left: document text with citation spans highlighted; right: facts/findings extracted from it. Clicking either side scrolls and focuses its counterpart. Prompt-injection-withheld blocks render visibly marked as withheld with their detection signals listed.

## Mock data

Collection "Acme Vendor Agreements" with an MSA (payment 30 days, liability cap 250000, notice 60 days), an amendment (payment 45, notice 90), a NET 10 invoice, and a policy doc containing an injection block. Runs: one committed, one awaiting review carrying a supersession conflict plus a source-rule violation, one blocked with an upheld blocker finding. All quotes, offsets and hashes are internally consistent so highlighting lines up.

## Technical notes

- Add dependencies: `zustand`, `@tanstack/react-table`. Everything else is already installed.
- `src/api/` holds typed functions (`getCollections`, `getRegister`, `getRun`, `getRunEvents`, `getReviewItems`, `submitReviewDecisions`, `getFindings`, `getDocument`) returning the exact shapes in the brief; fixtures in `src/api/mock/`. `src/api/config.ts` reads `import.meta.env.VITE_API_BASE_URL` — when unset, mocks; when set, `fetch` against that base. Same signatures either way.
- Types in `src/api/types.ts`: `RegisterItem`, `Citation`, `ReviewItem`, `RunEvent`, `Finding`, plus `Collection`, `Run`, `DocumentDetail`.
- TanStack Query for all reads/mutations with query-option factories; route loaders call `ensureQueryData` and components use `useSuspenseQuery`. Client-only reads, no server functions.
- TanStack Table for Collections, Register (grouped + expanding), Runs, Findings.
- Zustand store keyed by run for review decisions and comments; cleared on submit.
- Shared primitives: `StateBadge`, `SeverityBadge`, `VerdictBadge`, `Hash`, `CitationBlock` (quote with highlighted grounding span), `Money` (renders `unpriced` for null), `Duration`.
- Per-route `head()` metadata with distinct Doctask titles/descriptions.

export type RegisterState = "supported" | "disputed" | "unsupported" | "missing";

export type RunStatus =
  | "running"
  | "awaiting_review"
  | "blocked"
  | "committed"
  | "stale"
  | "unconfirmed"
  | "failed";

export type RunTrigger = "manual" | "watcher" | "api" | "mcp";

export type ReviewItemKind =
  | "register_update"
  | "conflict"
  | "supersession_candidate"
  | "scope_question"
  | "finding"
  | "injection_review"
  | "deliverable_confirmation";

export type StageDecision =
  | "continue"
  | "retry"
  | "skip"
  | "escalate"
  | "abstain"
  | "branch";

export interface Citation {
  quote: string;
  documentId: string;
  filename: string;
  page: number | null;
  blockIndex: number;
  /** Offsets are relative to `quote`, which is the verbatim source block. */
  charStart: number;
  charEnd: number;
}

export interface RivalValue {
  value: unknown;
  citation: Citation;
  label: string;
}

export interface RegisterItem {
  registerKey: string;
  agreementId: string;
  key: string;
  value: unknown;
  state: RegisterState;
  contentHash: string;
  version: number;
  citations: Citation[];
  updatedAt: string;
  /** Present on disputed rows: unresolved rival values with their sources. */
  rivalValues?: RivalValue[];
}

export interface ReviewItem {
  id: string;
  runId: string;
  kind: ReviewItemKind;
  targetKey: string;
  before: unknown | null;
  after: unknown | null;
  citations: Citation[];
  severity?: string;
  rationale?: string;
  state: "pending" | "approved" | "rejected";
  basisHash: string;
  rulesetHash: string;
  /** finding cards only */
  ruleCode?: string;
  verdict?: FindingVerdict;
  /** scope_question cards only: candidate agreements to answer with */
  scopeOptions?: string[];
  /** injection_review cards only */
  detectionSignals?: string[];
}

export interface RunEvent {
  seq: number;
  stage: string;
  decision: StageDecision | null;
  reason: string | null;
  nextNode: string | null;
  attempt: number;
  errorClass: string | null;
  model: string | null;
  tokensIn: number;
  tokensOut: number;
  cacheHit: boolean;
  costUsd: number | null;
  durationMs: number;
  externalCalls: number;
}

export type FindingSeverity = "blocker" | "major" | "minor" | "info";
export type FindingVerdict = "violation" | "pass" | "insufficient_evidence";
export type FindingDecision = "pending" | "upheld" | "dismissed";

export interface Finding {
  ruleCode: string;
  severity: FindingSeverity;
  verdict: FindingVerdict;
  reviewDecision: FindingDecision;
  decidedBy: string | null;
  rationale: string;
  citations: Citation[];
  recheckRequired: boolean;
  target: string;
}

export interface Run {
  id: string;
  collectionId: string;
  trigger: RunTrigger;
  status: RunStatus;
  startedAt: string;
  durationMs: number;
  costUsd: number | null;
  currentStage: string | null;
  gateReason: string | null;
  priceTableVersion: string;
  pendingReviewCount: number;
}

export interface Collection {
  id: string;
  name: string;
  documentCount: number;
  registerRowCount: number;
  openRunCount: number;
  lastActivityAt: string;
  playbook: PlaybookRule[];
}

export interface PlaybookRule {
  ruleCode: string;
  title: string;
  severity: FindingSeverity;
  description: string;
  enabled: boolean;
}

export interface DocumentBlock {
  index: number;
  page: number | null;
  text: string;
  withheld: boolean;
  detectionSignals: string[];
}

export interface DocumentFact {
  key: string;
  value: unknown;
  blockIndex: number;
  charStart: number;
  charEnd: number;
}

export interface DocumentSummary {
  id: string;
  collectionId: string;
  filename: string;
  kind: "msa" | "amendment" | "invoice" | "policy";
  pages: number;
  contentHash: string;
  ingestedAt: string;
}

export interface DocumentDetail extends DocumentSummary {
  blocks: DocumentBlock[];
  facts: DocumentFact[];
  findings: Finding[];
}

export interface ReviewDecisionInput {
  itemId: string;
  decision: "approved" | "rejected" | "answered";
  comment?: string;
  answer?: string;
  overrideReason?: string;
}

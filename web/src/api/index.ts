import { API_BASE_URL, USE_MOCKS, delay, rest } from "./config";
import { documents } from "./mock/documents";
import {
  collections,
  documentSummaries,
  findings as mockFindings,
  register as mockRegister,
  reviewItems as mockReviewItems,
  runEvents as mockRunEvents,
  runs as mockRuns,
} from "./mock/fixtures";
import type {
  Collection,
  DocumentDetail,
  DocumentSummary,
  Finding,
  RegisterItem,
  ReviewDecisionInput,
  ReviewItem,
  Run,
  RunEvent,
} from "./types";

export * from "./types";
export { API_BASE_URL, USE_MOCKS };

export async function getCollections(): Promise<Collection[]> {
  if (!USE_MOCKS) return rest<Collection[]>("/collections");
  return delay(collections);
}

export async function getCollection(collectionId: string): Promise<Collection> {
  if (!USE_MOCKS) return rest<Collection>(`/collections/${collectionId}`);
  const found = collections.find((c) => c.id === collectionId);
  if (!found) throw new Error(`Collection ${collectionId} not found`);
  return delay(found);
}

export async function getRegister(collectionId: string): Promise<RegisterItem[]> {
  if (!USE_MOCKS) return rest<RegisterItem[]>(`/collections/${collectionId}/register`);
  return delay(collectionId === "col-acme" ? mockRegister : []);
}

export async function getDocuments(collectionId: string): Promise<DocumentSummary[]> {
  if (!USE_MOCKS) return rest<DocumentSummary[]>(`/collections/${collectionId}/documents`);
  return delay(documentSummaries.filter((d) => d.collectionId === collectionId));
}

export async function getDocument(documentId: string): Promise<DocumentDetail> {
  if (!USE_MOCKS) return rest<DocumentDetail>(`/documents/${documentId}`);
  const doc = documents[documentId];
  if (!doc) throw new Error(`Document ${documentId} not found`);
  const related = Object.values(mockFindings)
    .flat()
    .filter((f) => f.target.startsWith(documentId));
  return delay({ ...doc, findings: related });
}

export async function getRuns(collectionId: string): Promise<Run[]> {
  if (!USE_MOCKS) return rest<Run[]>(`/collections/${collectionId}/runs`);
  return delay(mockRuns.filter((r) => r.collectionId === collectionId));
}

export async function getRun(runId: string): Promise<Run> {
  if (!USE_MOCKS) return rest<Run>(`/runs/${runId}`);
  const run = mockRuns.find((r) => r.id === runId);
  if (!run) throw new Error(`Run ${runId} not found`);
  return delay(run);
}

export async function getRunEvents(runId: string): Promise<RunEvent[]> {
  if (!USE_MOCKS) return rest<RunEvent[]>(`/runs/${runId}/events`);
  return delay(mockRunEvents[runId] ?? []);
}

export async function getReviewItems(runId: string): Promise<ReviewItem[]> {
  if (!USE_MOCKS) return rest<ReviewItem[]>(`/runs/${runId}/review-items`);
  return delay(mockReviewItems[runId] ?? []);
}

export async function getFindings(runId: string): Promise<Finding[]> {
  if (!USE_MOCKS) return rest<Finding[]>(`/runs/${runId}/findings`);
  return delay(mockFindings[runId] ?? []);
}

export async function submitReviewDecisions(
  runId: string,
  decisions: ReviewDecisionInput[],
): Promise<{ runId: string; accepted: number }> {
  if (!USE_MOCKS) {
    return rest(`/runs/${runId}/review-items:submit`, {
      method: "POST",
      body: JSON.stringify({ decisions }),
    });
  }
  const items = mockReviewItems[runId] ?? [];
  for (const d of decisions) {
    const item = items.find((i) => i.id === d.itemId);
    if (item) item.state = d.decision === "rejected" ? "rejected" : "approved";
  }
  const run = mockRuns.find((r) => r.id === runId);
  if (run) {
    run.pendingReviewCount = items.filter((i) => i.state === "pending").length;
    if (run.pendingReviewCount === 0) {
      run.status = decisions.some((d) => d.decision === "rejected") ? "unconfirmed" : "committed";
      run.currentStage = null;
      run.gateReason = null;
    }
  }
  return delay({ runId, accepted: decisions.length }, 400);
}

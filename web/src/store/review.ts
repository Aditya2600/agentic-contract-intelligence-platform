import { create } from "zustand";

export type ItemDecision = "approved" | "rejected" | "answered";

export interface ItemState {
  decision: ItemDecision | null;
  comment: string;
  answer?: string;
  overrideReason?: string;
}

interface ReviewStore {
  /** runId -> itemId -> state */
  byRun: Record<string, Record<string, ItemState>>;
  get: (runId: string, itemId: string) => ItemState;
  setDecision: (runId: string, itemId: string, decision: ItemDecision | null) => void;
  setComment: (runId: string, itemId: string, comment: string) => void;
  setAnswer: (runId: string, itemId: string, answer: string) => void;
  setOverrideReason: (runId: string, itemId: string, reason: string) => void;
  clearRun: (runId: string) => void;
}

const EMPTY: ItemState = { decision: null, comment: "" };

function patch(
  byRun: ReviewStore["byRun"],
  runId: string,
  itemId: string,
  next: Partial<ItemState>,
) {
  const run = byRun[runId] ?? {};
  const prev = run[itemId] ?? EMPTY;
  return { ...byRun, [runId]: { ...run, [itemId]: { ...prev, ...next } } };
}

export const useReviewStore = create<ReviewStore>((set, get) => ({
  byRun: {},
  get: (runId, itemId) => get().byRun[runId]?.[itemId] ?? EMPTY,
  setDecision: (runId, itemId, decision) =>
    set((s) => ({ byRun: patch(s.byRun, runId, itemId, { decision }) })),
  setComment: (runId, itemId, comment) =>
    set((s) => ({ byRun: patch(s.byRun, runId, itemId, { comment }) })),
  setAnswer: (runId, itemId, answer) =>
    set((s) => ({ byRun: patch(s.byRun, runId, itemId, { answer, decision: "answered" }) })),
  setOverrideReason: (runId, itemId, overrideReason) =>
    set((s) => ({ byRun: patch(s.byRun, runId, itemId, { overrideReason }) })),
  clearRun: (runId) =>
    set((s) => {
      const next = { ...s.byRun };
      delete next[runId];
      return { byRun: next };
    }),
}));

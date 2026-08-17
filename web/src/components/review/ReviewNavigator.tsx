import type { ReviewItem } from "@/api/types";
import type { ItemDecision } from "@/store/review";
import { SectionLabel } from "@/components/doctask/primitives";
import { cn } from "@/lib/utils";

export const KIND_LABEL: Record<ReviewItem["kind"], string> = {
  register_update: "update",
  conflict: "conflict",
  supersession_candidate: "supersession",
  scope_question: "scope",
  finding: "finding",
  injection_review: "injection",
  deliverable_confirmation: "deliverable",
};

/** The agreement prefix is constant down the list; the suffix is what differs. */
function shortKey(key: string) {
  return key.includes("::") ? (key.split("::").pop() ?? key) : key;
}

const dotClass: Record<ItemDecision | "undecided", string> = {
  approved: "border-success bg-success",
  rejected: "border-danger bg-danger",
  answered: "border-info bg-info",
  undecided: "border-border-strong bg-transparent",
};

/** Compact left rail listing every decision this gate is waiting on. */
export function ReviewNavigator({
  items,
  decisions,
  currentId,
  onSelect,
}: {
  items: ReviewItem[];
  decisions: Record<string, ItemDecision | null>;
  currentId: string | null;
  onSelect: (id: string) => void;
}) {
  const undecided = items.filter((i) => !decisions[i.id]).length;

  return (
    <nav
      aria-label="Review navigator"
      className="hidden w-[248px] shrink-0 flex-col border-r border-border bg-card lg:flex"
    >
      <div className="flex items-baseline justify-between border-b border-border px-3 py-2">
        <SectionLabel>Decisions</SectionLabel>
        <span className="num font-mono text-[11px] text-muted-foreground">
          {items.length - undecided}/{items.length}
        </span>
      </div>
      <ul className="min-h-0 flex-1 overflow-y-auto">
        {items.map((item, i) => {
          const active = item.id === currentId;
          const decision = decisions[item.id] ?? "undecided";
          return (
            <li key={item.id}>
              <button
                type="button"
                onClick={() => onSelect(item.id)}
                aria-current={active ? "true" : undefined}
                className={cn(
                  "flex w-full items-center gap-2 border-b border-border/70 px-3 py-[7px] text-left transition-colors",
                  active ? "bg-accent" : "hover:bg-accent/60",
                )}
              >
                <span
                  aria-hidden
                  className={cn("size-[7px] shrink-0 rounded-full border", dotClass[decision])}
                />
                <span
                  title={item.targetKey}
                  className={cn(
                    "min-w-0 flex-1 truncate font-mono text-[11.5px]",
                    active ? "font-medium text-foreground" : "text-foreground/90",
                  )}
                >
                  {shortKey(item.targetKey)}
                </span>
                <span className="shrink-0 text-[10px] text-muted-foreground">
                  {KIND_LABEL[item.kind]}
                </span>
                <span className="num w-[14px] shrink-0 text-right font-mono text-[10px] text-muted-foreground/70">
                  {i + 1}
                </span>
              </button>
            </li>
          );
        })}
      </ul>
      <div className="border-t border-border px-3 py-2 text-[10.5px] leading-[1.5] text-muted-foreground">
        Decisions are held locally until you submit the review.
      </div>
    </nav>
  );
}

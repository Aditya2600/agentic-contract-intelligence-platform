import type { ReviewItem } from "@/api/types";
import { Pill, SeverityBadge, VerdictBadge } from "@/components/doctask/badges";
import { SectionLabel, ValueDisplay } from "@/components/doctask/primitives";
import { KIND_LABEL } from "@/components/review/ReviewNavigator";
import { cn } from "@/lib/utils";

const CHANGE_TYPE: Record<ReviewItem["kind"], string> = {
  register_update: "Register update",
  conflict: "Unresolved conflict",
  supersession_candidate: "Supersession",
  scope_question: "Scope question",
  finding: "Policy finding",
  injection_review: "Withheld content",
  deliverable_confirmation: "Deliverable confirmation",
};

/** Turns payment_due_days into "Payment due days" for the focused heading. */
function humanise(key: string) {
  const bare = key.includes("::") ? (key.split("::").pop() ?? key) : key;
  const words = bare.replace(/_/g, " ").trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** The single decision the reviewer is looking at right now. */
export function ProposedChange({
  item,
  answer,
  onAnswer,
  comment,
  onComment,
}: {
  item: ReviewItem;
  answer: string | undefined;
  onAnswer: (option: string) => void;
  comment: string;
  onComment: (value: string) => void;
}) {
  const showValues = item.kind !== "scope_question";

  return (
    <div className="min-w-0 flex-1 overflow-y-auto px-6 py-5">
      <div className="flex flex-wrap items-baseline gap-x-2.5 gap-y-1">
        <h2 className="text-[18px] font-semibold tracking-[-0.015em] text-foreground">
          {humanise(item.targetKey)}
        </h2>
        <span className="font-mono text-[11.5px] text-muted-foreground">{item.targetKey}</span>
        <Pill tone="neutral">{KIND_LABEL[item.kind]}</Pill>
        {item.severity ? (
          <SeverityBadge severity={item.severity as "blocker" | "major" | "minor" | "info"} />
        ) : null}
        {item.verdict ? <VerdictBadge verdict={item.verdict} /> : null}
      </div>

      {showValues ? (
        <div className="mt-5 max-w-[560px] divide-y divide-border border-y border-border">
          <div className="py-3">
            <SectionLabel>Current</SectionLabel>
            <ValueDisplay
              value={item.before}
              className="mt-1 block text-[15px] text-muted-foreground line-through"
            />
          </div>
          <div className="py-3">
            <SectionLabel>Proposed</SectionLabel>
            <ValueDisplay
              value={item.after}
              className="mt-1 block text-[25px] font-semibold leading-none tracking-[-0.015em] text-foreground"
            />
          </div>
        </div>
      ) : null}

      <div className="mt-4 max-w-[560px] space-y-3.5">
        <div>
          <SectionLabel>Change type</SectionLabel>
          <p className="mt-1 text-[12.5px] text-foreground">{CHANGE_TYPE[item.kind]}</p>
        </div>

        {item.ruleCode ? (
          <div>
            <SectionLabel>Rule</SectionLabel>
            <p className="mt-1 font-mono text-[12.5px] text-foreground">{item.ruleCode}</p>
          </div>
        ) : null}

        {item.rationale ? (
          <div>
            <SectionLabel>Rationale</SectionLabel>
            <p className="mt-1 text-[12.5px] leading-[1.6] text-foreground/90">{item.rationale}</p>
          </div>
        ) : null}

        {item.detectionSignals?.length ? (
          <div className="rounded-md border border-danger/25 bg-danger-surface px-2.5 py-2">
            <SectionLabel className="text-danger-foreground">Detection signals</SectionLabel>
            <ul className="mt-1 space-y-0.5">
              {item.detectionSignals.map((s) => (
                <li key={s} className="font-mono text-[11.5px] text-danger-foreground">
                  {s}
                </li>
              ))}
            </ul>
          </div>
        ) : null}

        {item.scopeOptions?.length ? (
          <div>
            <SectionLabel>Answer with agreement</SectionLabel>
            <div className="mt-1.5 flex flex-wrap gap-1.5">
              {item.scopeOptions.map((opt) => (
                <button
                  key={opt}
                  type="button"
                  onClick={() => onAnswer(opt)}
                  className={cn(
                    "rounded border px-2 py-1 font-mono text-[11.5px] transition-colors",
                    answer === opt
                      ? "border-primary bg-info-surface text-info-foreground"
                      : "border-border bg-card hover:bg-accent",
                  )}
                >
                  {opt}
                </button>
              ))}
            </div>
          </div>
        ) : null}

        <div className="border-t border-border pt-3.5">
          <SectionLabel>Reviewer note</SectionLabel>
          <textarea
            value={comment}
            onChange={(e) => onComment(e.target.value)}
            rows={2}
            placeholder="Optional. Recorded in the audit trail with your decision."
            className="mt-1.5 w-full resize-y rounded border border-input bg-card px-2 py-1.5 text-[12px] leading-[1.5] outline-none placeholder:text-muted-foreground focus-visible:ring-2 focus-visible:ring-ring"
          />
        </div>
      </div>
    </div>
  );
}

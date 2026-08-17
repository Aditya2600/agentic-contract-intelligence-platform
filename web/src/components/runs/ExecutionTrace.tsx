import { Check, CornerDownRight, GitBranch, Minus, RotateCcw, TriangleAlert } from "lucide-react";
import type { RunEvent, StageDecision } from "@/api/types";
import { Duration } from "@/components/doctask/primitives";
import { cn } from "@/lib/utils";

interface StageNode {
  stage: string;
  /** Every attempt in order; all but the last are retries. */
  attempts: RunEvent[];
  final: RunEvent;
  /** 1 when the previous stage branched directly into this one. */
  depth: 0 | 1;
}

/**
 * Flat run events become a shallow graph: consecutive events on one stage are
 * attempts of that stage, and a stage that a `branch` decision jumped into is
 * indented one level so the path change is visible rather than implied.
 */
function buildGraph(events: RunEvent[]): StageNode[] {
  const groups: { stage: string; attempts: RunEvent[] }[] = [];
  for (const event of events) {
    const last = groups[groups.length - 1];
    if (last && last.stage === event.stage) last.attempts.push(event);
    else groups.push({ stage: event.stage, attempts: [event] });
  }

  return groups.map((g, i) => {
    const previous = groups[i - 1];
    const prevFinal = previous?.attempts[previous.attempts.length - 1];
    const branchedIntoThis =
      prevFinal?.decision === "branch" && prevFinal.nextNode === g.stage;
    return {
      stage: g.stage,
      attempts: g.attempts,
      final: g.attempts[g.attempts.length - 1]!,
      depth: branchedIntoThis ? 1 : 0,
    };
  });
}

function isGate(node: StageNode) {
  return node.final.decision === "escalate";
}

function Marker({ node }: { node: StageNode }) {
  const decision = node.final.decision;

  if (isGate(node)) {
    return (
      <span
        aria-hidden
        className="grid size-[15px] shrink-0 rotate-45 place-items-center border border-warning bg-warning-surface"
      />
    );
  }
  if (decision === "branch") {
    return (
      <span
        aria-hidden
        className="grid size-[15px] shrink-0 place-items-center rounded-full border border-info/50 bg-info-surface text-info-foreground"
      >
        <GitBranch className="size-2.5" />
      </span>
    );
  }
  if (decision === "abstain") {
    return (
      <span
        aria-hidden
        className="grid size-[15px] shrink-0 place-items-center rounded-full border border-warning/50 bg-warning-surface text-warning-foreground"
      >
        <TriangleAlert className="size-2.5" />
      </span>
    );
  }
  if (decision === "skip") {
    return (
      <span
        aria-hidden
        className="grid size-[15px] shrink-0 place-items-center rounded-full border border-border bg-surface text-muted-foreground"
      >
        <Minus className="size-2.5" />
      </span>
    );
  }
  return (
    <span
      aria-hidden
      className="grid size-[15px] shrink-0 place-items-center rounded-full border border-success/40 bg-success-surface text-success-foreground"
    >
      <Check className="size-2.5" strokeWidth={3} />
    </span>
  );
}

const decisionLabel: Record<StageDecision, string> = {
  continue: "continue",
  retry: "retry",
  skip: "skipped",
  escalate: "escalated",
  abstain: "abstained",
  branch: "branch",
};

/** Vertical execution graph: one rail, indented branches, nested retries. */
export function ExecutionTrace({ events }: { events: RunEvent[] }) {
  const nodes = buildGraph(events);

  return (
    <ol className="relative">
      {nodes.map((node, i) => {
        const last = i === nodes.length - 1;
        const retries = node.attempts.slice(0, -1);
        const gate = isGate(node);
        const totalMs = node.attempts.reduce((s, a) => s + a.durationMs, 0);

        return (
          <li
            key={`${node.stage}-${i}`}
            className={cn("relative flex items-start gap-2.5", node.depth === 1 && "ml-[26px]")}
          >
            {/* Rail down to the next node */}
            {!last ? (
              <span
                aria-hidden
                className="absolute left-[7px] top-4 h-[calc(100%-14px)] w-px bg-border-strong"
              />
            ) : null}

            {/* Elbow connecting an indented branch back to its parent */}
            {node.depth === 1 ? (
              <>
                <span
                  aria-hidden
                  className="absolute -left-[19px] top-0 h-[8px] border-l border-info/50"
                />
                <span
                  aria-hidden
                  className="absolute -left-[19px] top-[8px] w-[17px] border-t border-info/50"
                />
              </>
            ) : null}

            <div className="pt-[3px]">
              <Marker node={node} />
            </div>

            <div className="min-w-0 flex-1 border-b border-border/60 pb-2.5">
              <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
                <span
                  className={cn(
                    "font-mono text-[12.5px] font-semibold",
                    gate ? "text-warning-foreground" : "text-foreground",
                  )}
                >
                  {gate ? `GATE · ${node.stage}` : node.stage}
                </span>
                {node.final.decision ? (
                  <span
                    className={cn(
                      "text-[11px]",
                      node.final.decision === "continue"
                        ? "text-muted-foreground"
                        : node.final.decision === "branch"
                          ? "text-info-foreground"
                          : "text-warning-foreground",
                    )}
                  >
                    {decisionLabel[node.final.decision]}
                  </span>
                ) : null}
                {retries.length > 0 ? (
                  <span className="inline-flex items-center gap-1 text-[11px] text-warning-foreground">
                    <RotateCcw className="size-3" aria-hidden />
                    {node.attempts.length} attempts
                  </span>
                ) : null}
                <span className="ml-auto shrink-0 pl-2">
                  <Duration ms={totalMs} className="text-[11px] text-muted-foreground" />
                </span>
              </div>

              <p className="mt-[3px] text-[11.5px] leading-[1.5] text-muted-foreground">
                {node.final.reason}
              </p>

              {/* Retries nest under their parent stage with an elbow. */}
              {retries.length > 0 ? (
                <ul className="mt-1.5 space-y-1">
                  {retries.map((attempt) => (
                    <li
                      key={attempt.seq}
                      className="flex items-start gap-1.5 rounded border-l-2 border-warning bg-warning-surface/40 py-1 pl-2 pr-2"
                    >
                      <CornerDownRight
                        className="mt-[2px] size-3 shrink-0 text-warning-foreground"
                        aria-hidden
                      />
                      <div className="min-w-0 flex-1">
                        <div className="flex flex-wrap items-baseline gap-x-2 text-[11px]">
                          <span className="font-mono font-medium text-warning-foreground">
                            attempt {attempt.attempt}
                          </span>
                          {attempt.errorClass ? (
                            <span className="font-mono text-danger-foreground">
                              {attempt.errorClass}
                            </span>
                          ) : null}
                          <span className="ml-auto">
                            <Duration
                              ms={attempt.durationMs}
                              className="text-[11px] text-muted-foreground"
                            />
                          </span>
                        </div>
                        <p className="mt-0.5 text-[11.5px] leading-[1.45] text-muted-foreground">
                          {attempt.reason}
                        </p>
                      </div>
                    </li>
                  ))}
                </ul>
              ) : null}

              {node.final.decision === "branch" && node.final.nextNode ? (
                <div className="mt-1.5 flex items-center gap-1.5 text-[11px] text-info-foreground">
                  <CornerDownRight className="size-3" aria-hidden />
                  path changed →{" "}
                  <span className="font-mono font-semibold">{node.final.nextNode}</span>
                </div>
              ) : null}

              {gate ? (
                <div className="mt-1.5 rounded border border-warning/30 bg-warning-surface px-2 py-1 text-[11.5px] text-warning-foreground">
                  Waiting for reviewer — no register write has occurred.
                </div>
              ) : null}
            </div>
          </li>
        );
      })}
    </ol>
  );
}

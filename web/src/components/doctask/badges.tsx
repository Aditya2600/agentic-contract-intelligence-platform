import { cn } from "@/lib/utils";
import type {
  FindingDecision,
  FindingSeverity,
  FindingVerdict,
  RegisterState,
  RunStatus,
  RunTrigger,
  StageDecision,
} from "@/api/types";

const base =
  "inline-flex items-center gap-1 rounded border px-1.5 py-[1px] text-[11px] font-medium leading-4 whitespace-nowrap";

type Tone = "neutral" | "success" | "warning" | "danger" | "info" | "outline";

const tones: Record<Tone, string> = {
  neutral: "border-border bg-neutral-surface text-muted-foreground",
  success: "border-success/25 bg-success-surface text-success-foreground",
  warning: "border-warning/30 bg-warning-surface text-warning-foreground",
  danger: "border-danger/25 bg-danger-surface text-danger-foreground",
  info: "border-info/20 bg-info-surface text-info-foreground",
  outline: "border-border-strong bg-transparent text-foreground",
};

export function Pill({
  tone = "neutral",
  className,
  children,
}: {
  tone?: Tone;
  className?: string;
  children: React.ReactNode;
}) {
  return <span className={cn(base, tones[tone], className)}>{children}</span>;
}

const registerTone: Record<RegisterState, Tone> = {
  supported: "success",
  disputed: "warning",
  unsupported: "danger",
  missing: "neutral",
};

export function StateBadge({ state }: { state: RegisterState }) {
  return <Pill tone={registerTone[state]}>{state}</Pill>;
}

const severityTone: Record<FindingSeverity, Tone> = {
  blocker: "danger",
  major: "warning",
  minor: "info",
  info: "neutral",
};

export function SeverityBadge({ severity }: { severity: FindingSeverity }) {
  return <Pill tone={severityTone[severity]}>{severity}</Pill>;
}

const verdictTone: Record<FindingVerdict, Tone> = {
  violation: "danger",
  pass: "success",
  insufficient_evidence: "warning",
};

export function VerdictBadge({ verdict }: { verdict: FindingVerdict }) {
  return <Pill tone={verdictTone[verdict]}>{verdict.replace(/_/g, " ")}</Pill>;
}

const decisionTone: Record<FindingDecision, Tone> = {
  pending: "neutral",
  upheld: "danger",
  dismissed: "outline",
};

export function ReviewDecisionBadge({ decision }: { decision: FindingDecision }) {
  return <Pill tone={decisionTone[decision]}>{decision}</Pill>;
}

const runTone: Record<RunStatus, Tone> = {
  running: "info",
  awaiting_review: "warning",
  blocked: "danger",
  committed: "success",
  stale: "neutral",
  unconfirmed: "warning",
  failed: "danger",
};

export function RunStatusBadge({ status }: { status: RunStatus }) {
  return (
    <Pill tone={runTone[status]}>
      {status === "running" ? (
        <span className="size-1.5 animate-pulse rounded-full bg-info" />
      ) : null}
      {status.replace(/_/g, " ")}
    </Pill>
  );
}

export function TriggerBadge({ trigger }: { trigger: RunTrigger }) {
  return (
    <span className="font-mono text-[11px] text-muted-foreground uppercase tracking-wide">
      {trigger}
    </span>
  );
}

const stageTone: Record<StageDecision, Tone> = {
  continue: "success",
  retry: "warning",
  skip: "outline",
  escalate: "danger",
  abstain: "warning",
  branch: "info",
};

export function StageDecisionBadge({ decision }: { decision: StageDecision }) {
  return <Pill tone={stageTone[decision]}>{decision}</Pill>;
}

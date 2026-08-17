import type { Finding, PlaybookRule } from "@/api/types";
import { RefreshCw } from "lucide-react";
import {
  ReviewDecisionBadge,
  SeverityBadge,
  VerdictBadge,
} from "@/components/doctask/badges";
import { EmptyState, SectionLabel } from "@/components/doctask/primitives";
import {
  InspectorEmpty,
  InspectorSection,
  MetaList,
  Row,
  TableFrame,
  Thead,
  td,
  th,
} from "@/components/doctask/surfaces";

const SEVERITY_ORDER = { blocker: 0, major: 1, minor: 2, info: 3 } as const;

/** A dismissed adverse finding is not settled — it has to be re-evaluated. */
function needsRecheck(f: Finding) {
  return f.recheckRequired || (f.reviewDecision === "dismissed" && f.verdict !== "pass");
}

export function FindingsTable({
  findings,
  selectedCode,
  onSelect,
}: {
  findings: Finding[];
  selectedCode: string | null;
  onSelect: (finding: Finding) => void;
}) {
  const sorted = [...findings].sort(
    (a, b) => SEVERITY_ORDER[a.severity] - SEVERITY_ORDER[b.severity],
  );

  if (sorted.length === 0) {
    return (
      <EmptyState
        title="No findings for this run"
        hint="Every enabled playbook rule evaluated without raising a finding."
      />
    );
  }

  return (
    <TableFrame>
      <Thead>
        <th className={th}>Rule</th>
        <th className={th}>Target</th>
        <th className={th}>Severity</th>
        <th className={th}>Verdict</th>
        <th className={th}>Review</th>
        <th className={th}>Reviewer</th>
      </Thead>
      <tbody>
        {sorted.map((f) => (
          <Row key={f.ruleCode} selected={selectedCode === f.ruleCode} onClick={() => onSelect(f)}>
            <td className={`${td} whitespace-nowrap font-mono font-medium text-foreground`}>
              {f.ruleCode}
            </td>
            <td className={`${td} font-mono text-[11.5px] text-muted-foreground`}>{f.target}</td>
            <td className={td}>
              <SeverityBadge severity={f.severity} />
            </td>
            <td className={td}>
              <VerdictBadge verdict={f.verdict} />
            </td>
            <td className={td}>
              <span className="flex flex-wrap items-center gap-1.5">
                <ReviewDecisionBadge decision={f.reviewDecision} />
                {needsRecheck(f) ? (
                  <span className="inline-flex items-center gap-1 text-[11px] font-medium text-warning-foreground">
                    <RefreshCw className="size-3" aria-hidden />
                    recheck required
                  </span>
                ) : null}
              </span>
            </td>
            <td className={`${td} text-[11.5px] text-muted-foreground`}>{f.decidedBy ?? "—"}</td>
          </Row>
        ))}
      </tbody>
    </TableFrame>
  );
}

export function FindingInspector({
  finding,
  rule,
}: {
  finding: Finding | null;
  rule: PlaybookRule | undefined;
}) {
  if (!finding) {
    return (
      <InspectorEmpty>
        Select a finding to read the rule it tested, the value it evaluated and its evidence.
      </InspectorEmpty>
    );
  }

  return (
    <div>
      <div className="border-b border-border px-3.5 py-3">
        <div className="font-mono text-[12px] font-semibold text-foreground">
          {finding.ruleCode}
        </div>
        <div className="mt-1 flex flex-wrap items-center gap-1.5">
          <SeverityBadge severity={finding.severity} />
          <VerdictBadge verdict={finding.verdict} />
          <ReviewDecisionBadge decision={finding.reviewDecision} />
        </div>
      </div>

      {needsRecheck(finding) ? (
        <div className="border-b border-warning/30 bg-warning-surface px-3.5 py-2 text-[11.5px] leading-[1.5] text-warning-foreground">
          <span className="font-semibold">Recheck required.</span>{" "}
          {finding.reviewDecision === "dismissed"
            ? "This adverse finding was dismissed by a reviewer, so it must be re-evaluated on the next run before commit."
            : "The evidence behind this finding changed since it was decided."}
        </div>
      ) : null}

      <InspectorSection label="Rule text">
        <p className="text-[12.5px] font-medium leading-[1.55] text-foreground">
          {rule?.title ?? "Rule text is not exposed for this finding."}
        </p>
        {rule ? (
          <p className="mt-1.5 text-[11.5px] leading-[1.55] text-muted-foreground">
            {rule.description}
          </p>
        ) : null}
      </InspectorSection>

      <InspectorSection label="Rationale">
        <p className="text-[12px] leading-[1.6] text-foreground/90">{finding.rationale}</p>
      </InspectorSection>

      <InspectorSection label="Evaluated">
        <MetaList
          rows={[
            { label: "Target", value: finding.target },
            { label: "Verdict", value: finding.verdict.replace(/_/g, " ") },
            {
              label: "Policy requirement",
              value: rule ? rule.severity : <span className="text-muted-foreground">—</span>,
            },
          ]}
        />
        {finding.verdict === "pass" ? (
          <p className="mt-2 text-[11.5px] leading-[1.5] text-muted-foreground">
            This rule was evaluated and passed. It stays on the record so an absent finding is never
            confused with an unevaluated one.
          </p>
        ) : null}
      </InspectorSection>

      <InspectorSection label={`Evidence (${finding.citations.length})`}>
        {finding.citations.length === 0 ? (
          <p className="text-[11.5px] leading-[1.5] text-muted-foreground">
            No quote satisfied this rule, so the verdict is insufficient evidence — not a violation.
          </p>
        ) : (
          <ul className="space-y-2">
            {finding.citations.map((c, i) => (
              <li key={i}>
                <div className="flex items-baseline justify-between gap-2">
                  <span className="truncate text-[11.5px] font-medium text-foreground">
                    {c.filename}
                  </span>
                  <span className="shrink-0 font-mono text-[10.5px] text-muted-foreground">
                    {c.page === null ? "no page" : `p.${c.page}`} · blk {c.blockIndex}
                  </span>
                </div>
                <p className="mt-1 rounded border border-border bg-surface px-2 py-1.5 font-mono text-[11px] leading-[1.55] text-foreground/90">
                  {c.quote.slice(0, c.charStart)}
                  <mark className="evidence-mark">{c.quote.slice(c.charStart, c.charEnd)}</mark>
                  {c.quote.slice(c.charEnd)}
                </p>
              </li>
            ))}
          </ul>
        )}
      </InspectorSection>

      <InspectorSection label="Review history">
        <MetaList
          rows={[
            { label: "Decision", value: finding.reviewDecision },
            {
              label: "Decided by",
              value: finding.decidedBy ?? <span className="text-muted-foreground">undecided</span>,
            },
            {
              label: "Recheck",
              value: needsRecheck(finding) ? "required" : "not required",
            },
          ]}
        />
        <p className="mt-2 text-[11px] leading-[1.5] text-muted-foreground">
          Timestamped decision history is not exposed by the findings API.
        </p>
      </InspectorSection>
    </div>
  );
}

export { SectionLabel };

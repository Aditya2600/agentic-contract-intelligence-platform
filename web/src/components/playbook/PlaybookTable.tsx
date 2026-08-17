import { useState } from "react";
import type { PlaybookRule } from "@/api/types";
import { Pill, SeverityBadge } from "@/components/doctask/badges";
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

/**
 * Playbook rules are configuration, not code: each row is an editable policy
 * that a run pins by hash before it evaluates anything.
 */
export function PlaybookTable({
  rules,
  scope,
  selectedCode,
  onSelect,
}: {
  rules: PlaybookRule[];
  scope: string;
  selectedCode: string | null;
  onSelect: (rule: PlaybookRule) => void;
}) {
  if (rules.length === 0) {
    return (
      <div className="rounded-md border border-dashed border-border py-10 text-center">
        <p className="text-[13px] font-medium text-foreground">No playbook configured</p>
        <p className="mt-1 text-[12px] text-muted-foreground">
          This collection inherits no policy rules, so runs raise no findings.
        </p>
      </div>
    );
  }

  return (
    <TableFrame>
      <Thead>
        <th className={th}>Rule</th>
        <th className={th}>Severity</th>
        <th className={th}>Scope</th>
        <th className={th}>Target keys</th>
        <th className={th}>Status</th>
      </Thead>
      <tbody>
        {rules.map((rule) => (
          <Row
            key={rule.ruleCode}
            selected={selectedCode === rule.ruleCode}
            onClick={() => onSelect(rule)}
          >
            <td className={td}>
              <div className="font-mono text-[12px] font-medium text-foreground">
                {rule.ruleCode}
              </div>
              <div className="text-[11.5px] text-muted-foreground">{rule.title}</div>
            </td>
            <td className={td}>
              <SeverityBadge severity={rule.severity} />
            </td>
            <td className={`${td} font-mono text-[11.5px] text-muted-foreground`}>{scope}</td>
            <td className={`${td} text-[11.5px] text-muted-foreground`}>
              <span className="italic">not exposed</span>
            </td>
            <td className={td}>
              <Pill tone={rule.enabled ? "success" : "neutral"}>
                {rule.enabled ? "enabled" : "disabled"}
              </Pill>
            </td>
          </Row>
        ))}
      </tbody>
    </TableFrame>
  );
}

export function PlaybookInspector({
  rule,
  scope,
  rulesetVersion,
}: {
  rule: PlaybookRule | null;
  scope: string;
  rulesetVersion: string;
}) {
  if (!rule) {
    return (
      <InspectorEmpty>
        Select a rule to read its policy text and how a run evaluates it.
      </InspectorEmpty>
    );
  }

  return (
    <div>
      <div className="border-b border-border px-3.5 py-3">
        <div className="font-mono text-[12px] font-semibold text-foreground">{rule.ruleCode}</div>
        <div className="mt-1 flex flex-wrap items-center gap-2">
          <SeverityBadge severity={rule.severity} />
          <Pill tone={rule.enabled ? "success" : "neutral"}>
            {rule.enabled ? "enabled" : "disabled"}
          </Pill>
        </div>
      </div>

      <InspectorSection label="Rule text">
        <p className="text-[12.5px] font-medium leading-[1.55] text-foreground">{rule.title}</p>
      </InspectorSection>

      <InspectorSection label="Evaluation behavior">
        <p className="text-[12px] leading-[1.6] text-muted-foreground">{rule.description}</p>
        <p className="mt-2 text-[11.5px] leading-[1.55] text-muted-foreground">
          {rule.enabled
            ? "Evaluated on every run against the pinned ruleset. A violation opens a finding and, at blocker severity, bars commit."
            : "Skipped at evaluation time. Existing findings from earlier runs remain on the record."}
        </p>
      </InspectorSection>

      <InspectorSection label="Configuration">
        <MetaList
          rows={[
            { label: "Scope", value: scope },
            { label: "Ruleset", value: rulesetVersion },
            // The playbook API returns no rule-level version or audit history.
            { label: "Rule version", value: <span className="text-muted-foreground">not exposed</span> },
            { label: "Target keys", value: <span className="text-muted-foreground">not exposed</span> },
          ]}
        />
      </InspectorSection>

      <InspectorSection label="History">
        <p className="text-[11.5px] leading-[1.55] text-muted-foreground">
          Per-rule change history is not exposed by the API. Runs pin the whole ruleset by hash, so
          a finding always names the ruleset version it was raised under.
        </p>
      </InspectorSection>
    </div>
  );
}

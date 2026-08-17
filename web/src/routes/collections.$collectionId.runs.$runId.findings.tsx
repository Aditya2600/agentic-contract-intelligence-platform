import { createFileRoute, Link } from "@tanstack/react-router";
import { useSuspenseQuery } from "@tanstack/react-query";
import { useState } from "react";
import type { Finding } from "@/api/types";
import { collectionQuery, findingsQuery, runQuery } from "@/api/queries";
import { AppShell, PageBodyFlush, PageHeader } from "@/components/layout/AppShell";
import { FindingInspector, FindingsTable } from "@/components/findings/FindingsTable";
import { RunStatusBadge } from "@/components/doctask/badges";
import { MetricStrip, SplitView } from "@/components/doctask/surfaces";

const TITLE = "Playbook findings — Doctask";
const DESCRIPTION =
  "Playbook rule results with severity, verdict and the exact quote each finding relies on.";

export const Route = createFileRoute("/collections/$collectionId/runs/$runId/findings")({
  head: () => ({
    meta: [
      { title: TITLE },
      { name: "description", content: DESCRIPTION },
      { property: "og:title", content: TITLE },
      { property: "og:description", content: DESCRIPTION },
    ],
  }),
  loader: async ({ context, params }) => {
    await context.queryClient.ensureQueryData(findingsQuery(params.runId));
  },
  component: FindingsPage,
});

function FindingsPage() {
  const { collectionId, runId } = Route.useParams();
  const { data: findings } = useSuspenseQuery(findingsQuery(runId));
  const { data: run } = useSuspenseQuery(runQuery(runId));
  const { data: collection } = useSuspenseQuery(collectionQuery(collectionId));

  const [selected, setSelected] = useState<Finding | null>(findings[0] ?? null);

  const blockers = findings.filter((f) => f.severity === "blocker").length;
  const passes = findings.filter((f) => f.verdict === "pass").length;
  const recheck = findings.filter(
    (f) => f.recheckRequired || (f.reviewDecision === "dismissed" && f.verdict !== "pass"),
  ).length;

  return (
    <AppShell>
      <PageHeader
        breadcrumb={
          <>
            <Link to="/collections" className="hover:text-foreground">
              Collections
            </Link>
            <span>/</span>
            <Link
              to="/collections/$collectionId"
              params={{ collectionId }}
              search={{ tab: "runs" }}
              className="hover:text-foreground"
            >
              {collection.name}
            </Link>
            <span>/</span>
            <Link
              to="/collections/$collectionId/runs/$runId"
              params={{ collectionId, runId }}
              className="font-mono hover:text-foreground"
            >
              {runId}
            </Link>
            <span>/</span>
            <span className="text-foreground">Findings</span>
          </>
        }
        title="Playbook findings"
        meta={
          <>
            <RunStatusBadge status={run.status} />
            <span className="font-mono">{runId}</span>
          </>
        }
      />

      <PageBodyFlush>
        <SplitView
          inspectorWidth="380px"
          main={
            <div className="space-y-2.5">
              <MetricStrip
                items={[
                  { label: "Findings", value: findings.length },
                  {
                    label: "Blockers",
                    value: blockers,
                    ...(blockers > 0 ? { tone: "danger" as const } : {}),
                  },
                  { label: "Passed", value: passes },
                  {
                    label: "Recheck required",
                    value: recheck,
                    ...(recheck > 0 ? { tone: "warning" as const } : {}),
                  },
                ]}
              />
              <FindingsTable
                findings={findings}
                selectedCode={selected?.ruleCode ?? null}
                onSelect={setSelected}
              />
            </div>
          }
          inspector={
            <FindingInspector
              finding={selected}
              rule={collection.playbook.find((r) => r.ruleCode === selected?.ruleCode)}
            />
          }
        />
      </PageBodyFlush>
    </AppShell>
  );
}

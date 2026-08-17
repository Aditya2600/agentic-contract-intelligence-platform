import { createFileRoute, Link } from "@tanstack/react-router";
import { useQueries, useSuspenseQuery } from "@tanstack/react-query";
import { FilePlus2, FolderPlus } from "lucide-react";
import { collectionsQuery, findingsQuery, runsQuery } from "@/api/queries";
import type { Finding, Run } from "@/api/types";
import { AppShell, PageBody, PageHeader } from "@/components/layout/AppShell";
import { RunStatusBadge } from "@/components/doctask/badges";
import { Timestamp } from "@/components/doctask/primitives";
import { MetricStrip, Row, TableFrame, Thead, td, th } from "@/components/doctask/surfaces";
import { Button } from "@/components/ui/button";

const TITLE = "Collections — Doctask";
const DESCRIPTION =
  "Vendor agreement workspaces and obligation registers, with open reviews and last run status per collection.";

/** Runs in these states still owe a reviewer a decision. */
const UNSETTLED: Run["status"][] = ["awaiting_review", "blocked", "unconfirmed", "failed"];

export const Route = createFileRoute("/collections/")({
  head: () => ({
    meta: [
      { title: TITLE },
      { name: "description", content: DESCRIPTION },
      { property: "og:title", content: TITLE },
      { property: "og:description", content: DESCRIPTION },
    ],
  }),
  loader: async ({ context }) => {
    await context.queryClient.ensureQueryData(collectionsQuery());
  },
  component: CollectionsPage,
});

function CollectionsPage() {
  const { data: collections } = useSuspenseQuery(collectionsQuery());

  // Runs are only exposed per collection, so the index fans out one read each.
  const runResults = useQueries({
    queries: collections.map((c) => runsQuery(c.id)),
  });
  const runsByCollection = new Map<string, Run[]>(
    collections.map((c, i) => [c.id, runResults[i]?.data ?? []]),
  );
  const allRuns = runResults.flatMap((r) => r.data ?? []);

  // Findings are per run, so only unsettled runs are queried rather than all.
  const unsettledRuns = allRuns.filter((r) => UNSETTLED.includes(r.status));
  const findingResults = useQueries({
    queries: unsettledRuns.map((r) => findingsQuery(r.id)),
  });
  const unresolvedFindings = findingResults
    .flatMap((r) => (r.data ?? []) as Finding[])
    .filter((f) => f.reviewDecision === "pending" && f.verdict !== "pass").length;

  const openReviews = allRuns.reduce((sum, r) => sum + r.pendingReviewCount, 0);
  const documents = collections.reduce((sum, c) => sum + c.documentCount, 0);

  return (
    <AppShell>
      <PageHeader
        breadcrumb={<span className="text-foreground">Collections</span>}
        title="Collections"
        meta={<span>Vendor agreement workspaces and obligation registers</span>}
        actions={
          <>
            <Button size="sm" variant="outline" className="h-7 text-[12px]">
              <FolderPlus className="mr-1 size-3.5" aria-hidden />
              New collection
            </Button>
            <Button size="sm" className="h-7 text-[12px]">
              <FilePlus2 className="mr-1 size-3.5" aria-hidden />
              Import document
            </Button>
          </>
        }
      />

      <PageBody className="space-y-3">
        <MetricStrip
          items={[
            { label: "Collections", value: collections.length },
            {
              label: "Open reviews",
              value: openReviews,
              ...(openReviews > 0 ? { tone: "warning" as const } : {}),
            },
            { label: "Documents", value: documents },
            {
              label: "Unresolved findings",
              value: unresolvedFindings,
              ...(unresolvedFindings > 0 ? { tone: "danger" as const } : {}),
            },
          ]}
        />

        <TableFrame>
          <Thead>
            <th className={th}>Collection</th>
            <th className={`${th} text-right`}>Documents</th>
            <th className={`${th} text-right`}>Register rows</th>
            <th className={`${th} text-right`}>Open review</th>
            <th className={th}>Last run</th>
            <th className={th}>Last activity</th>
          </Thead>
          <tbody>
            {collections.map((c) => {
              const runs = runsByCollection.get(c.id) ?? [];
              const latest = [...runs].sort(
                (a, b) => Date.parse(b.startedAt) - Date.parse(a.startedAt),
              )[0];
              const pending = runs.reduce((sum, r) => sum + r.pendingReviewCount, 0);

              return (
                <Row key={c.id}>
                  <td className={td}>
                    <Link
                      to="/collections/$collectionId"
                      params={{ collectionId: c.id }}
                      search={{ tab: "register" }}
                      className="font-medium text-foreground hover:text-primary hover:underline"
                    >
                      {c.name}
                    </Link>
                    <div className="font-mono text-[10.5px] text-muted-foreground">{c.id}</div>
                  </td>
                  <td className={`${td} num text-right font-mono`}>{c.documentCount}</td>
                  <td className={`${td} num text-right font-mono`}>{c.registerRowCount}</td>
                  <td className={`${td} num text-right font-mono`}>
                    {pending === 0 ? (
                      <span className="text-muted-foreground">—</span>
                    ) : latest ? (
                      <Link
                        to="/collections/$collectionId/runs/$runId/review"
                        params={{ collectionId: c.id, runId: latest.id }}
                        className="text-warning-foreground hover:underline"
                      >
                        {pending}
                      </Link>
                    ) : (
                      <span className="text-warning-foreground">{pending}</span>
                    )}
                  </td>
                  <td className={td}>
                    {latest ? (
                      <span className="flex items-center gap-2">
                        <Link
                          to="/collections/$collectionId/runs/$runId"
                          params={{ collectionId: c.id, runId: latest.id }}
                          className="font-mono text-[11.5px] text-muted-foreground hover:text-foreground hover:underline"
                        >
                          {latest.id}
                        </Link>
                        <RunStatusBadge status={latest.status} />
                      </span>
                    ) : (
                      <span className="text-[11.5px] text-muted-foreground">no runs</span>
                    )}
                  </td>
                  <td className={td}>
                    <Timestamp value={c.lastActivityAt} />
                  </td>
                </Row>
              );
            })}
          </tbody>
        </TableFrame>
      </PageBody>
    </AppShell>
  );
}

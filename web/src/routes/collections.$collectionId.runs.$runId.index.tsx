import { createFileRoute, Link } from "@tanstack/react-router";
import { useSuspenseQuery } from "@tanstack/react-query";
import { ClipboardCheck, ShieldAlert } from "lucide-react";
import {
  collectionQuery,
  documentsQuery,
  reviewItemsQuery,
  runEventsQuery,
  runQuery,
} from "@/api/queries";
import { AppShell, PageBodyFlush, PageHeader } from "@/components/layout/AppShell";
import { ExecutionTrace } from "@/components/runs/ExecutionTrace";
import { RunInspector } from "@/components/runs/RunInspector";
import { RunStatusBadge, TriggerBadge } from "@/components/doctask/badges";
import { Hash, SectionLabel, Timestamp } from "@/components/doctask/primitives";
import { SplitView } from "@/components/doctask/surfaces";
import { Button } from "@/components/ui/button";

const TITLE = "Run detail — Doctask";
const DESCRIPTION =
  "Stage-by-stage agent trace: decisions, retries, branches, escalations, compute and spend.";

export const Route = createFileRoute("/collections/$collectionId/runs/$runId/")({
  head: () => ({
    meta: [
      { title: TITLE },
      { name: "description", content: DESCRIPTION },
      { property: "og:title", content: TITLE },
      { property: "og:description", content: DESCRIPTION },
    ],
  }),
  loader: async ({ context, params }) => {
    await Promise.all([
      context.queryClient.ensureQueryData(runEventsQuery(params.runId)),
      context.queryClient.ensureQueryData(reviewItemsQuery(params.runId)),
      context.queryClient.ensureQueryData(documentsQuery(params.collectionId)),
    ]);
  },
  component: RunDetailPage,
});

function RunDetailPage() {
  const { collectionId, runId } = Route.useParams();
  const { data: run } = useSuspenseQuery(runQuery(runId));
  const { data: events } = useSuspenseQuery(runEventsQuery(runId));
  const { data: collection } = useSuspenseQuery(collectionQuery(collectionId));
  const { data: documents } = useSuspenseQuery(documentsQuery(collectionId));
  const { data: reviewItems } = useSuspenseQuery(reviewItemsQuery(runId));

  const retries = events.filter((e) => e.decision === "retry").length;
  const gates = events.filter((e) => e.decision === "escalate").length;
  // The run payload carries no ruleset field; review items record the hash the run pinned.
  const rulesetHash = reviewItems[0]?.rulesetHash ?? null;

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
            <span className="font-mono text-foreground">{run.id}</span>
          </>
        }
        title={<span className="font-mono">{run.id}</span>}
        meta={
          <>
            <RunStatusBadge status={run.status} />
            <span>
              trigger <TriggerBadge trigger={run.trigger} />
            </span>
            {rulesetHash ? (
              <span className="flex items-center gap-1">
                ruleset <Hash value={rulesetHash} chars={8} />
              </span>
            ) : null}
            <span>
              <span className="num font-mono text-foreground">{documents.length}</span> documents
            </span>
            <Timestamp value={run.startedAt} />
          </>
        }
        actions={
          <>
            <Button asChild size="sm" variant="outline" className="h-7 text-[12px]">
              <Link
                to="/collections/$collectionId/runs/$runId/findings"
                params={{ collectionId, runId }}
              >
                <ShieldAlert className="mr-1 size-3.5" aria-hidden />
                Findings
              </Link>
            </Button>
            <Button asChild size="sm" className="h-7 text-[12px]">
              <Link
                to="/collections/$collectionId/runs/$runId/review"
                params={{ collectionId, runId }}
              >
                <ClipboardCheck className="mr-1 size-3.5" aria-hidden />
                Review queue
                {run.pendingReviewCount > 0 ? (
                  <span className="ml-1.5 rounded bg-primary-foreground/20 px-1 font-mono text-[11px]">
                    {run.pendingReviewCount}
                  </span>
                ) : null}
              </Link>
            </Button>
          </>
        }
        banner={
          run.gateReason ? (
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 border-t border-warning/30 bg-warning-surface px-5 py-2">
              <span className="size-1.5 shrink-0 rounded-full bg-warning" aria-hidden />
              <p className="min-w-0 flex-1 text-[12px] leading-[1.5] text-foreground">
                <span className="font-semibold text-warning-foreground">
                  Human review required
                </span>{" "}
                <span className="text-muted-foreground">— {run.gateReason}</span>
              </p>
              {run.pendingReviewCount > 0 ? (
                <Button
                  asChild
                  size="sm"
                  variant="outline"
                  className="h-6 border-warning/50 bg-card text-[11.5px] text-warning-foreground hover:bg-warning-surface"
                >
                  <Link
                    to="/collections/$collectionId/runs/$runId/review"
                    params={{ collectionId, runId }}
                  >
                    Review {run.pendingReviewCount} decisions
                  </Link>
                </Button>
              ) : null}
            </div>
          ) : null
        }
      />

      <PageBodyFlush>
        <SplitView
          inspectorWidth="340px"
          main={
            <>
              <div className="mb-3 flex items-baseline justify-between gap-3">
                <SectionLabel>Execution trace</SectionLabel>
                <span className="font-mono text-[11px] text-muted-foreground">
                  {events.length} stages · {retries} retr{retries === 1 ? "y" : "ies"} · {gates}{" "}
                  gate{gates === 1 ? "" : "s"}
                </span>
              </div>
              <ExecutionTrace events={events} />
            </>
          }
          inspector={
            <RunInspector
              run={run}
              events={events}
              documents={documents}
              reviewItems={reviewItems}
            />
          }
        />
      </PageBodyFlush>
    </AppShell>
  );
}

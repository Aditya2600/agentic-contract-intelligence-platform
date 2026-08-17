import { createFileRoute, Link, useRouter } from "@tanstack/react-router";
import { useMutation, useQueryClient, useSuspenseQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { Check, CheckCheck, Loader2, ShieldAlert, X } from "lucide-react";
import { submitReviewDecisions } from "@/api/index";
import { collectionQuery, documentsQuery, reviewItemsQuery, runQuery } from "@/api/queries";
import type { ReviewDecisionInput } from "@/api/types";
import { AppShell, PageHeader } from "@/components/layout/AppShell";
import { ProposedChange } from "@/components/review/ProposedChange";
import { ReviewNavigator } from "@/components/review/ReviewNavigator";
import { SourceEvidencePanel } from "@/components/review/SourceEvidencePanel";
import { EmptyState } from "@/components/doctask/primitives";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/textarea";
import { useReviewStore, type ItemDecision } from "@/store/review";
import { cn } from "@/lib/utils";

const TITLE = "Review queue — Doctask";
const DESCRIPTION =
  "Approve or reject each proposed register change against its source quote. Nothing commits without a human decision.";

const overrideSchema = z.object({
  overrideReason: z.string().trim().min(20, "Give at least 20 characters of written justification."),
});
type OverrideForm = z.infer<typeof overrideSchema>;

export const Route = createFileRoute("/collections/$collectionId/runs/$runId/review")({
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
      context.queryClient.ensureQueryData(reviewItemsQuery(params.runId)),
      context.queryClient.ensureQueryData(documentsQuery(params.collectionId)),
    ]);
  },
  component: ReviewQueuePage,
});

function ReviewQueuePage() {
  const { collectionId, runId } = Route.useParams();
  const { data: items } = useSuspenseQuery(reviewItemsQuery(runId));
  const { data: run } = useSuspenseQuery(runQuery(runId));
  const { data: documents } = useSuspenseQuery(documentsQuery(collectionId));
  const { data: collection } = useSuspenseQuery(collectionQuery(collectionId));

  const byRun = useReviewStore((s) => s.byRun[runId]);
  const setDecision = useReviewStore((s) => s.setDecision);
  const setAnswer = useReviewStore((s) => s.setAnswer);
  const setComment = useReviewStore((s) => s.setComment);
  const setOverrideReason = useReviewStore((s) => s.setOverrideReason);
  const clearRun = useReviewStore((s) => s.clearRun);

  const queryClient = useQueryClient();
  const router = useRouter();
  const [currentId, setCurrentId] = useState<string | null>(items[0]?.id ?? null);
  const [overrideOpen, setOverrideOpen] = useState(false);
  const [committed, setCommitted] = useState<number | null>(null);

  const current = items.find((i) => i.id === currentId) ?? items[0] ?? null;

  const decisions = useMemo(() => {
    const map: Record<string, ItemDecision | null> = {};
    for (const item of items) map[item.id] = byRun?.[item.id]?.decision ?? null;
    return map;
  }, [items, byRun]);

  const decided = items.filter((i) => decisions[i.id]);
  const approved = items.filter((i) => decisions[i.id] === "approved").length;
  const rejected = items.filter((i) => decisions[i.id] === "rejected").length;
  const answered = items.filter((i) => decisions[i.id] === "answered").length;
  const undecided = items.length - decided.length;

  const currentState = current ? byRun?.[current.id] : undefined;
  const currentDecision = current ? decisions[current.id] : null;
  const ungrounded = current ? current.citations.length === 0 : false;
  const isBlocker = current?.severity === "blocker";
  // Approving with no quote, or clearing a blocker, is never a single click.
  const requiresOverride = ungrounded || isBlocker;

  const form = useForm<OverrideForm>({
    resolver: zodResolver(overrideSchema),
    defaultValues: { overrideReason: "" },
  });

  const mutation = useMutation({
    mutationFn: (input: ReviewDecisionInput[]) => submitReviewDecisions(runId, input),
    onSuccess: (result) => {
      setCommitted(result.accepted);
      clearRun(runId);
      queryClient.invalidateQueries({ queryKey: ["review-items", runId] });
      queryClient.invalidateQueries({ queryKey: ["run", runId] });
      queryClient.invalidateQueries({ queryKey: ["runs", collectionId] });
      queryClient.invalidateQueries({ queryKey: ["register", collectionId] });
      router.invalidate();
    },
  });

  const submit = () => {
    const payload: ReviewDecisionInput[] = decided.map((item) => {
      const state = byRun?.[item.id];
      return {
        itemId: item.id,
        decision: state!.decision!,
        ...(state?.comment ? { comment: state.comment } : {}),
        ...(state?.answer ? { answer: state.answer } : {}),
        ...(state?.overrideReason ? { overrideReason: state.overrideReason } : {}),
      };
    });
    mutation.mutate(payload);
  };

  const submitOverride = form.handleSubmit((values) => {
    if (!current) return;
    setOverrideReason(runId, current.id, values.overrideReason);
    setDecision(runId, current.id, "approved");
    setOverrideOpen(false);
    form.reset({ overrideReason: "" });
  });

  const breadcrumb = (
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
      <span className="text-foreground">Review</span>
    </>
  );

  if (items.length === 0) {
    return (
      <AppShell>
        <PageHeader breadcrumb={breadcrumb} title="Review queue" />
        <main className="flex-1 px-5 py-4">
          <EmptyState
            title="Nothing awaiting review"
            hint="This run produced no proposed register changes, conflicts or findings that need a human decision."
          />
        </main>
      </AppShell>
    );
  }

  return (
    <AppShell>
      <PageHeader
        breadcrumb={breadcrumb}
        actions={
          <>
            <span className="font-mono text-[11px] text-muted-foreground">
              {run.status.replace(/_/g, " ")}
            </span>
            <Button
              size="sm"
              className="h-7 text-[12px]"
              disabled={decided.length === 0 || mutation.isPending}
              onClick={submit}
            >
              {mutation.isPending ? (
                <Loader2 className="mr-1 size-3.5 animate-spin" aria-hidden />
              ) : (
                <CheckCheck className="mr-1 size-3.5" aria-hidden />
              )}
              Submit review
            </Button>
          </>
        }
        banner={
          committed !== null ? (
            <div className="border-t border-success/30 bg-success-surface px-5 py-2 text-[12px] text-success-foreground">
              <span className="font-semibold">{committed} decisions submitted.</span> Register
              versions were incremented only for approved, grounded values.
            </div>
          ) : null
        }
      />

      <div className="flex min-h-0 flex-1">
        <ReviewNavigator
          items={items}
          decisions={decisions}
          currentId={current?.id ?? null}
          onSelect={setCurrentId}
        />

        <div className="flex min-w-0 flex-1 flex-col">
          <div className="flex min-h-0 flex-1 flex-col xl:flex-row">
            {current ? (
              <>
                <ProposedChange
                  item={current}
                  answer={currentState?.answer}
                  onAnswer={(opt) => setAnswer(runId, current.id, opt)}
                  comment={currentState?.comment ?? ""}
                  onComment={(value) => setComment(runId, current.id, value)}
                />
                <SourceEvidencePanel citations={current.citations} documents={documents} />
              </>
            ) : null}
          </div>

          {/* Decisions are held locally; this bar is the only path to the API. */}
          <div className="sticky bottom-0 flex flex-wrap items-center gap-x-3 gap-y-2 border-t border-border bg-background/95 px-4 py-2 backdrop-blur">
            <div className="flex items-center gap-2 text-[11.5px] text-muted-foreground">
              <span className="num font-mono text-success-foreground">{approved}</span> approved
              <span className="text-border-strong">·</span>
              <span className="num font-mono text-danger-foreground">{rejected}</span> rejected
              {answered > 0 ? (
                <>
                  <span className="text-border-strong">·</span>
                  <span className="num font-mono text-info-foreground">{answered}</span> answered
                </>
              ) : null}
              <span className="text-border-strong">·</span>
              <span className="num font-mono text-foreground">{undecided}</span> undecided
            </div>

            <div className="ml-auto flex flex-wrap items-center gap-2">
              {current ? (
                <>
                  <Button
                    size="sm"
                    variant="outline"
                    className={cn(
                      "h-7 text-[12px]",
                      currentDecision === "rejected" &&
                        "border-danger bg-danger-surface text-danger-foreground",
                    )}
                    onClick={() =>
                      setDecision(runId, current.id, currentDecision === "rejected" ? null : "rejected")
                    }
                  >
                    <X className="mr-1 size-3.5" aria-hidden />
                    Reject
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={requiresOverride}
                    title={
                      requiresOverride
                        ? "This item can only be approved through a written override."
                        : undefined
                    }
                    className={cn(
                      "h-7 text-[12px]",
                      currentDecision === "approved" &&
                        "border-success bg-success-surface text-success-foreground",
                    )}
                    onClick={() =>
                      setDecision(runId, current.id, currentDecision === "approved" ? null : "approved")
                    }
                  >
                    <Check className="mr-1 size-3.5" aria-hidden />
                    Approve
                  </Button>

                  {/* Overriding is a separate, deliberate act with a typed reason. */}
                  {requiresOverride ? (
                    <>
                      <span className="mx-1 h-5 w-px bg-border-strong" aria-hidden />
                      <Button
                        size="sm"
                        variant="outline"
                        className="h-7 border-warning/50 text-[12px] text-warning-foreground hover:bg-warning-surface"
                        onClick={() => setOverrideOpen(true)}
                      >
                        <ShieldAlert className="mr-1 size-3.5" aria-hidden />
                        Blocker override
                      </Button>
                    </>
                  ) : null}
                </>
              ) : null}
            </div>

            {currentState?.overrideReason ? (
              <span className="w-full font-mono text-[10.5px] text-warning-foreground">
                override recorded for {current?.targetKey}
              </span>
            ) : null}
          </div>
        </div>
      </div>

      <Dialog open={overrideOpen} onOpenChange={setOverrideOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle className="text-[15px]">
              {ungrounded ? "Approve without grounding" : "Override a blocker"}
            </DialogTitle>
            <DialogDescription className="text-[12.5px]">
              {ungrounded
                ? `${current?.targetKey} has no supporting quote.`
                : `${current?.targetKey} is a blocker-severity item that bars commit.`}{" "}
              Your justification is stored with the register version and is visible to auditors.
            </DialogDescription>
          </DialogHeader>
          <form onSubmit={submitOverride} className="space-y-2">
            <Textarea
              rows={4}
              placeholder={
                ungrounded
                  ? "Why is this value correct despite the missing citation?"
                  : "Why is it safe to clear this blocker?"
              }
              className="text-[12.5px]"
              {...form.register("overrideReason")}
            />
            {form.formState.errors.overrideReason ? (
              <p className="text-[11.5px] text-danger-foreground">
                {form.formState.errors.overrideReason.message}
              </p>
            ) : null}
            <DialogFooter>
              <Button type="button" variant="ghost" size="sm" onClick={() => setOverrideOpen(false)}>
                Cancel
              </Button>
              <Button type="submit" size="sm">
                Record override
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </AppShell>
  );
}

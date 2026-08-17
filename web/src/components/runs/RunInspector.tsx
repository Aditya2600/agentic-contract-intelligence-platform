import type { DocumentSummary, ReviewItem, Run, RunEvent } from "@/api/types";
import { RunStatusBadge, TriggerBadge } from "@/components/doctask/badges";
import { Duration, Money, Timestamp } from "@/components/doctask/primitives";
import { InspectorSection, MetaList } from "@/components/doctask/surfaces";

/** Kinds that would write to the register once approved. */
const WRITE_KINDS: ReviewItem["kind"][] = [
  "register_update",
  "supersession_candidate",
  "conflict",
];

export function RunInspector({
  run,
  events,
  documents,
  reviewItems,
}: {
  run: Run;
  events: RunEvent[];
  documents: DocumentSummary[];
  reviewItems: ReviewItem[];
}) {
  const totalMs = events.reduce((s, e) => s + e.durationMs, 0);
  const modelMs = events.filter((e) => e.model).reduce((s, e) => s + e.durationMs, 0);
  const priced = events.filter((e) => e.costUsd !== null);
  // Prefer the run's own total; fall back to summing the stages that are priced.
  const spend =
    run.costUsd ?? (priced.length ? priced.reduce((s, e) => s + (e.costUsd ?? 0), 0) : null);
  const tokensIn = events.reduce((s, e) => s + e.tokensIn, 0);
  const tokensOut = events.reduce((s, e) => s + e.tokensOut, 0);
  const cacheHits = events.filter((e) => e.cacheHit).length;
  const externalCalls = events.reduce((s, e) => s + e.externalCalls, 0);
  // Only model-bearing stages can legitimately carry a price.
  const unpricedStages = events.filter((e) => e.costUsd === null).length;

  const proposedWrites = reviewItems.filter((i) => WRITE_KINDS.includes(i.kind)).length;
  const findingCards = reviewItems.filter((i) => i.kind === "finding").length;
  const pending = reviewItems.filter((i) => i.state === "pending").length;
  const committed = run.status === "committed";

  return (
    <div>
      <InspectorSection label="Run status">
        <div className="mb-2 flex flex-wrap items-center gap-2">
          <RunStatusBadge status={run.status} />
          <TriggerBadge trigger={run.trigger} />
        </div>
        <MetaList
          rows={[
            { label: "Run", value: run.id },
            { label: "Started", value: <Timestamp value={run.startedAt} /> },
            { label: "Wall clock", value: <Duration ms={run.durationMs} /> },
            { label: "Stages", value: events.length },
            {
              label: "Current stage",
              value: run.currentStage ?? <span className="text-muted-foreground">—</span>,
            },
          ]}
        />
      </InspectorSection>

      <InspectorSection label="Document">
        <MetaList
          rows={[
            { label: "In scope", value: `${documents.length} documents` },
            ...documents.slice(0, 4).map((d) => ({
              label: d.kind,
              value: <span className="truncate">{d.filename}</span>,
            })),
          ]}
        />
        <p className="mt-2 text-[11px] leading-[1.5] text-muted-foreground">
          The run API does not name which document triggered this run, so the whole collection is
          shown.
        </p>
      </InspectorSection>

      <InspectorSection label="Register impact">
        <MetaList
          rows={[
            { label: "Proposed writes", value: proposedWrites },
            { label: "Finding cards", value: findingCards },
            { label: "Pending decisions", value: pending },
          ]}
        />
        <p className="mt-2 text-[11px] leading-[1.5] text-muted-foreground">
          {committed
            ? "Approved values were written and their register versions incremented."
            : "Nothing is written to the register until every gate on this run is cleared."}
        </p>
      </InspectorSection>

      <InspectorSection label="Cost & latency">
        <MetaList
          rows={[
            { label: "Runtime", value: <Duration ms={totalMs} /> },
            { label: "Model time", value: <Duration ms={modelMs} /> },
            {
              label: "Spend",
              // An unpriced stage makes any total a floor, never a settled figure.
              value:
                spend !== null && unpricedStages > 0 ? (
                  <span className="text-muted-foreground">
                    ≥ <Money value={spend} className="text-foreground" />
                  </span>
                ) : (
                  <Money value={spend} />
                ),
            },
            { label: "Tokens", value: (tokensIn + tokensOut).toLocaleString() },
            { label: "Cache hits", value: `${cacheHits} / ${events.length}` },
            { label: "External calls", value: externalCalls },
            { label: "Price table", value: run.priceTableVersion },
          ]}
        />
        {unpricedStages > 0 ? (
          <p className="mt-2 text-[11px] leading-[1.5] text-muted-foreground">
            <span className="font-mono uppercase">{unpricedStages} unpriced</span> stage
            {unpricedStages === 1 ? "" : "s"} — no price-table entry, so spend is a floor, not a
            total.
          </p>
        ) : null}
      </InspectorSection>
    </div>
  );
}

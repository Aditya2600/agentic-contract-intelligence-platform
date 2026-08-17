import { GitCompareArrows } from "lucide-react";
import type { Citation, DocumentSummary, RegisterItem } from "@/api/types";
import { StateBadge } from "@/components/doctask/badges";
import { Hash, SectionLabel, Timestamp, ValueDisplay } from "@/components/doctask/primitives";
import { InspectorEmpty, InspectorSection, MetaList } from "@/components/doctask/surfaces";

/** Verbatim quote with the exact grounding span marked. */
function GroundedQuote({ citation }: { citation: Citation }) {
  const { quote, charStart, charEnd } = citation;
  return (
    <p className="rounded border border-border bg-surface px-2.5 py-2 font-mono text-[11.5px] leading-[1.65] text-foreground/90">
      {quote.slice(0, charStart)}
      <mark className="evidence-mark">{quote.slice(charStart, charEnd)}</mark>
      {quote.slice(charEnd)}
    </p>
  );
}

function SourceLine({ citation }: { citation: Citation }) {
  return (
    <div className="font-mono text-[11px] text-muted-foreground">
      {citation.page === null ? "no page" : `Page ${citation.page}`} · Block {citation.blockIndex} ·
      chars {citation.charStart}–{citation.charEnd}
    </div>
  );
}

/**
 * Persistent right-hand inspector for the obligations register. Shows where a
 * value came from and, for disputed rows, why neither rival wins automatically.
 */
export function EvidenceInspector({
  item,
  documents,
}: {
  item: RegisterItem | null;
  documents: DocumentSummary[];
}) {
  if (!item) {
    return (
      <InspectorEmpty>
        Select an obligation to see its source quote, grounding offsets and provenance.
      </InspectorEmpty>
    );
  }

  const primary = item.citations[0] ?? null;
  const docHash = primary
    ? (documents.find((d) => d.id === primary.documentId)?.contentHash ?? null)
    : null;

  const rivals = (item.rivalValues ?? []).map((rival) => {
    const kind = documents.find((d) => d.id === rival.citation.documentId)?.kind;
    return { ...rival, kind, class: kind === "invoice" ? "Operational" : "Contractual" };
  });
  const hasOperational = rivals.some((r) => r.class === "Operational");

  return (
    <div>
      <div className="border-b border-border px-3.5 py-3">
        <div className="font-mono text-[11px] text-muted-foreground">{item.key}</div>
        <div className="mt-1 flex flex-wrap items-baseline gap-2">
          <ValueDisplay value={item.value} className="text-[20px] font-semibold" />
          <StateBadge state={item.state} />
        </div>
      </div>

      {item.state === "disputed" && rivals.length ? (
        <InspectorSection label="Unresolved dispute">
          <div className="grid grid-cols-2 gap-2">
            {rivals.map((rival, i) => (
              <div key={i} className="rounded border border-border bg-surface px-2 py-1.5">
                <div className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
                  {rival.class}
                </div>
                <ValueDisplay value={rival.value} className="mt-0.5 block text-[15px] font-semibold" />
                <div className="mt-0.5 text-[10.5px] leading-[1.4] text-muted-foreground">
                  {rival.label}
                </div>
              </div>
            ))}
          </div>
          <p className="mt-2 flex items-start gap-1.5 rounded border border-warning/30 bg-warning-surface px-2 py-1.5 text-[11.5px] leading-[1.5] text-warning-foreground">
            <GitCompareArrows className="mt-0.5 size-3.5 shrink-0" aria-hidden />
            {hasOperational
              ? "Operational evidence does not supersede contractual terms. A reviewer must resolve this before the value is authoritative."
              : "Both values are contractual. Neither is authoritative until a reviewer confirms which clause governs."}
          </p>
        </InspectorSection>
      ) : null}

      <InspectorSection label="Source">
        {primary ? (
          <>
            <div className="text-[12.5px] font-medium text-foreground">{primary.filename}</div>
            <SourceLine citation={primary} />
            <div className="mt-2">
              <GroundedQuote citation={primary} />
            </div>
          </>
        ) : (
          <p className="text-[12px] leading-[1.55] text-muted-foreground">
            No source quote. This value is recorded as {item.state} and is never asserted as fact.
          </p>
        )}
      </InspectorSection>

      {item.citations.length > 1 ? (
        <InspectorSection label={`Additional quotes (${item.citations.length - 1})`}>
          <div className="space-y-2">
            {item.citations.slice(1).map((c, i) => (
              <div key={i}>
                <div className="text-[11.5px] font-medium text-foreground">{c.filename}</div>
                <SourceLine citation={c} />
                <div className="mt-1">
                  <GroundedQuote citation={c} />
                </div>
              </div>
            ))}
          </div>
        </InspectorSection>
      ) : null}

      <InspectorSection label="Provenance">
        <MetaList
          rows={[
            { label: "Agreement", value: item.agreementId || "—" },
            { label: "Version", value: `v${item.version}` },
            // The register API does not carry the originating run id.
            { label: "Run ID", value: <span className="text-muted-foreground">not exposed</span> },
            { label: "Content hash", value: <Hash value={item.contentHash} chars={12} /> },
            {
              label: "Document hash",
              value: docHash ? (
                <Hash value={docHash} chars={12} />
              ) : (
                <span className="text-muted-foreground">—</span>
              ),
            },
            { label: "Last updated", value: <Timestamp value={item.updatedAt} /> },
          ]}
        />
      </InspectorSection>

      <InspectorSection label="Register key">
        <div className="break-all font-mono text-[11px] text-muted-foreground">
          {item.registerKey}
        </div>
      </InspectorSection>
    </div>
  );
}

export { SectionLabel };

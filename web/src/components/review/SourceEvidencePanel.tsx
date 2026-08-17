import { ShieldAlert } from "lucide-react";
import type { Citation, DocumentSummary } from "@/api/types";
import { Hash, SectionLabel } from "@/components/doctask/primitives";
import { MetaList } from "@/components/doctask/surfaces";

/**
 * The reviewer's proof panel: the source page as written, with the exact span
 * the extraction relied on marked more strongly than elsewhere in the product.
 */
export function SourceEvidencePanel({
  citations,
  documents,
}: {
  citations: Citation[];
  documents: DocumentSummary[];
}) {
  const primary = citations[0] ?? null;

  if (!primary) {
    return (
      <aside className="flex w-full shrink-0 flex-col border-t border-border bg-background xl:w-[380px] xl:border-l xl:border-t-0">
        <div className="border-b border-border px-4 py-2.5">
          <span className="text-[12.5px] font-medium text-foreground">No source</span>
        </div>
        <div className="px-4 py-4">
          <div className="flex items-start gap-2 rounded-md border border-warning/30 bg-warning-surface px-2.5 py-2 text-[12px] leading-[1.55] text-warning-foreground">
            <ShieldAlert className="mt-0.5 size-3.5 shrink-0" aria-hidden />
            No quote supports this value. Approving it requires a written override recorded against
            your name.
          </div>
        </div>
      </aside>
    );
  }

  const docHash = documents.find((d) => d.id === primary.documentId)?.contentHash ?? null;
  const before = primary.quote.slice(0, primary.charStart);
  const span = primary.quote.slice(primary.charStart, primary.charEnd);
  const after = primary.quote.slice(primary.charEnd);

  return (
    <aside className="flex w-full shrink-0 flex-col overflow-y-auto border-t border-border bg-background xl:w-[380px] xl:border-l xl:border-t-0">
      <div className="flex items-baseline justify-between gap-2 border-b border-border px-4 py-2.5">
        <span className="truncate text-[12.5px] font-medium text-foreground">
          {primary.filename}
        </span>
        <span className="shrink-0 font-mono text-[11px] text-muted-foreground">
          {primary.page === null ? "no page" : `Page ${primary.page}`}
        </span>
      </div>

      <div className="px-4 py-4">
        <SectionLabel>Excerpt</SectionLabel>
        <div className="mt-2 border border-border bg-card px-4 py-4">
          <p className="text-[13px] leading-[1.85] text-foreground">
            {before}
            <mark className="rounded-[2px] bg-warning/25 px-0.5 font-medium text-foreground shadow-[inset_0_-1.5px_0_0_var(--color-warning)]">
              {span}
            </mark>
            {after}
          </p>
        </div>

        <SectionLabel className="mt-4">Grounding</SectionLabel>
        <div className="mt-1.5">
          <MetaList
            rows={[
              { label: "Block ID", value: `block ${primary.blockIndex}` },
              { label: "Offsets", value: `${primary.charStart}–${primary.charEnd}` },
              {
                label: "Document hash",
                value: docHash ? (
                  <Hash value={docHash} chars={12} />
                ) : (
                  <span className="text-muted-foreground">—</span>
                ),
              },
              // No extraction-method field is returned with a citation.
              {
                label: "Extraction",
                value: <span className="text-muted-foreground">not exposed</span>,
              },
            ]}
          />
        </div>

        {citations.length > 1 ? (
          <>
            <SectionLabel className="mt-4">Other quotes ({citations.length - 1})</SectionLabel>
            <ul className="mt-1.5 space-y-1.5">
              {citations.slice(1).map((c, i) => (
                <li key={i} className="rounded border border-border bg-card px-2.5 py-1.5">
                  <div className="flex items-baseline justify-between gap-2">
                    <span className="truncate text-[11.5px] font-medium text-foreground">
                      {c.filename}
                    </span>
                    <span className="shrink-0 font-mono text-[10.5px] text-muted-foreground">
                      {c.page === null ? "no page" : `p.${c.page}`} · blk {c.blockIndex}
                    </span>
                  </div>
                  <p className="mt-1 font-mono text-[11px] leading-[1.5] text-muted-foreground">
                    {c.quote.slice(0, c.charStart)}
                    <mark className="evidence-mark">{c.quote.slice(c.charStart, c.charEnd)}</mark>
                    {c.quote.slice(c.charEnd)}
                  </p>
                </li>
              ))}
            </ul>
          </>
        ) : null}
      </div>
    </aside>
  );
}

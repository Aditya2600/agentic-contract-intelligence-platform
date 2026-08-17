import { useMemo, useRef, useState } from "react";
import { EyeOff } from "lucide-react";
import type { DocumentDetail, DocumentFact } from "@/api/types";
import { SeverityBadge, VerdictBadge } from "@/components/doctask/badges";
import { Hash, SectionLabel, Timestamp, ValueDisplay } from "@/components/doctask/primitives";
import { InspectorSection, MetaList } from "@/components/doctask/surfaces";
import { cn } from "@/lib/utils";

type Tab = "facts" | "findings" | "metadata";

const TABS: { id: Tab; label: string }[] = [
  { id: "facts", label: "Facts" },
  { id: "findings", label: "Findings" },
  { id: "metadata", label: "Metadata" },
];

/** Renders block text with each extracted span marked; the focused one stronger. */
function BlockText({
  text,
  spans,
  activeKey,
}: {
  text: string;
  spans: DocumentFact[];
  activeKey: string | null;
}) {
  if (spans.length === 0) return <>{text}</>;

  const sorted = [...spans].sort((a, b) => a.charStart - b.charStart);
  const parts: React.ReactNode[] = [];
  let cursor = 0;

  sorted.forEach((s, i) => {
    if (s.charStart > cursor) parts.push(text.slice(cursor, s.charStart));
    parts.push(
      <mark
        key={`${s.key}-${i}`}
        data-fact={s.key}
        className={cn(
          "evidence-mark",
          activeKey === s.key &&
            "bg-warning/30 font-medium shadow-[inset_0_-1.5px_0_0_var(--color-warning)]",
        )}
      >
        {text.slice(s.charStart, s.charEnd)}
      </mark>,
    );
    cursor = Math.max(cursor, s.charEnd);
  });

  if (cursor < text.length) parts.push(text.slice(cursor));
  return <>{parts}</>;
}

export function DocumentViewer({ doc }: { doc: DocumentDetail }) {
  const [activeKey, setActiveKey] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("facts");
  const canvasRef = useRef<HTMLDivElement>(null);

  const factsByBlock = useMemo(() => {
    const map = new Map<number, DocumentFact[]>();
    for (const f of doc.facts) {
      const list = map.get(f.blockIndex) ?? [];
      list.push(f);
      map.set(f.blockIndex, list);
    }
    return map;
  }, [doc.facts]);

  /** Clicking a fact scrolls its citation into view and locks the highlight. */
  const focusFact = (fact: DocumentFact) => {
    setActiveKey(fact.key);
    const node = canvasRef.current?.querySelector(`[data-block="${fact.blockIndex}"]`);
    node?.scrollIntoView({ behavior: "smooth", block: "center" });
  };

  const withheld = doc.blocks.filter((b) => b.withheld);

  return (
    <div className="flex min-h-0 flex-1 flex-col xl:flex-row">
      {/* Paper-like document canvas */}
      <div ref={canvasRef} className="min-w-0 flex-1 overflow-y-auto bg-surface px-6 py-6">
        <article className="mx-auto max-w-[720px] border border-border bg-card px-10 py-9">
          <header className="mb-6 border-b border-border pb-3">
            <h2 className="text-[15px] font-semibold text-foreground">{doc.filename}</h2>
            <p className="mt-0.5 font-mono text-[11px] text-muted-foreground">
              {doc.kind} · {doc.pages} pages · {doc.blocks.length} blocks
            </p>
          </header>

          <div className="space-y-5">
            {doc.blocks.map((block) => (
              <div key={block.index} data-block={block.index} className="scroll-mt-24">
                <div className="mb-1 font-mono text-[10px] uppercase tracking-[0.06em] text-muted-foreground">
                  block {block.index} · {block.page === null ? "no page" : `p.${block.page}`}
                </div>

                {block.withheld ? (
                  <div className="rounded border border-danger/40 bg-danger-surface px-3 py-2.5">
                    <div className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-[0.06em] text-danger-foreground">
                      <EyeOff className="size-3.5" aria-hidden />
                      Withheld from model context
                    </div>
                    <div className="mt-2">
                      <div className="text-[10px] font-semibold uppercase tracking-[0.08em] text-danger-foreground/80">
                        Detected signals
                      </div>
                      <ul className="mt-0.5 space-y-0.5">
                        {block.detectionSignals.map((s) => (
                          <li key={s} className="font-mono text-[11px] text-danger-foreground">
                            {s}
                          </li>
                        ))}
                      </ul>
                    </div>
                    <p className="mt-2 whitespace-pre-wrap font-mono text-[11.5px] leading-[1.6] text-muted-foreground line-through">
                      {block.text}
                    </p>
                    <p className="mt-2 border-t border-danger/20 pt-1.5 text-[11px] leading-[1.5] text-muted-foreground">
                      Other safe blocks continued through extraction. Nothing in this block reached
                      the model.
                    </p>
                  </div>
                ) : (
                  <p className="whitespace-pre-wrap text-[13px] leading-[1.85] text-foreground">
                    <BlockText
                      text={block.text}
                      spans={factsByBlock.get(block.index) ?? []}
                      activeKey={activeKey}
                    />
                  </p>
                )}
              </div>
            ))}
          </div>
        </article>
      </div>

      {/* Document intelligence inspector */}
      <aside className="flex w-full shrink-0 flex-col overflow-y-auto border-t border-border bg-card xl:w-[360px] xl:border-l xl:border-t-0">
        <div className="sticky top-0 z-10 border-b border-border bg-card px-3.5 pt-3">
          <SectionLabel>Document intelligence</SectionLabel>
          <nav className="mt-2 flex gap-1">
            {TABS.map((t) => (
              <button
                key={t.id}
                type="button"
                onClick={() => setTab(t.id)}
                className={cn(
                  "-mb-px border-b-2 px-2 py-1.5 text-[12px] font-medium transition-colors",
                  tab === t.id
                    ? "border-primary text-foreground"
                    : "border-transparent text-muted-foreground hover:text-foreground",
                )}
              >
                {t.label}
                <span className="ml-1 font-mono text-[10.5px] text-muted-foreground">
                  {t.id === "facts"
                    ? doc.facts.length
                    : t.id === "findings"
                      ? doc.findings.length
                      : ""}
                </span>
              </button>
            ))}
          </nav>
        </div>

        {tab === "facts" ? (
          <ul className="divide-y divide-border">
            {doc.facts.map((f) => (
              <li key={f.key}>
                <button
                  type="button"
                  onClick={() => focusFact(f)}
                  onMouseEnter={() => setActiveKey(f.key)}
                  onFocus={() => setActiveKey(f.key)}
                  className={cn(
                    "w-full px-3.5 py-2 text-left transition-colors hover:bg-accent/60",
                    activeKey === f.key && "bg-accent",
                  )}
                >
                  <div className="flex items-baseline justify-between gap-2">
                    <span className="truncate font-mono text-[12px] text-foreground">{f.key}</span>
                    <ValueDisplay value={f.value} />
                  </div>
                  <div className="mt-0.5 font-mono text-[10.5px] text-muted-foreground">
                    block {f.blockIndex} · chars {f.charStart}–{f.charEnd}
                  </div>
                </button>
              </li>
            ))}
            {doc.facts.length === 0 ? (
              <li className="px-3.5 py-3 text-[12px] text-muted-foreground">
                No facts were extracted from this document.
              </li>
            ) : null}
          </ul>
        ) : null}

        {tab === "findings" ? (
          <ul className="divide-y divide-border">
            {doc.findings.map((f) => (
              <li key={f.ruleCode} className="px-3.5 py-2.5">
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="font-mono text-[12px] font-medium text-foreground">
                    {f.ruleCode}
                  </span>
                  <SeverityBadge severity={f.severity} />
                  <VerdictBadge verdict={f.verdict} />
                </div>
                <p className="mt-1 text-[11.5px] leading-[1.55] text-muted-foreground">
                  {f.rationale}
                </p>
              </li>
            ))}
            {doc.findings.length === 0 ? (
              <li className="px-3.5 py-3 text-[12px] text-muted-foreground">
                No playbook rule raised a finding against this document.
              </li>
            ) : null}
          </ul>
        ) : null}

        {tab === "metadata" ? (
          <div>
            <InspectorSection label="Identity">
              <MetaList
                rows={[
                  { label: "Document ID", value: doc.id },
                  { label: "Kind", value: doc.kind },
                  { label: "Pages", value: doc.pages },
                  { label: "Blocks", value: doc.blocks.length },
                  { label: "Content hash", value: <Hash value={doc.contentHash} chars={14} /> },
                  { label: "Ingested", value: <Timestamp value={doc.ingestedAt} /> },
                ]}
              />
            </InspectorSection>
            <InspectorSection label="Extraction safety">
              <MetaList
                rows={[
                  { label: "Facts extracted", value: doc.facts.length },
                  { label: "Blocks withheld", value: withheld.length },
                ]}
              />
              {withheld.length > 0 ? (
                <p className="mt-2 text-[11.5px] leading-[1.5] text-danger-foreground">
                  {withheld.length} block{withheld.length === 1 ? " was" : "s were"} withheld from
                  model context after injection signals were detected.
                </p>
              ) : (
                <p className="mt-2 text-[11.5px] leading-[1.5] text-muted-foreground">
                  No injection signals were detected in this document.
                </p>
              )}
            </InspectorSection>
          </div>
        ) : null}
      </aside>
    </div>
  );
}

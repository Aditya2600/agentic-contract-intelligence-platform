import { cn } from "@/lib/utils";
import type { Citation } from "@/api/types";
import { FileText } from "lucide-react";

export function Hash({ value, chars = 10, className }: { value: string; chars?: number; className?: string }) {
  const bare = value.replace(/^sha256:/, "");
  return (
    <span
      title={value}
      className={cn("font-mono text-[11px] text-muted-foreground", className)}
    >
      {bare.slice(0, chars)}…
    </span>
  );
}

export function Money({ value, className }: { value: number | null; className?: string }) {
  // An unknown cost is never rendered as $0 — that would understate real spend.
  if (value === null) {
    return (
      <span
        className={cn(
          "font-mono text-[11px] uppercase tracking-[0.04em] text-muted-foreground",
          className,
        )}
      >
        unpriced
      </span>
    );
  }
  return (
    <span className={cn("num font-mono text-[12px]", className)}>
      ${value.toFixed(4)}
    </span>
  );
}

export function Duration({ ms, className }: { ms: number; className?: string }) {
  const s = ms / 1000;
  const text =
    s < 60 ? `${s.toFixed(1)}s` : `${Math.floor(s / 60)}m ${String(Math.round(s % 60)).padStart(2, "0")}s`;
  return <span className={cn("num font-mono text-[12px]", className)}>{text}</span>;
}

export function Timestamp({ value, className }: { value: string; className?: string }) {
  const d = new Date(value);
  const text = `${d.toISOString().slice(0, 10)} ${d.toISOString().slice(11, 16)}Z`;
  return <span className={cn("num text-[12px] text-muted-foreground", className)}>{text}</span>;
}

export function ValueDisplay({ value, className }: { value: unknown; className?: string }) {
  if (value === null || value === undefined) {
    return <span className={cn("text-[12px] text-muted-foreground italic", className)}>none</span>;
  }
  if (typeof value === "boolean") {
    return <span className={cn("font-mono text-[12px]", className)}>{String(value)}</span>;
  }
  return (
    <span className={cn("num font-mono text-[12px] text-foreground", className)}>
      {typeof value === "object" ? JSON.stringify(value) : String(value)}
    </span>
  );
}

export function SectionLabel({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <div
      className={cn(
        "text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground",
        className,
      )}
    >
      {children}
    </div>
  );
}

/** A verbatim source quote with the grounding span highlighted. */
export function CitationBlock({
  citation,
  className,
  compact = false,
}: {
  citation: Citation;
  className?: string;
  compact?: boolean;
}) {
  const { quote, charStart, charEnd } = citation;
  const before = quote.slice(0, charStart);
  const span = quote.slice(charStart, charEnd);
  const after = quote.slice(charEnd);

  return (
    <div className={cn("rounded-md border border-border bg-surface px-2.5 py-2", className)}>
      <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[11px] text-muted-foreground">
        <FileText className="size-3 shrink-0" />
        <span className="font-medium text-foreground">{citation.filename}</span>
        <span className="font-mono">
          {citation.page === null ? "no page" : `p.${citation.page}`}
        </span>
        <span className="font-mono">block {citation.blockIndex}</span>
        <span className="font-mono">
          [{citation.charStart}–{citation.charEnd}]
        </span>
      </div>
      <p
        className={cn(
          "mt-1.5 font-mono text-foreground/90",
          compact ? "text-[11px] leading-[1.55]" : "text-[12px] leading-[1.6]",
        )}
      >
        {before}
        <mark className="evidence-mark bg-transparent">{span}</mark>
        {after}
      </p>
    </div>
  );
}

export function EmptyState({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="flex flex-col items-center justify-center gap-1 rounded-md border border-dashed border-border py-12 text-center">
      <p className="text-[13px] font-medium text-foreground">{title}</p>
      {hint ? <p className="max-w-md text-[12px] text-muted-foreground">{hint}</p> : null}
    </div>
  );
}

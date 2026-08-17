import type { ReactNode } from "react";
import { cn } from "@/lib/utils";
import { SectionLabel } from "@/components/doctask/primitives";

/**
 * Shared structural primitives. Every operational screen composes these instead
 * of restyling its own tables and panels, so density stays consistent.
 */

/** Header cell classes for every dense operational table. */
export const th =
  "px-2.5 py-1.5 text-left text-[10.5px] font-semibold uppercase tracking-[0.06em] text-muted-foreground whitespace-nowrap";

/** Body cell classes. Rows sit at ~30px with this padding. */
export const td = "px-2.5 py-[7px] align-middle text-[12.5px]";

/** A flat bordered region. Not a floating card: one hairline, no shadow. */
export function Panel({
  children,
  className,
  flush = false,
}: {
  children: ReactNode;
  className?: string;
  flush?: boolean;
}) {
  return (
    <div
      className={cn(
        "border border-border bg-card",
        flush ? "border-x-0" : "rounded-md",
        className,
      )}
    >
      {children}
    </div>
  );
}

/** Bordered wrapper around a dense table, with horizontal overflow contained. */
export function TableFrame({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div className={cn("overflow-x-auto rounded-md border border-border bg-card", className)}>
      <table className="w-full min-w-[720px] border-collapse text-left">{children}</table>
    </div>
  );
}

export function Thead({ children }: { children: ReactNode }) {
  return (
    <thead>
      <tr className="border-b border-border bg-surface">{children}</tr>
    </thead>
  );
}

/** A clickable table row with a selected treatment. */
export function Row({
  children,
  selected = false,
  onClick,
  className,
}: {
  children: ReactNode;
  selected?: boolean;
  onClick?: () => void;
  className?: string;
}) {
  return (
    <tr
      onClick={onClick}
      className={cn(
        "border-b border-border/70 last:border-0",
        onClick && "cursor-pointer hover:bg-accent/60",
        selected && "bg-accent",
        className,
      )}
    >
      {children}
    </tr>
  );
}

/** Group separator row inside a grouped table. */
export function GroupRow({
  label,
  count,
  colSpan,
  muted = false,
}: {
  label: string;
  count: number;
  colSpan: number;
  muted?: boolean;
}) {
  return (
    <tr className="border-y border-border bg-surface">
      <td colSpan={colSpan} className="px-2.5 py-1">
        <div className="flex items-center gap-2">
          <span
            className={cn(
              "font-mono text-[11.5px] font-semibold",
              muted ? "italic text-muted-foreground" : "text-foreground",
            )}
          >
            {label}
          </span>
          <span className="text-[11px] text-muted-foreground">{count} rows</span>
        </div>
      </td>
    </tr>
  );
}

/** Compact horizontal metric readout. Hairline dividers, no cards. */
export function MetricStrip({
  items,
  className,
}: {
  items: { label: string; value: ReactNode; tone?: "default" | "warning" | "danger" }[];
  className?: string;
}) {
  return (
    <dl
      className={cn(
        "flex flex-wrap items-stretch divide-x divide-border rounded-md border border-border bg-card",
        className,
      )}
    >
      {items.map((m) => (
        <div key={m.label} className="min-w-[128px] flex-1 px-3 py-2">
          <dt className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
            {m.label}
          </dt>
          <dd
            className={cn(
              "num mt-0.5 font-mono text-[16px] leading-none",
              m.tone === "warning"
                ? "text-warning-foreground"
                : m.tone === "danger"
                  ? "text-danger-foreground"
                  : "text-foreground",
            )}
          >
            {m.value}
          </dd>
        </div>
      ))}
    </dl>
  );
}

/**
 * Master/detail frame: dense content on the left, a persistent inspector on the
 * right that sticks under the page header.
 */
export function SplitView({
  main,
  inspector,
  inspectorWidth = "380px",
}: {
  main: ReactNode;
  inspector: ReactNode;
  inspectorWidth?: string;
}) {
  return (
    <div className="flex min-h-0 flex-1 flex-col items-stretch xl:flex-row">
      <div className="min-w-0 flex-1 overflow-x-auto px-5 py-4">{main}</div>
      {/* Below xl the inspector stacks under the table rather than disappearing:
          hiding it would take the evidence away entirely. */}
      <aside
        className="w-full shrink-0 border-t border-border bg-card xl:w-[var(--inspector-w)] xl:border-l xl:border-t-0"
        style={{ ["--inspector-w" as string]: inspectorWidth }}
        data-inspector
      >
        <div className="xl:sticky xl:top-[var(--header-h,96px)] xl:max-h-[calc(100vh-var(--header-h,96px))] xl:overflow-y-auto">
          {inspector}
        </div>
      </aside>
    </div>
  );
}

/** A labelled section inside an inspector panel, separated by hairlines. */
export function InspectorSection({
  label,
  children,
  actions,
  className,
}: {
  label: string;
  children: ReactNode;
  actions?: ReactNode;
  className?: string;
}) {
  return (
    <section className={cn("border-b border-border px-3.5 py-3 last:border-0", className)}>
      <div className="flex items-center justify-between gap-2">
        <SectionLabel>{label}</SectionLabel>
        {actions}
      </div>
      <div className="mt-2">{children}</div>
    </section>
  );
}

/** Tight key/value list used throughout inspectors. Values are monospace. */
export function MetaList({
  rows,
  className,
}: {
  rows: { label: string; value: ReactNode }[];
  className?: string;
}) {
  return (
    <dl className={cn("space-y-1", className)}>
      {rows.map((r) => (
        <div key={r.label} className="flex items-baseline justify-between gap-3">
          <dt className="shrink-0 text-[11.5px] text-muted-foreground">{r.label}</dt>
          <dd className="min-w-0 truncate text-right font-mono text-[11.5px] text-foreground">
            {r.value}
          </dd>
        </div>
      ))}
    </dl>
  );
}

/** Placeholder shown in an inspector when nothing is selected. */
export function InspectorEmpty({ children }: { children: ReactNode }) {
  return (
    <div className="px-3.5 py-6 text-[12px] leading-[1.6] text-muted-foreground">{children}</div>
  );
}

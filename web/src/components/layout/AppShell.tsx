import { Link } from "@tanstack/react-router";
import {
  Activity,
  BookMarked,
  ChevronsUpDown,
  ClipboardCheck,
  FolderTree,
  ShieldAlert,
} from "lucide-react";
import type { ReactNode } from "react";
import { cn } from "@/lib/utils";
import { USE_MOCKS } from "@/api/config";

const PRIMARY_COLLECTION = "col-acme";
const PRIMARY_RUN = "run-1002";

const linkClass =
  "flex items-center gap-2 rounded px-2 py-[5px] text-[12.5px] text-navy-muted transition-colors hover:bg-navy-hover hover:text-navy-foreground";
const activeClass = { className: "bg-navy-active text-navy-foreground" };

/**
 * Fixed dark navigation rail. Deliberately quiet: the contract data in the main
 * region should be the only thing with visual weight.
 */
export function AppShell({ children }: { children: ReactNode }) {
  return (
    <div className="flex min-h-screen bg-background">
      <aside className="sticky top-0 hidden h-screen w-[220px] shrink-0 flex-col border-r border-navy-border bg-navy text-navy-foreground md:flex">
        <div className="flex items-center gap-2 px-3.5 py-3">
          <span className="grid size-5 place-items-center rounded bg-navy-active text-[10px] font-bold">
            D
          </span>
          <span className="text-[13px] font-semibold tracking-tight">Doctask</span>
        </div>

        <Link
          to="/collections"
          className="mx-2 flex items-center gap-2 rounded border border-navy-border px-2 py-1.5 text-left transition-colors hover:bg-navy-hover"
        >
          <span className="min-w-0 flex-1 truncate text-[11.5px] font-medium text-navy-foreground">
            Acme Vendor Agreements
          </span>
          <ChevronsUpDown className="size-3 shrink-0 text-navy-muted" aria-hidden />
        </Link>

        <nav className="mt-3 flex-1 space-y-0.5 px-2">
          <Link
            to="/collections"
            activeOptions={{ exact: true }}
            className={linkClass}
            activeProps={activeClass}
          >
            <FolderTree className="size-3.5 shrink-0" aria-hidden />
            Collections
          </Link>
          <Link
            to="/collections/$collectionId/runs/$runId/review"
            params={{ collectionId: PRIMARY_COLLECTION, runId: PRIMARY_RUN }}
            className={linkClass}
            activeProps={activeClass}
          >
            <ClipboardCheck className="size-3.5 shrink-0" aria-hidden />
            Reviews
          </Link>
          <Link
            to="/collections/$collectionId"
            params={{ collectionId: PRIMARY_COLLECTION }}
            search={{ tab: "runs" }}
            className={linkClass}
            activeProps={activeClass}
          >
            <Activity className="size-3.5 shrink-0" aria-hidden />
            Runs
          </Link>
          <Link
            to="/collections/$collectionId/runs/$runId/findings"
            params={{ collectionId: PRIMARY_COLLECTION, runId: PRIMARY_RUN }}
            className={linkClass}
            activeProps={activeClass}
          >
            <ShieldAlert className="size-3.5 shrink-0" aria-hidden />
            Findings
          </Link>
          <Link
            to="/collections/$collectionId"
            params={{ collectionId: PRIMARY_COLLECTION }}
            search={{ tab: "playbook" }}
            className={linkClass}
            activeProps={activeClass}
          >
            <BookMarked className="size-3.5 shrink-0" aria-hidden />
            Playbooks
          </Link>
        </nav>

        <div className="border-t border-navy-border px-3.5 py-2.5 text-[10.5px] leading-relaxed text-navy-muted">
          <div className="flex items-center gap-1.5">
            <span className={cn("size-1.5 rounded-full", USE_MOCKS ? "bg-warning" : "bg-success")} />
            {USE_MOCKS ? "mock fixtures" : "live REST backend"}
          </div>
          <div className="mt-0.5 font-mono">ruleset ab41c05d</div>
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">{children}</div>
    </div>
  );
}

/**
 * Top bar: breadcrumb left, contextual actions right. Optional title block and
 * tabs sit beneath it. Kept to a hairline bottom border with no shadow.
 */
export function PageHeader({
  breadcrumb,
  title,
  meta,
  actions,
  banner,
  tabs,
}: {
  breadcrumb?: ReactNode;
  title?: ReactNode;
  meta?: ReactNode;
  actions?: ReactNode;
  banner?: ReactNode;
  tabs?: ReactNode;
}) {
  return (
    <header className="sticky top-0 z-20 border-b border-border bg-background/95 backdrop-blur">
      <div className="flex h-[50px] items-center gap-3 px-5">
        <div className="flex min-w-0 flex-1 items-center gap-1.5 text-[11.5px] text-muted-foreground">
          {breadcrumb}
        </div>
        {actions ? <div className="flex shrink-0 items-center gap-2">{actions}</div> : null}
      </div>

      {title ? (
        <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 px-5 pb-3">
          <h1 className="truncate text-[19px] font-semibold tracking-[-0.02em] text-foreground">
            {title}
          </h1>
          {meta ? (
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11.5px] text-muted-foreground">
              {meta}
            </div>
          ) : null}
        </div>
      ) : null}

      {tabs ? <div className="px-5">{tabs}</div> : null}
      {banner}
    </header>
  );
}

export function PageBody({ children, className }: { children: ReactNode; className?: string }) {
  return <main className={cn("min-w-0 flex-1 px-5 py-4", className)}>{children}</main>;
}

/** Body variant for master/detail screens that manage their own padding. */
export function PageBodyFlush({ children }: { children: ReactNode }) {
  return <main className="flex min-w-0 flex-1 flex-col">{children}</main>;
}

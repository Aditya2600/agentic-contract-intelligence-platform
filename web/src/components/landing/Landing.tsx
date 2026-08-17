import { Link } from "@tanstack/react-router";
import { ArrowDown, ArrowRight, Check, X } from "lucide-react";
import { USE_MOCKS } from "@/api/config";
import { cn } from "@/lib/utils";

const PRIMARY_COLLECTION = "col-acme";

/** Small uppercase eyebrow used to label landing sections. */
function Eyebrow({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <p
      className={cn(
        "text-[10.5px] font-semibold uppercase tracking-[0.12em] text-muted-foreground",
        className,
      )}
    >
      {children}
    </p>
  );
}

/**
 * The hero visual is a real Doctask evidence inspector rendered with the same
 * primitives the product uses, not a marketing illustration.
 */
function EvidencePreview() {
  return (
    <section
      aria-label="Evidence inspector preview"
      className="overflow-hidden rounded-lg border border-border bg-card"
    >
      <div className="flex items-center justify-between gap-4 border-b border-border px-3.5 py-2.5">
        <span className="text-[12.5px] font-semibold text-foreground">Acme Vendor Agreements</span>
        <span className="rounded border border-border bg-surface px-1.5 py-0.5 font-mono text-[10.5px] text-muted-foreground">
          Amendment No. 1
        </span>
      </div>

      <div className="px-3.5 py-3.5">
        <div className="font-mono text-[11px] text-muted-foreground">payment_due_days</div>

        <div className="mt-1.5 flex items-baseline gap-2.5">
          <span className="num font-mono text-[14px] text-muted-foreground line-through">
            30 days
          </span>
          <ArrowRight className="size-3 shrink-0 self-center text-muted-foreground" aria-hidden />
          <span className="num text-[21px] font-semibold tracking-[-0.02em] text-foreground">
            45 days
          </span>
        </div>

        <Eyebrow className="mt-4">Evidence</Eyebrow>
        <blockquote className="mt-1.5 rounded-md border border-border bg-surface px-2.5 py-2">
          <p className="font-mono text-[11.5px] leading-[1.65] text-foreground/90">
            …Customer shall pay each undisputed invoice{" "}
            <mark className="evidence-mark">within forty-five (45) days</mark> of receipt.
          </p>
        </blockquote>

        <Eyebrow className="mt-4">Source</Eyebrow>
        <p className="mt-1.5 font-mono text-[11.5px] text-muted-foreground">
          Amendment_No_1.pdf · Page 1
        </p>
      </div>

      <div className="flex items-center justify-end gap-2 border-t border-border bg-surface/60 px-3.5 py-2.5">
        <span className="inline-flex items-center gap-1 rounded border border-border bg-card px-2.5 py-1 text-[12px] font-medium text-muted-foreground">
          <X className="size-3.5" aria-hidden />
          Reject
        </span>
        <span className="inline-flex items-center gap-1 rounded border border-success/30 bg-success-surface px-2.5 py-1 text-[12px] font-medium text-success-foreground">
          <Check className="size-3.5" aria-hidden />
          Approve
        </span>
      </div>
    </section>
  );
}

const PRINCIPLES = [
  {
    index: "01",
    title: "Grounded evidence",
    body: "Every register value resolves to a verbatim quote with character offsets inside a named source document. No quote, no assertion.",
  },
  {
    index: "02",
    title: "Human-controlled changes",
    body: "No durable write happens without an explicit reviewer decision. Ungrounded approvals require a written override recorded against a name.",
  },
  {
    index: "03",
    title: "Crash-safe execution",
    body: "Runs are resumable and every stage decision, retry and branch is recorded, so an interrupted run replays instead of guessing.",
  },
] as const;

const WORKFLOW = [
  { step: "Documents", caption: "pdf · docx · txt" },
  { step: "AI pipeline", caption: "extract · ground · verify" },
  { step: "Human review", caption: "approve · reject · override" },
  { step: "Obligations register", caption: "versioned · cited" },
] as const;

export function Landing() {
  return (
    <div className="min-h-screen bg-background">
      <header className="sticky top-0 z-20 border-b border-border bg-background/95 backdrop-blur">
        <div className="mx-auto flex h-13 max-w-6xl items-center justify-between px-6 py-3">
          <div className="flex items-center gap-2">
            <span className="grid size-5 place-items-center rounded bg-navy text-[10px] font-bold text-navy-foreground">
              D
            </span>
            <span className="text-[13.5px] font-semibold tracking-tight text-foreground">
              Doctask
            </span>
          </div>
          <Link
            to="/collections"
            className="rounded-md bg-primary px-3 py-1.5 text-[12.5px] font-medium text-primary-foreground transition-colors hover:bg-primary/90"
          >
            Open demo workspace
          </Link>
        </div>
      </header>

      <main>
        {/* Hero */}
        <section className="mx-auto grid max-w-6xl grid-cols-1 items-start gap-12 px-6 pb-16 pt-16 lg:grid-cols-[minmax(0,1.15fr)_minmax(0,0.85fr)] lg:gap-14 lg:pt-20">
          <div>
            <Eyebrow>Obligations intelligence</Eyebrow>
            <h1 className="mt-4 text-[32px] font-semibold leading-[1.12] tracking-[-0.032em] text-foreground sm:text-[38px] lg:text-[42px]">
              <span className="block whitespace-nowrap">Every obligation. Grounded.</span>
              <span className="block whitespace-nowrap">Every change. Human-approved.</span>
            </h1>
            <p className="mt-5 max-w-[58ch] text-[15px] leading-[1.65] text-muted-foreground">
              Doctask turns contracts, amendments, SOWs, policies and invoices into an
              evidence-backed obligations register while keeping humans in control of every durable
              change.
            </p>
            <div className="mt-7 flex flex-wrap items-center gap-3">
              <Link
                to="/collections"
                className="rounded-md bg-primary px-4 py-2 text-[13.5px] font-medium text-primary-foreground transition-colors hover:bg-primary/90"
              >
                Open demo workspace
              </Link>
              <a
                href="#how-it-works"
                className="group inline-flex items-center gap-1.5 rounded-md px-2 py-2 text-[13.5px] font-medium text-foreground transition-colors hover:text-primary"
              >
                See how it works
                <ArrowDown
                  className="size-3.5 transition-transform group-hover:translate-y-0.5"
                  aria-hidden
                />
              </a>
            </div>
          </div>

          <div className="lg:pt-5">
            <EvidencePreview />
          </div>
        </section>

        {/* Principles */}
        <section className="border-t border-border">
          <div className="mx-auto max-w-6xl px-6 py-12">
            <div className="grid gap-px bg-border sm:grid-cols-3">
              {PRINCIPLES.map((p) => (
                <div key={p.index} className="bg-background px-0 sm:px-5 sm:first:pl-0 sm:last:pr-0">
                  <div className="font-mono text-[11px] text-muted-foreground">{p.index}</div>
                  <h2 className="mt-2 text-[14px] font-semibold text-foreground">{p.title}</h2>
                  <p className="mt-1.5 text-[13px] leading-[1.6] text-muted-foreground">{p.body}</p>
                </div>
              ))}
            </div>
          </div>
        </section>

        {/* Workflow */}
        <section id="how-it-works" className="scroll-mt-16 border-t border-border">
          <div className="mx-auto max-w-6xl px-6 py-12">
            <Eyebrow>How it works</Eyebrow>
            <ol className="mt-5 flex flex-col gap-3 sm:flex-row sm:items-stretch">
              {WORKFLOW.map((w, i) => (
                <li key={w.step} className="flex flex-1 items-center gap-3">
                  <div className="flex-1 border-t border-border-strong pt-2">
                    <div className="text-[13px] font-medium text-foreground">{w.step}</div>
                    <div className="mt-0.5 font-mono text-[10.5px] text-muted-foreground">
                      {w.caption}
                    </div>
                  </div>
                  {i < WORKFLOW.length - 1 ? (
                    <ArrowRight
                      className="hidden size-3.5 shrink-0 text-muted-foreground sm:block"
                      aria-hidden
                    />
                  ) : null}
                </li>
              ))}
            </ol>
          </div>
        </section>

        {/* Final CTA */}
        <section className="border-t border-border">
          <div className="mx-auto max-w-6xl px-6 py-16 text-center">
            <Link
              to="/collections/$collectionId"
              params={{ collectionId: PRIMARY_COLLECTION }}
              search={{ tab: "register" }}
              className="group inline-flex items-center gap-2 text-[24px] font-semibold tracking-[-0.02em] text-foreground transition-colors hover:text-primary sm:text-[28px]"
            >
              Open Acme Vendor Agreements
              <ArrowRight
                className="size-5 transition-transform group-hover:translate-x-0.5"
                aria-hidden
              />
            </Link>
            <p className="mt-3 text-[13px] text-muted-foreground">
              Four documents. Six register rows. One unresolved supersession conflict.
            </p>
          </div>
        </section>
      </main>

      <footer className="border-t border-border">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-2 px-6 py-5">
          <span className="text-[12.5px] font-semibold tracking-tight text-foreground">Doctask</span>
          <span className="font-mono text-[11px] text-muted-foreground">
            {USE_MOCKS ? "mock fixtures" : "live REST backend"} · ruleset ab41c05d
          </span>
        </div>
      </footer>
    </div>
  );
}

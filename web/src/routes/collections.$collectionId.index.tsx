import { createFileRoute, Link } from "@tanstack/react-router";
import { useSuspenseQuery } from "@tanstack/react-query";
import { useState } from "react";
import type { PlaybookRule, RegisterItem } from "@/api/types";
import { collectionQuery, documentsQuery, registerQuery, runsQuery } from "@/api/queries";
import { AppShell, PageBody, PageBodyFlush, PageHeader } from "@/components/layout/AppShell";
import { RegisterTable } from "@/components/register/RegisterTable";
import { EvidenceInspector } from "@/components/register/EvidenceInspector";
import { PlaybookInspector, PlaybookTable } from "@/components/playbook/PlaybookTable";
import { RunsTable } from "@/components/runs/RunsTable";
import { Pill } from "@/components/doctask/badges";
import { Hash, Timestamp } from "@/components/doctask/primitives";
import { Row, SplitView, TableFrame, Thead, td, th } from "@/components/doctask/surfaces";
import { cn } from "@/lib/utils";

const TABS = [
  { id: "register", label: "Register" },
  { id: "runs", label: "Runs" },
  { id: "documents", label: "Documents" },
  { id: "playbook", label: "Playbook" },
] as const;

const TITLE = "Obligations register — Doctask";
const DESCRIPTION =
  "Every obligation value traced to an exact quote in a vendor contract, amendment or invoice.";

export const Route = createFileRoute("/collections/$collectionId/")({
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
      context.queryClient.ensureQueryData(registerQuery(params.collectionId)),
      context.queryClient.ensureQueryData(runsQuery(params.collectionId)),
      context.queryClient.ensureQueryData(documentsQuery(params.collectionId)),
    ]);
  },
  component: CollectionPage,
});

function CollectionPage() {
  const { collectionId } = Route.useParams();
  const { tab } = Route.useSearch();
  const { data: collection } = useSuspenseQuery(collectionQuery(collectionId));
  const { data: register } = useSuspenseQuery(registerQuery(collectionId));
  const { data: runs } = useSuspenseQuery(runsQuery(collectionId));
  const { data: documents } = useSuspenseQuery(documentsQuery(collectionId));

  const activeTab = tab ?? "register";
  const [selectedRow, setSelectedRow] = useState<RegisterItem | null>(register[0] ?? null);
  const [selectedRule, setSelectedRule] = useState<PlaybookRule | null>(
    collection.playbook[0] ?? null,
  );

  const disputed = register.filter((r) => r.state === "disputed").length;
  const ungrounded = register.filter(
    (r) => r.state === "unsupported" || r.state === "missing",
  ).length;

  const header = (
    <PageHeader
      breadcrumb={
        <>
          <Link to="/collections" className="hover:text-foreground">
            Collections
          </Link>
          <span>/</span>
          <span className="text-foreground">{collection.name}</span>
        </>
      }
      title={collection.name}
      meta={
        <>
          <span>
            <span className="num font-mono text-foreground">{register.length}</span> register rows
          </span>
          <span>
            <span className="num font-mono text-warning-foreground">{disputed}</span> disputed
          </span>
          <span>
            <span className="num font-mono text-danger-foreground">{ungrounded}</span> ungrounded
          </span>
          <span>
            <span className="num font-mono text-foreground">{documents.length}</span> documents
          </span>
        </>
      }
      tabs={
        <nav className="flex gap-1">
          {TABS.map((t) => (
            <Link
              key={t.id}
              to="/collections/$collectionId"
              params={{ collectionId }}
              search={{ tab: t.id }}
              className={cn(
                "-mb-px border-b-2 px-2.5 py-1.5 text-[12.5px] font-medium transition-colors",
                activeTab === t.id
                  ? "border-primary text-foreground"
                  : "border-transparent text-muted-foreground hover:text-foreground",
              )}
            >
              {t.label}
            </Link>
          ))}
        </nav>
      }
    />
  );

  if (activeTab === "register") {
    return (
      <AppShell>
        {header}
        <PageBodyFlush>
          <SplitView
            main={
              <RegisterTable
                items={register}
                selectedKey={selectedRow?.registerKey ?? null}
                onSelect={setSelectedRow}
              />
            }
            inspector={<EvidenceInspector item={selectedRow} documents={documents} />}
          />
        </PageBodyFlush>
      </AppShell>
    );
  }

  if (activeTab === "playbook") {
    return (
      <AppShell>
        {header}
        <PageBodyFlush>
          <SplitView
            main={
              <PlaybookTable
                rules={collection.playbook}
                scope={collectionId}
                selectedCode={selectedRule?.ruleCode ?? null}
                onSelect={setSelectedRule}
              />
            }
            inspector={
              <PlaybookInspector
                rule={selectedRule}
                scope={collectionId}
                rulesetVersion="ab41c05d"
              />
            }
            inspectorWidth="360px"
          />
        </PageBodyFlush>
      </AppShell>
    );
  }

  return (
    <AppShell>
      {header}
      <PageBody>
        {activeTab === "runs" ? <RunsTable runs={runs} collectionId={collectionId} /> : null}

        {activeTab === "documents" ? (
          <TableFrame>
            <Thead>
              <th className={th}>Filename</th>
              <th className={th}>Kind</th>
              <th className={`${th} text-right`}>Pages</th>
              <th className={th}>Content hash</th>
              <th className={th}>Ingested</th>
            </Thead>
            <tbody>
              {documents.map((d) => (
                <Row key={d.id}>
                  <td className={td}>
                    <Link
                      to="/collections/$collectionId/documents/$documentId"
                      params={{ collectionId, documentId: d.id }}
                      search={{ tab: "documents" }}
                      className="font-medium text-foreground hover:text-primary hover:underline"
                    >
                      {d.filename}
                    </Link>
                  </td>
                  <td className={td}>
                    <Pill tone="neutral">{d.kind}</Pill>
                  </td>
                  <td className={`${td} num text-right font-mono`}>{d.pages}</td>
                  <td className={td}>
                    <Hash value={d.contentHash} />
                  </td>
                  <td className={td}>
                    <Timestamp value={d.ingestedAt} />
                  </td>
                </Row>
              ))}
            </tbody>
          </TableFrame>
        ) : null}
      </PageBody>
    </AppShell>
  );
}

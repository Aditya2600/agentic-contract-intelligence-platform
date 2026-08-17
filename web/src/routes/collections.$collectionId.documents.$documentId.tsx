import { createFileRoute, Link } from "@tanstack/react-router";
import { useSuspenseQuery } from "@tanstack/react-query";
import { collectionQuery, documentQuery } from "@/api/queries";
import { AppShell, PageBodyFlush, PageHeader } from "@/components/layout/AppShell";
import { DocumentViewer } from "@/components/documents/DocumentViewer";
import { Pill } from "@/components/doctask/badges";
import { Hash, Timestamp } from "@/components/doctask/primitives";

export const Route = createFileRoute("/collections/$collectionId/documents/$documentId")({
  head: () => ({
    meta: [
      { title: "Document evidence viewer — Doctask" },
      {
        name: "description",
        content:
          "Verbatim source blocks with grounded extraction spans, withheld content and rule findings.",
      },
      { property: "og:title", content: "Document evidence viewer — Doctask" },
      {
        property: "og:description",
        content: "Verbatim source blocks with grounded extraction spans and rule findings.",
      },
    ],
  }),
  loader: async ({ context, params }) => {
    await Promise.all([
      context.queryClient.ensureQueryData(documentQuery(params.documentId)),
      context.queryClient.ensureQueryData(collectionQuery(params.collectionId)),
    ]);
  },
  component: DocumentPage,
});

function DocumentPage() {
  const { collectionId, documentId } = Route.useParams();
  const { data: doc } = useSuspenseQuery(documentQuery(documentId));
  const { data: collection } = useSuspenseQuery(collectionQuery(collectionId));

  const withheld = doc.blocks.filter((b) => b.withheld).length;

  return (
    <AppShell>
      <PageHeader
        breadcrumb={
          <>
            <Link to="/collections" className="hover:text-foreground">
              Collections
            </Link>
            <span>/</span>
            <Link
              to="/collections/$collectionId"
              params={{ collectionId }}
              search={{ tab: "documents" }}
              className="hover:text-foreground"
            >
              {collection.name}
            </Link>
            <span>/</span>
            <span className="font-mono">{doc.id}</span>
          </>
        }
        title={doc.filename}
        meta={
          <span className="flex flex-wrap items-center gap-3 text-[12px] text-muted-foreground">
            <Pill tone="neutral">{doc.kind}</Pill>
            <span>
              <span className="num font-mono text-foreground">{doc.pages}</span> pages
            </span>
            <span>
              <span className="num font-mono text-foreground">{doc.blocks.length}</span> blocks
            </span>
            {withheld > 0 ? (
              <span className="text-danger-foreground">
                <span className="num font-mono">{withheld}</span> withheld
              </span>
            ) : null}
            <span className="flex items-center gap-1.5">
              hash <Hash value={doc.contentHash} chars={16} />
            </span>
            <Timestamp value={doc.ingestedAt} />
          </span>
        }
      />
      <PageBodyFlush>
        <DocumentViewer doc={doc} />
      </PageBodyFlush>
    </AppShell>
  );
}

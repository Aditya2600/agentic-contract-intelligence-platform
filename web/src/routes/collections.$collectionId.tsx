import { createFileRoute, Outlet } from "@tanstack/react-router";
import { collectionQuery } from "@/api/queries";

const TABS = ["register", "runs", "documents", "playbook"] as const;
export type CollectionTab = (typeof TABS)[number];

interface CollectionSearch {
  tab?: CollectionTab;
}

export const Route = createFileRoute("/collections/$collectionId")({
  validateSearch: (search: Record<string, unknown>): CollectionSearch =>
    TABS.includes(search["tab"] as CollectionTab)
      ? { tab: search["tab"] as CollectionTab }
      : {},
  loader: async ({ context, params }) => {
    await context.queryClient.ensureQueryData(collectionQuery(params.collectionId));
  },
  component: () => <Outlet />,
});

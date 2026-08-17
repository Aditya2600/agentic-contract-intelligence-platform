import { createFileRoute, Outlet } from "@tanstack/react-router";
import { runEventsQuery, runQuery } from "@/api/queries";

export const Route = createFileRoute("/collections/$collectionId/runs/$runId")({
  loader: async ({ context, params }) => {
    await Promise.all([
      context.queryClient.ensureQueryData(runQuery(params.runId)),
      context.queryClient.ensureQueryData(runEventsQuery(params.runId)),
    ]);
  },
  component: () => <Outlet />,
});

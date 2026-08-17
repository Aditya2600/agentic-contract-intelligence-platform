import { createFileRoute } from "@tanstack/react-router";
import { Landing } from "@/components/landing/Landing";

const TITLE = "Doctask — evidence-backed obligations register";
const DESCRIPTION =
  "Doctask turns contracts, amendments, SOWs, policies and invoices into an evidence-backed obligations register while keeping humans in control of every durable change.";

export const Route = createFileRoute("/")({
  head: () => ({
    meta: [
      { title: TITLE },
      { name: "description", content: DESCRIPTION },
      { property: "og:title", content: TITLE },
      { property: "og:description", content: DESCRIPTION },
    ],
  }),
  component: Landing,
});

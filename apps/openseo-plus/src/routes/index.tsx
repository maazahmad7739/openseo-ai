import { createFileRoute, redirect } from "@tanstack/react-router";

export const Route = createFileRoute("/")({
  beforeLoad: () => {
    // Single-site demo: route straight into the operator for the fixture site.
    throw redirect({ to: "/p/$projectId/action-queue", params: { projectId: SITE_ID } });
  },
});

const SITE_ID = "c171d087-2264-4be8-b827-16fb663ba986";
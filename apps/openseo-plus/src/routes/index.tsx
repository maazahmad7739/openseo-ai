import { createFileRoute, redirect } from "@tanstack/react-router";

export const Route = createFileRoute("/")({
  beforeLoad: () => {
    // Single-site demo: route straight into the operator for the fixture site.
    throw redirect({ to: "/p/$projectId/action-queue", params: { projectId: SITE_ID } });
  },
});

const SITE_ID = "5b36849f-1a2b-4c3d-9e4f-0abcd1234ef8";
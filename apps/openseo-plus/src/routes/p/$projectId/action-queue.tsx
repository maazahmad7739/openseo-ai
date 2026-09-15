import { createFileRoute } from "@tanstack/react-router";
import { ActionQueuePage } from "@/features/operator/queue/ActionQueuePage";

export const Route = createFileRoute("/p/$projectId/action-queue")({
  component: () => {
    const { projectId } = Route.useParams();
    return <ActionQueuePage projectId={projectId} />;
  },
});
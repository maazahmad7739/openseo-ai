import { createFileRoute } from "@tanstack/react-router";
import { OffpageQueuePage } from "@/features/operator/offpage/OffpageQueuePage";

export const Route = createFileRoute("/p/$projectId/offpage-queue")({
  component: () => {
    const { projectId } = Route.useParams();
    return <OffpageQueuePage projectId={projectId} />;
  },
});
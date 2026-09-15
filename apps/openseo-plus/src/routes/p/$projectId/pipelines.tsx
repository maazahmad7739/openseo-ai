import { createFileRoute } from "@tanstack/react-router";
import { PipelinesPage } from "@/features/operator/pipelines/PipelinesPage";

export const Route = createFileRoute("/p/$projectId/pipelines")({
  component: () => {
    const { projectId } = Route.useParams();
    return <PipelinesPage projectId={projectId} />;
  },
});
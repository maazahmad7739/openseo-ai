import { createFileRoute } from "@tanstack/react-router";
import { ResultsPage } from "@/features/operator/results/ResultsPage";

export const Route = createFileRoute("/p/$projectId/results")({
  component: () => {
    const { projectId } = Route.useParams();
    return <ResultsPage projectId={projectId} />;
  },
});
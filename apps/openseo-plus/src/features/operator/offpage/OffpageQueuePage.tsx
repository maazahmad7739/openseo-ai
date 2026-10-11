import { MessageSquareText } from "lucide-react";
import { PageHeader, PageTitle } from "../components/PageHeader";
import { SkeletonCard } from "../components/Skeleton";
import { EmptyState } from "../components/EmptyState";
import { useOffpageQueue } from "./data";
import { OffpageTaskCard } from "./OffpageTaskCard";
import { OffpageSettingsPanel } from "./OffpageSettingsPanel";

export function OffpageQueuePage({ projectId }: { projectId: string }) {
  const { queue, isBusy, choose, skip, publish } = useOffpageQueue(projectId);

  if (queue.isError) {
    return (
      <div className="app-main">
        <div className="alert alert-error">Could not load the off-page queue.</div>
      </div>
    );
  }

  if (queue.isPending || !queue.data) {
    return (
      <div className="app-main flex flex-col gap-5">
        <PageHeader projectId={projectId} title="Off-page Queue" />
        <div className="skeleton h-8 w-72" />
        <SkeletonCard lines={5} />
        <SkeletonCard lines={5} />
      </div>
    );
  }

  const { data } = queue;
  const pendingCards = data.items;

  return (
    <div className="app-main flex flex-col gap-6">
      <PageHeader projectId={projectId} title="Off-page Queue" />

      <PageTitle
        title="Off-page Queue"
        subtitle="Review drafted community replies, copy the right segment, and submit the live permalink"
      />

      <div className="flex flex-wrap items-center justify-between gap-2 text-sm text-base-content/60">
        <p>
          <span className="font-semibold text-base-content">
            {data.total_active}
          </span>{" "}
          of {data.active_cap} active draft
          {data.total_active === 1 ? "" : "s"}
          {data.total_pending > data.total_active ? (
            <>
              {" "}
              · <span className="text-base-content/70">{data.total_pending}</span>{" "}
              pending at discovery
            </>
          ) : null}
        </p>
        <p className="text-xs text-base-content/45">
          Publishing is strictly manual — the sweep only verifies pasted
          permalinks (T+15m · T+2h · T+24h · T+72h)
        </p>
      </div>

      {pendingCards.length === 0 ? (
        <EmptyState
          icon={MessageSquareText}
          title="No off-page tasks in the queue"
          body="Discovery drafts eligible replies from community threads matching the client's target keywords. Run the live off-page pipeline to mint new drafts."
        />
      ) : (
        <div className="flex flex-col gap-4">
          {pendingCards.map((item) => (
            <OffpageTaskCard
              key={item.draft_id}
              item={item}
              busy={isBusy}
              onChoose={choose}
              onSkip={skip}
              onPublish={publish}
            />
          ))}
        </div>
      )}

      <details className="mt-2">
        <summary className="cursor-pointer list-none text-[11px] font-medium text-base-content/50">
          Platform channels &amp; discovery settings
        </summary>
        <div className="mt-3">
          <OffpageSettingsPanel projectId={projectId} />
        </div>
      </details>
    </div>
  );
}
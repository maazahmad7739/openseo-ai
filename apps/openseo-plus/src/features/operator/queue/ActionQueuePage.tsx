import { useMemo, useState } from "react";
import { toast } from "sonner";
import { Inbox, X } from "lucide-react";
import { sort } from "remeda";
import {
  EMPTY_RECS,
  EMPTY_STATS,
  useOperatorData,
} from "../data/useOperatorData";
import type {
  ImpactLevel,
  Recommendation,
  QueueSortKey,
} from "../data/types";
import { PageHeader, PageTitle } from "../components/PageHeader";
import { SkeletonCard } from "../components/Skeleton";
import { EmptyState } from "../components/EmptyState";
import { DetailDrawer } from "../components/DetailDrawer";
import { RecommendationCard } from "./RecommendationCard";
import {
  QueueBulkBar,
  QueueSummary,
  QueueToolbar,
} from "./QueueControls";
import type {
  ActionTypeFilter,
  EffortFilter,
  GeneratorFilter,
  ImpactFilter,
  OwnerFilter,
  StatusFilter,
} from "./QueueControls";
import { generatorMeta } from "../components/meta";

const IMPACT_ORDER: Record<ImpactLevel, number> = { high: 0, medium: 1, low: 2 };

const MAX_QUEUE_SIZE = 5;

export function ActionQueuePage({ projectId }: { projectId: string }) {
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState<StatusFilter>("all");
  const [generator, setGenerator] = useState<GeneratorFilter>("all");
  const [owner, setOwner] = useState<OwnerFilter>("all");
  const [actionType, setActionType] = useState<ActionTypeFilter>("all");
  const [impact, setImpact] = useState<ImpactFilter>("all");
  const [effort, setEffort] = useState<EffortFilter>("all");
  const [sortKey, setSortKey] = useState<QueueSortKey>("impact");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [detailId, setDetailId] = useState<string | null>(null);

  const data = useOperatorData(projectId);

  const rows = data.data?.recommendations ?? EMPTY_RECS;
  const stats = data.data?.stats ?? EMPTY_STATS;

  const filtered = useMemo(() => {
    const term = query.trim().toLowerCase();
    const list = rows.filter((rec) => {
      // Exclusivity: the Action Queue IS the proposed backlog. Approved cards
      // live only in the Pipeline's APPROVED column — never here.
      if (rec.status !== "proposed") return false;
      if (status !== "all" && rec.status !== status) return false;
      if (generator !== "all" && rec.generator !== generator) return false;
      if (owner !== "all" && rec.owner.toLowerCase() !== owner) return false;
      if (actionType !== "all" && rec.action_type !== actionType) return false;
      if (impact !== "all" && rec.impact !== impact) return false;
      if (effort !== "all" && rec.effort !== effort) return false;
      if (term) {
        const haystack = [
          rec.diagnosis,
          rec.target_url ?? "",
          rec.proposed_url ?? "",
          rec.recommendation_id,
        ]
          .join(" ")
          .toLowerCase();
        if (!haystack.includes(term)) return false;
      }
      return true;
    });

    let sorted = list;
    switch (sortKey) {
      case "impact":
        sorted = sort(
          list,
          (a, b) =>
            IMPACT_ORDER[a.impact] - IMPACT_ORDER[b.impact] ||
            new Date(b.created_at).getTime() - new Date(a.created_at).getTime(),
        );
        break;
      case "volume":
        sorted = sort(list, (a, b) => {
          const av = stats[a.recommendation_id]?.volume ?? 0;
          const bv = stats[b.recommendation_id]?.volume ?? 0;
          return bv - av;
        });
        break;
      case "generator":
        sorted = sort(list, (a, b) =>
          generatorMeta[a.generator].label.localeCompare(
            generatorMeta[b.generator].label,
          ),
        );
        break;
      case "created_at":
        sorted = sort(
          list,
          (a, b) =>
            new Date(b.created_at).getTime() - new Date(a.created_at).getTime(),
        );
        break;
    }
    return sorted;
  }, [rows, stats, query, status, generator, owner, actionType, impact, effort, sortKey]);

  // The queue surfaces the top candidate set only (5 per weekly run, per the
  // product spec). Filters still browse the full backlog when narrowed.
  const visible = filtered.slice(0, MAX_QUEUE_SIZE);
  const queueTotal = filtered.length;

  const selectedRowCount = useMemo(
    () => rows.filter((r) => selected.has(r.recommendation_id)).length,
    [rows, selected],
  );

const hasFilters =
    query.trim() !== "" ||
    status !== "all" ||
    generator !== "all" ||
    owner !== "all" ||
    actionType !== "all" ||
    impact !== "all" ||
    effort !== "all";

  const clearFilters = () => {
    setQuery("");
    setStatus("all");
    setGenerator("all");
    setOwner("all");
    setActionType("all");
    setImpact("all");
    setEffort("all");
  };

  if (data.isError) {
    return (
      <div className="app-main">
        <div className="alert alert-error">Could not load the action queue.</div>
      </div>
    );
  }

  if (data.isPending || !data.data) {
    return (
      <div className="app-main flex flex-col gap-5">
        <div className="skeleton h-8 w-56" />
        <div className="skeleton h-14 w-full" />
        <div className="grid gap-4 lg:grid-cols-2">
          <SkeletonCard />
          <SkeletonCard />
        </div>
        <SkeletonCard lines={4} />
      </div>
    );
  }

  const filteredIds = visible.map((r) => r.recommendation_id);
  const allSelected =
    visible.length > 0 &&
    filteredIds.every((id) => selected.has(id)) &&
    visible.every((r) => r.status === "proposed");

  const toggleSelect = (id: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const toggleAll = () =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (allSelected) {
        filteredIds.forEach((id) => next.delete(id));
      } else {
        visible
          .filter((r) => r.status === "proposed")
          .forEach((r) => next.add(r.recommendation_id));
      }
      return next;
    });

  const approve = (rec: Recommendation) => {
    data.approve(rec.recommendation_id);
    setSelected((prev) => {
      const next = new Set(prev);
      next.delete(rec.recommendation_id);
      return next;
    });
    toast.success(`${rec.target_url ?? rec.proposed_url} approved`);
  };

  const reject = (rec: Recommendation, reason?: string) => {
    data.reject(rec.recommendation_id, reason);
    setSelected((prev) => {
      const next = new Set(prev);
      next.delete(rec.recommendation_id);
      return next;
    });
    toast.error(`${rec.target_url ?? rec.proposed_url} rejected`);
  };

  const bulkApprove = () => {
    const targets = rows.filter(
      (r) => selected.has(r.recommendation_id) && r.status === "proposed",
    );
    targets.forEach((rec) => approve(rec));
    toast.success(`Approved ${targets.length} recommendation${targets.length === 1 ? "" : "s"}`);
  };

  const bulkReject = () => {
    const targets = rows.filter(
      (r) =>
        selected.has(r.recommendation_id) &&
        (r.status === "proposed" || r.status === "approved"),
    );
    targets.forEach((rec) => reject(rec));
    toast.error(`Rejected ${targets.length} recommendation${targets.length === 1 ? "" : "s"}`);
  };

  return (
    <div className="app-main flex flex-col gap-6">
      <PageHeader projectId={projectId} title="Action Queue" />

      <PageTitle
        title="Action Queue"
        subtitle="Review, approve, and deprioritize weekly candidate recommendations"
      />

      <QueueToolbar
        query={query}
        onQueryChange={setQuery}
        status={status}
        onStatusChange={setStatus}
        generator={generator}
        onGeneratorChange={setGenerator}
        owner={owner}
        onOwnerChange={setOwner}
        actionType={actionType}
        onActionTypeChange={setActionType}
        impact={impact}
        onImpactChange={setImpact}
        effort={effort}
        onEffortChange={setEffort}
        sortKey={sortKey}
        onSortKeyChange={setSortKey}
      />

      <QueueSummary
        count={visible.length}
        total={queueTotal}
        hasFilters={hasFilters}
        allSelected={allSelected}
        hasProposed={visible.some((r) => r.status === "proposed")}
        onToggleAll={toggleAll}
        onReset={clearFilters}
      />

      {filtered.length === 0 ? (
        <EmptyState
          icon={Inbox}
          title={query.trim() ? "No recommendations match your search" : "No recommendations here"}
          kind={query.trim() ? "search" : "filters"}
          body={
            hasFilters
              ? "Nothing in the queue matches the current search and filters."
              : "The weekly candidate run only generates recommendations when there is something worth doing. Run it again after the next data sync."
          }
          action={
            hasFilters ? (
              <button
                type="button"
                className="btn btn-primary btn-sm"
                onClick={clearFilters}
              >
                <X className="size-3.5" />
                Clear filters
              </button>
            ) : undefined
          }
        />
      ) : (
        <div className="grid gap-4">
          {visible.map((rec) => (
            <RecommendationCard
              key={rec.recommendation_id}
              rec={rec}
              stats={stats[rec.recommendation_id]}
              selected={selected.has(rec.recommendation_id)}
              expanded={expandedId === rec.recommendation_id}
              domain={null}
              onOpenDetail={() => setDetailId(rec.recommendation_id)}
              onToggleSelect={() => toggleSelect(rec.recommendation_id)}
              onToggleExpand={() =>
                setExpandedId((current) =>
                  current === rec.recommendation_id ? null : rec.recommendation_id,
                )
              }
              onApprove={() => approve(rec)}
              onReject={(reason) => reject(rec, reason)}
            />
          ))}
        </div>
      )}

      {selectedRowCount > 0 ? (
        <QueueBulkBar
          count={selectedRowCount}
          onClear={() => setSelected(new Set())}
          onReject={bulkReject}
          onApprove={bulkApprove}
        />
      ) : null}

      <DetailDrawer
        recommendationId={detailId}
        onClose={() => setDetailId(null)}
        projectId={projectId}
      />
    </div>
  );
}
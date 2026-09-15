import { useMemo, useState } from "react";
import {
  AlertTriangle,
  ArrowLeftRight,
  ChartNoAxesCombined,
  CheckCircle2,
  ChevronRight,
  Table2,
  Trophy,
} from "lucide-react";
import { EMPTY_MEASURED, EMPTY_RECS, useOperatorData } from "../data/useOperatorData";
import type { MeasuredResult } from "../data/types";
import { PageHeader, PageTitle } from "../components/PageHeader";
import { KpiCard } from "../components/KpiCard";
import { ResultBadge, GeneratorBadge } from "../components/Badge";
import { OutcomeTrendChart } from "./OutcomeTrendChart";
import {
  BreakdownTable,
  ResultCountChips,
} from "./BreakdownTable";
import { ResultDetailModal } from "./ResultDetailModal";
import { EmptyState } from "../components/EmptyState";
import { SkeletonStrip, SkeletonCard } from "../components/Skeleton";

export function ResultsPage({ projectId }: { projectId: string }) {
  const data = useOperatorData(projectId);
  const recs = data.data?.recommendations ?? EMPTY_RECS;
  const measured = data.data?.measured ?? EMPTY_MEASURED;

  const [selectedResult, setSelectedResult] = useState<MeasuredResult | null>(
    null,
  );
  const [bucketFilter, setBucketFilter] = useState<string | null>(null);

  const metrics = useMemo(() => {
    const total = recs.length || 0;
    const nonRejected = recs.filter((r) => r.status !== "rejected").length;
    const acceptedOrBeyond = recs.filter(
      (r) =>
        r.status !== "rejected" &&
        r.status !== "proposed",
    ).length;
    const implemented = recs.filter(
      (r) =>
        r.status !== "rejected" &&
        r.implemented_at != null &&
        (r.status === "in_progress" || r.status === "live" || r.status === "measured"),
    ).length;
    const won = measured.filter((r) => r.result === "won").length;
    const canMeasure = measured.length || 0;

    const acceptance = nonRejected ? acceptedOrBeyond / nonRejected : 0;
    const implementation = acceptedOrBeyond ? implemented / acceptedOrBeyond : 0;
    const improvement = canMeasure ? won / canMeasure : 0;
    return {
      total,
      acceptance,
      implementation,
      improvement,
      mvpScore: acceptance * implementation * improvement,
      measured: canMeasure,
      won,
      accepted: acceptedOrBeyond,
    };
  }, [recs, measured]);

  const lost = useMemo(
    () => measured.filter((r) => r.result === "lost"),
    [measured],
  );

  const filteredMeasured = useMemo(
    () =>
      bucketFilter
        ? measured.filter(
            (r) =>
              `${r.generator}::${r.action_type}` === bucketFilter,
          )
        : measured,
    [measured, bucketFilter],
  );

  if (data.isError) {
    return (
      <div className="app-main">
        <div className="alert alert-error">Could not load results.</div>
      </div>
    );
  }

  if (data.isPending || !data.data) {
    return (
      <div className="app-main flex flex-col gap-5">
        <div className="skeleton h-8 w-56" />
        <SkeletonStrip />
        <div className="app-panel">
          <div className="p-5">
            <div className="skeleton h-44 w-full" />
          </div>
        </div>
        <div className="grid gap-4 lg:grid-cols-2">
          <SkeletonCard lines={5} />
          <SkeletonCard lines={5} />
        </div>
      </div>
    );
  }

  return (
    <div className="app-main flex flex-col gap-6">
      <PageHeader projectId={projectId} title="Results Dashboard" />

      <PageTitle
        title="Results Dashboard"
        subtitle="Measure the impact of approved recommendations"
      />

      {/* KPIs */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <KpiCard
          label="Acceptance rate"
          value={pct(metrics.acceptance)}
          sub={`${metrics.accepted} of ${metrics.total} proposed`}
          icon={<CheckCircle2 className="size-4.5" />}
        />
        <KpiCard
          label="Implementation rate"
          value={pct(metrics.implementation)}
          sub="implemented vs approved"
          icon={<ArrowLeftRight className="size-4.5" />}
        />
        <KpiCard
          label="Improvement rate"
          value={pct(metrics.improvement)}
          sub={`${metrics.won} won of ${metrics.measured} measured`}
          icon={<ChartNoAxesCombined className="size-4.5" />}
          tone={metrics.improvement >= 0.5 ? "good" : metrics.measured > 0 && metrics.improvement < 0.34 ? "bad" : "neutral"}
        />
        <KpiCard
          label="MVP score"
          value={pct(metrics.mvpScore)}
          sub="acceptance × implementation × improvement"
          icon={<Trophy className="size-4.5" />}
          tone={metrics.mvpScore >= 0.3 ? "good" : "neutral"}
        />
      </div>

      <ResultCountChips results={measured} />

      {/* Trend chart */}
      <div className="app-panel">
        <div className="app-panel-header">
          <ChartNoAxesCombined className="size-4 text-base-content/40" />
          Win / neutral / lost over time
        </div>
        <div className="p-5">
          <OutcomeTrendChart results={measured} />
        </div>
      </div>

      {/* Lost cases surfaced prominently */}
      {lost.length > 0 ? (
        <div className="rounded-2xl border border-error/30 bg-error/5 p-5">
          <div className="flex items-center gap-2 text-sm font-semibold text-error">
            <AlertTriangle className="size-4" />
            {lost.length} lost result{lost.length === 1 ? "" : "s"} need review
          </div>
          <ul className="mt-3 space-y-2">
            {lost.map((r) => (
              <li key={r.recommendation_id}>
                <button
                  type="button"
                  onClick={() => setSelectedResult(r)}
                  className="flex w-full items-center justify-between gap-3 rounded-lg border border-error/20 bg-base-100 px-3.5 py-2.5 text-left transition-colors hover:bg-error/5"
                >
                  <span className="min-w-0">
                    <span className="block truncate font-mono text-sm font-medium">
                      {r.target_url}
                    </span>
                    <span className="block text-xs text-base-content/55">
                      {r.summary}
                    </span>
                  </span>
                  <span className="flex shrink-0 items-center gap-2">
                    <span className="rounded bg-error/10 px-2 py-0.5 text-xs font-semibold tabular-nums text-error">
                      {r.relative_change != null
                        ? `−${Math.abs(r.relative_change).toFixed(1)}%`
                        : "lost"}
                    </span>
                    <ChevronRight className="size-4 text-base-content/30" />
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {/* Breakdown by generator × action type */}
      <div className="app-panel">
        <div className="app-panel-header">
          <Table2 className="size-4 text-base-content/40" />
          Improvement rate by generator &amp; action type
        </div>
        <div className="p-5 pt-4">
          <BreakdownTable
            results={measured}
            onDrill={(id) => {
              setBucketFilter((current) => (current === id ? null : id));
            }}
          />
        </div>
      </div>

      {/* All measured results with drill-down */}
      <div className="app-panel">
        <div className="app-panel-header justify-between">
          <span className="flex items-center gap-2">
            <ArrowLeftRight className="size-4 text-base-content/40" />
            Measured results
          </span>
          {bucketFilter ? (
            <button
              type="button"
              className="btn btn-ghost btn-xs"
              onClick={() => setBucketFilter(null)}
            >
              Clear bucket filter
            </button>
          ) : null}
        </div>
        <div className="p-5 pt-4">
          {filteredMeasured.length === 0 ? (
            <EmptyState
              icon={ArrowLeftRight}
              title="No measured results"
              kind="none"
              body="Recommendations surface here once their observation window closes and the measurement module classifies the outcome."
            />
          ) : (
            <div className="divide-y divide-base-200 overflow-hidden rounded-xl border border-base-300">
              {filteredMeasured.map((r) => (
                <div
                  key={r.recommendation_id}
                  className="flex flex-col gap-2 bg-base-100 px-4 py-3 sm:flex-row sm:items-center sm:justify-between"
                >
                  <div className="min-w-0">
                    <p className="truncate font-mono text-[13px] font-medium">
                      {r.target_url}
                    </p>
                    <div className="mt-1 flex flex-wrap items-center gap-1.5">
                      <GeneratorBadge generator={r.generator} />
                      <ResultBadge result={r.result} />
                      <span className="text-xs tabular-nums text-base-content/50">
                        deltas ({r.delta >= 0 ? "+" : "−"}
                        {Math.abs(r.delta)} ·{" "}
                        {r.relative_change != null
                          ? `${r.relative_change >= 0 ? "+" : "−"}${Math.abs(r.relative_change).toFixed(1)}%`
                          : "—"}
                        )
                      </span>
                    </div>
                  </div>
                  <div className="flex shrink-0 items-center gap-2">
                    {r.significance != null ? (
                      <span
                        className={`text-xs tabular-nums ${
                          r.significance >= 0.95 ? "text-success" : "text-base-content/45"
                        }`}
                      >
                        sig {Math.round(r.significance * 100)}%
                      </span>
                    ) : null}
                    <button
                      type="button"
                      className="btn btn-outline btn-xs"
                      onClick={() => setSelectedResult(r)}
                    >
                      View before/after
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {selectedResult ? (
        <ResultDetailModal
          result={selectedResult}
          onClose={() => setSelectedResult(null)}
        />
      ) : null}
    </div>
  );
}

function pct(n: number) {
  return `${Math.round(n * 100)}%`;
}
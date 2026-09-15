import { useMemo, useState } from "react";
import { toast } from "sonner";
import {
  Activity,
  CheckCircle2,
  CircleSlash,
  Loader2,
} from "lucide-react";
import { EMPTY_RECS, EMPTY_RUNS, useOperatorData } from "../data/useOperatorData";
import { PageHeader, PageTitle } from "../components/PageHeader";
import { KpiCard } from "../components/KpiCard";
import { DetailDrawer } from "../components/DetailDrawer";
import { SkeletonCard, SkeletonStrip } from "../components/Skeleton";
import { PipelineCard, PipelineColumn, RawCandidatesCard } from "./PipelineCard";
import type { CandidateRun } from "../data/types";

const RUN_META: Record<
  CandidateRun["run_type"],
  { label: string }
> = {
  daily_sync: { label: "Daily sync" },
  candidate_generation: { label: "Candidate generation" },
  agent_evaluation: { label: "Agent evaluation" },
  measurement: { label: "Measurement" },
};

/** Horizontal kanban of the whole recommendation flow. The raw stage is
 * represented by the latest candidate run (candidates are not yet rows in the
 * queue), then proposed → approved → in progress (incl. live) → measured. */
export function PipelinesPage({ projectId }: { projectId: string }) {
  const data = useOperatorData(projectId);
  const rows = data.data?.recommendations ?? EMPTY_RECS;
  const runs = data.data?.runs ?? EMPTY_RUNS;

  const proposedCount = useMemo(
    () => rows.filter((r) => r.status === "proposed").length,
    [rows],
  );

  const byStage = useMemo(
    () => ({
      proposed: rows.filter((r) => r.status === "proposed"),
      approved: rows.filter((r) => r.status === "approved"),
      inProgress: rows.filter(
        (r) => r.status === "in_progress" || r.status === "live",
      ),
      measured: rows.filter((r) => r.status === "measured"),
    }),
    [rows],
  );

  const inObservation = byStage.inProgress.length;
  const [detailId, setDetailId] = useState<string | null>(null);

  if (data.isError) {
    return (
      <div className="app-main">
        <div className="alert alert-error">Could not load the pipeline.</div>
      </div>
    );
  }

  if (data.isPending || !data.data) {
    return (
      <div className="app-main flex flex-col gap-5">
        <div className="skeleton h-8 w-56" />
        <SkeletonStrip />
        <div className="flex gap-4 overflow-hidden">
          <SkeletonCard className="w-64 shrink-0" lines={2} />
          <SkeletonCard className="w-64 shrink-0" lines={2} />
          <SkeletonCard className="w-64 shrink-0" lines={2} />
        </div>
      </div>
    );
  }

  return (
    <div className="app-main flex flex-col gap-6">
      <PageHeader projectId={projectId} title="Pipeline" />

      <PageTitle
        title="Pipeline"
        subtitle="Follow recommendations from raw candidate to measured outcome"
      />

      {/* Pipeline health KPIs */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <KpiCard
          label="In observation"
          value={inObservation}
          sub="awaiting measurement"
          icon={<Activity className="size-4.5" />}
          tone={inObservation > 0 ? "good" : "neutral"}
        />
        <KpiCard
          label="Proposed"
          value={byStage.proposed.length}
          sub="awaiting operator decision"
        />
        <KpiCard
          label="Approved"
          value={byStage.approved.length}
          sub="committed to implementation"
        />
        <KpiCard
          label="Measured"
          value={byStage.measured.length}
          sub="outcomes classified"
        />
      </div>

      {/* Recent run strip */}
      {runs.length > 0 ? (
        <div className="grid gap-4 sm:grid-cols-3">
          {runs.slice(0, 3).map((run) => (
            <RunStatusCard key={run.run_id} run={run} />
          ))}
        </div>
      ) : null}

      {/* Kanban board */}
      <div className="app-panel overflow-hidden">
        <div className="overflow-x-auto p-4">
          <div className="flex gap-4 pb-1">
            <PipelineColumn
              title="Raw"
              accentColor="bg-base-content/30"
              count={0}
            >
              <RawCandidatesCard proposedCount={proposedCount} />
            </PipelineColumn>

            <PipelineColumn
              title="Proposed"
              accentColor="bg-violet-400"
              count={byStage.proposed.length}
            >
              {byStage.proposed.length === 0 ? (
                <ColumnEmpty text="No proposed recommendations" />
              ) : (
                byStage.proposed.map((rec) => (
                  <PipelineCard
                    key={rec.recommendation_id}
                    rec={rec}
                    onOpenDetail={setDetailId}
                  />
                ))
              )}
            </PipelineColumn>

            <PipelineColumn
              title="Approved"
              accentColor="bg-emerald-500"
              count={byStage.approved.length}
            >
              {byStage.approved.length === 0 ? (
                <ColumnEmpty text="Nothing approved yet" />
              ) : (
                byStage.approved.map((rec) => (
                  <PipelineCard
                    key={rec.recommendation_id}
                    rec={rec}
                    onOpenDetail={setDetailId}
                    onImplement={(id) => {
                      data.implement(id);
                      toast.success("Marked as implemented — baseline captured, observation window started");
                    }}
                  />
                ))
              )}
            </PipelineColumn>

            <PipelineColumn
              title="In progress"
              accentColor="bg-sky-500"
              count={byStage.inProgress.length}
            >
              {byStage.inProgress.length === 0 ? (
                <ColumnEmpty text="Nothing being implemented" />
              ) : (
                byStage.inProgress.map((rec) => (
                  <PipelineCard
                    key={rec.recommendation_id}
                    rec={rec}
                    onOpenDetail={setDetailId}
                    onMarkLive={(id) => {
                      data.markLive(id);
                      toast.success("Marked live — outcome will be measured at window close");
                    }}
                  />
                ))
              )}
            </PipelineColumn>

            <PipelineColumn
              title="Measured"
              accentColor="bg-slate-400"
              count={byStage.measured.length}
            >
              {byStage.measured.length === 0 ? (
                <ColumnEmpty text="No measured outcomes yet" />
              ) : (
                byStage.measured.map((rec) => (
                  <PipelineCard
                    key={rec.recommendation_id}
                    rec={rec}
                    onOpenDetail={setDetailId}
                  />
                ))
              )}
            </PipelineColumn>
          </div>
        </div>
      </div>

      <DetailDrawer
        recommendationId={detailId}
        onClose={() => setDetailId(null)}
      />
    </div>
  );
}

function ColumnEmpty({ text }: { text: string }) {
  return (
    <div className="flex min-h-32 flex-1 items-center justify-center rounded-xl border border-dashed border-base-300/80 bg-base-200/40 px-3 py-8">
      <p className="text-center text-xs text-base-content/45">{text}</p>
    </div>
  );
}

function RunStatusCard({ run }: { run: CandidateRun }) {
  const meta = RUN_META[run.run_type];
  const status = run.status;
  return (
    <div className="app-kpi">
      <div className="flex items-center justify-between gap-2">
        <p className="app-kpi-label">{meta.label}</p>
        {status === "completed" ? (
          <CheckCircle2 className="size-4 text-success" />
        ) : status === "running" ? (
          <Loader2 className="size-4 animate-spin text-info" />
        ) : status === "failed" ? (
          <CircleSlash className="size-4 text-error" />
        ) : (
          <Activity className="size-4 text-base-content/40" />
        )}
      </div>
      <p className="app-kpi-value text-base">
        {status === "completed"
          ? "Completed"
          : status === "running"
            ? "Running"
            : status === "failed"
              ? "Failed"
              : "Pending"}
      </p>
      {run.note ? (
        <p className="app-kpi-sub">{run.note}</p>
      ) : null}
    </div>
  );
}
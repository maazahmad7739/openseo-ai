import { useMemo } from "react";
import {
  Activity,
  CheckCircle2,
  CircleSlash,
  Loader2,
} from "lucide-react";
import { EMPTY_RECS, EMPTY_RUNS, useOperatorData } from "../data/useOperatorData";
import { PageHeader } from "../components/PageHeader";
import { SkeletonCard } from "../components/Skeleton";
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

  const inObservation = useMemo(
    () =>
      byStage.inProgress.filter(
        (r) =>
          r.status === "in_progress" || r.status === "live",
      ).length,
    [byStage.inProgress],
  );

  if (data.isError) {
    return (
      <div className="px-4 py-4 md:px-6 md:py-6">
        <div className="alert alert-error">Could not load the pipeline.</div>
      </div>
    );
  }

  if (data.isPending || !data.data) {
    return (
      <div className="mx-auto flex max-w-6xl flex-col gap-5 px-4 py-4 md:px-6 md:py-6">
        <div className="skeleton h-7 w-56" />
        <div className="skeleton h-24 w-full" />
        <div className="flex gap-4 overflow-hidden">
          <SkeletonCard className="w-64 shrink-0" lines={2} />
          <SkeletonCard className="w-64 shrink-0" lines={2} />
          <SkeletonCard className="w-64 shrink-0" lines={2} />
        </div>
      </div>
    );
  }

  return (
    <div className="px-4 py-4 md:px-6 md:py-6 pb-24 md:pb-8">
      <div className="mx-auto flex max-w-6xl flex-col gap-5">
        <PageHeader
          projectId={projectId}
          title="Pipeline"
          subtitle="Follow recommendations from raw candidate to measured outcome"
        />

        {/* Pipeline health strip */}
        <PipelineRunsStrip runs={runs} inObservation={inObservation} />

        {/* Kanban board */}
        <div className="overflow-x-auto rounded-xl border border-base-200 bg-base-200/30 p-3">
          <div className="flex gap-3 pb-1">
            <PipelineColumn
              title="Raw"
              accentColor="bg-base-content/30"
              count={61}
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
                  <PipelineCard key={rec.recommendation_id} rec={rec} />
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
                  <PipelineCard key={rec.recommendation_id} rec={rec} />
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
                  <PipelineCard key={rec.recommendation_id} rec={rec} />
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
                  <PipelineCard key={rec.recommendation_id} rec={rec} />
                ))
              )}
            </PipelineColumn>
          </div>
        </div>
      </div>
    </div>
  );
}

function ColumnEmpty({ text }: { text: string }) {
  return (
    <div className="flex flex-1 items-center justify-center rounded-lg border border-dashed border-base-300 py-8">
      <p className="text-xs text-base-content/45">{text}</p>
    </div>
  );
}

/** Recent automated pipeline runs + how many items are in an observation
 * window, so a human can see at a glance whether the system is healthy. */
function PipelineRunsStrip({
  runs,
  inObservation,
}: {
  runs: CandidateRun[];
  inObservation: number;
}) {
  return (
    <div className="grid gap-px overflow-hidden rounded-lg border border-base-300 bg-base-300/70 lg:grid-cols-5">
      <div className="col-span-2 bg-base-100 px-4 py-3">
        <p className="text-[11px] uppercase tracking-wider text-base-content/50">
          In observation window
        </p>
        <p className="mt-0.5 flex items-center gap-2 text-xl font-semibold tabular-nums">
          {inObservation}
          <span className="text-xs font-normal text-base-content/50">
            awaiting measurement
          </span>
        </p>
      </div>

      {runs.slice(0, 3).map((run) => {
        const meta = RUN_META[run.run_type];
        const status = run.status;
        return (
          <div key={run.run_id} className="bg-base-100 px-4 py-3">
            <p className="text-[11px] uppercase tracking-wider text-base-content/50">
              {meta.label}
            </p>
            <p className="mt-0.5 flex items-center gap-1.5 text-sm font-medium">
              {status === "completed" ? (
                <CheckCircle2 className="size-4 text-success" />
              ) : status === "running" ? (
                <Loader2 className="size-4 animate-spin text-info" />
              ) : status === "failed" ? (
                <CircleSlash className="size-4 text-error" />
              ) : (
                <Activity className="size-4 text-base-content/40" />
              )}
              {status === "completed"
                ? "Completed"
                : status === "running"
                  ? "Running"
                  : status === "failed"
                    ? "Failed"
                    : "Pending"}
            </p>
            {run.note ? (
              <p className="mt-0.5 truncate text-[11px] text-base-content/45">
                {run.note}
              </p>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}
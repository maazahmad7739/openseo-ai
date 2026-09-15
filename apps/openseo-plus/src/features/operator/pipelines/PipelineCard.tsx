import {
  CalendarClock,
  ChevronRight,
  TimerReset,
} from "lucide-react";
import type { Recommendation } from "../data/types";
import {
  GeneratorBadge,
  ImpactBadge,
  OwnerBadgeSolid,
  StatusBadge,
} from "../components/Badge";
import { actionTypeMeta } from "../components/meta";

export function daysUntil(iso: string | null): number | null {
  if (!iso) return null;
  return Math.round(
    (new Date(iso).getTime() - Date.now()) / 86_400_000,
  );
}

/** Countdown chip for an item in its observation window. Green when healthy,
 * amber when it closes soon, red once overdue. */
export function DueCountdown({ dueAt }: { dueAt: string | null }) {
  const days = daysUntil(dueAt);
  if (days == null) return null;
  const overdue = days < 0;
  const soon = days <= 7 && !overdue;
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium tabular-nums ring-1 ring-inset ${
        overdue
          ? "tag-chip-rose"
          : soon
            ? "tag-chip-amber"
            : "tag-chip-emerald"
      }`}
      title={
        dueAt
          ? `Measurement due ${new Date(dueAt).toLocaleDateString()}`
          : undefined
      }
    >
      {overdue ? (
        <TimerReset className="size-3" />
      ) : (
        <CalendarClock className="size-3" />
      )}
      {overdue
        ? `Overdue ${Math.abs(days)}d`
        : days === 0
          ? "Due today"
          : `Due in ${days}d`}
    </span>
  );
}

/** Slug label from a full URL — the readable part of a long URL. */
function urlLabel(url: string | null): string {
  if (!url) return "—";
  return url.split("/").filter(Boolean).pop() || url;
}

export function PipelineCard({ rec }: { rec: Recommendation }) {
  const url = rec.target_url ?? rec.proposed_url;
  return (
    <div className="group rounded-xl border border-base-300 bg-base-100 p-3.5 shadow-sm transition-all hover:-translate-y-0.5 hover:shadow-md">
      <div className="flex items-center justify-between gap-2">
        <GeneratorBadge generator={rec.generator} />
        <ImpactBadge impact={rec.impact} />
      </div>
      <p className="mt-2.5 line-clamp-2 break-all font-mono text-xs font-medium leading-snug text-base-content">
        {urlLabel(url)}
      </p>
      <p className="mt-1.5 flex items-center gap-1 text-[11px] text-base-content/45">
        <span className="shrink-0">{actionTypeMeta[rec.action_type].label}</span>
        <ChevronRight className="size-3 shrink-0" />
        <span className={`truncate ${rec.target_url ? "" : "text-primary/80"}`}>
          {rec.proposed_url ?? rec.target_url}
        </span>
      </p>

      <div className="mt-3 flex flex-wrap items-center gap-1.5 border-t border-base-200 pt-2.5">
        <OwnerBadgeSolid owner={rec.owner} />
        {rec.status !== "proposed" ? <StatusBadge status={rec.status} /> : null}
        {rec.status === "in_progress" || rec.status === "live" ? (
          <DueCountdown dueAt={rec.measurement_due_at} />
        ) : null}
      </div>
    </div>
  );
}

export function PipelineColumn({
  title,
  accentColor,
  count,
  children,
}: {
  title: string;
  accentColor: string;
  count: number;
  children: React.ReactNode;
}) {
  return (
    <div className="flex w-72 shrink-0 flex-col rounded-xl border border-base-300/70 bg-base-200/50">
      <div className="flex items-center justify-between border-b border-base-200 px-3.5 py-3">
        <div className="flex items-center gap-2">
          <span className={`size-2 rounded-full ${accentColor}`} />
          <span className="text-xs font-semibold uppercase tracking-wider text-base-content/60">
            {title}
          </span>
        </div>
        <span className="rounded-full bg-base-100 px-2 py-0.5 text-[11px] font-semibold tabular-nums text-base-content/70 ring-1 ring-inset ring-base-300">
          {count}
        </span>
      </div>
      <div className="flex min-h-32 flex-1 flex-col gap-2.5 p-2.5">
        {children}
      </div>
    </div>
  );
}

/** Raw column content: the latest candidate run condensed into a readable
 * summary with a flow arrow, since raw candidates aren't yet recommendations. */
export function RawCandidatesCard({
  proposedCount,
}: {
  proposedCount: number;
}) {
  return (
    <div className="flex min-h-32 flex-1 flex-col items-center justify-center rounded-xl border border-dashed border-base-300 bg-base-100/70 px-4 py-6 text-center">
      <p className="text-[11px] text-base-content/50">raw candidates</p>
      <div className="mt-3 flex flex-wrap items-center justify-center gap-x-1.5 gap-y-1 text-[11px] text-base-content/45">
        <span>4 generators</span>
        <ChevronRight className="size-3 text-base-content/25" />
        <span>pre-rank 27</span>
        <ChevronRight className="size-3 text-base-content/25" />
        <span className="font-semibold text-primary">
          agent → {proposedCount} proposed
        </span>
      </div>
    </div>
  );
}
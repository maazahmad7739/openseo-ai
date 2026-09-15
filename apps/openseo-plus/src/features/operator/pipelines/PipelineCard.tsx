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

/** Observation-window badge: "Day X/N" while measuring (spec §3 wording),
 * green with runway, amber near the close, red once overdue. */
export function DueCountdown({
  dueAt,
  implementedAt,
}: {
  dueAt: string | null;
  implementedAt?: string | null;
}) {
  const days = daysUntil(dueAt);
  if (days == null) return null;
  const overdue = days < 0;
  const soon = days <= 7 && !overdue;

  const window =
    implementedAt && dueAt
      ? Math.max(
          Math.round(
            (new Date(dueAt).getTime() - new Date(implementedAt).getTime()) /
              86_400_000,
          ),
          1,
        )
      : null;
  const dayElapsed =
    implementedAt && window
      ? Math.min(
          Math.max(
            Math.floor(
              (Date.now() - new Date(implementedAt).getTime()) / 86_400_000,
            ),
            0,
          ),
          window,
        )
      : null;

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
        : dayElapsed != null && window
          ? `Day ${dayElapsed}/${window}`
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

export function PipelineCard({
  rec,
  onImplement,
  onMarkLive,
  onOpenDetail,
}: {
  rec: Recommendation;
  onImplement?: (id: string) => void;
  onMarkLive?: (id: string) => void;
  onOpenDetail?: (id: string) => void;
}) {
  const url = rec.target_url ?? rec.proposed_url;
  return (
    <div
      className="group cursor-pointer rounded-xl border border-base-300 bg-base-100 p-3.5 shadow-sm transition-all hover:-translate-y-0.5 hover:shadow-md"
      onClick={() => onOpenDetail?.(rec.recommendation_id)}
    >
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
          <DueCountdown
            dueAt={rec.measurement_due_at}
            implementedAt={rec.implemented_at}
          />
        ) : null}
        {rec.status === "approved" && onImplement ? (
          <button
            type="button"
            className="btn btn-primary btn-xs ml-auto"
            onClick={(e) => {
              e.stopPropagation();
              onImplement(rec.recommendation_id);
            }}
            title="Mark as implemented: capture baseline, start observation window"
          >
            Implement
          </button>
        ) : null}
        {rec.status === "in_progress" && onMarkLive ? (
          <button
            type="button"
            className="btn btn-xs btn-outline ml-auto"
            onClick={(e) => {
              e.stopPropagation();
              onMarkLive(rec.recommendation_id);
            }}
            title="Change is live in production"
          >
            Mark live
          </button>
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
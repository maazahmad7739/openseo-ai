import {
  CalendarClock,
  ListChecks,
  TimerReset,
} from "lucide-react";
import type { Recommendation } from "../data/types";
import { taskProgress } from "../data/useOperatorData";
import {
  ActionTypeBadge,
  GeneratorBadge,
  ImpactBadge,
  OwnerBadgeSolid,
  StatusBadge,
} from "../components/Badge";
import { splitDiagnosis } from "../queue/RecommendationCard";

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

/** Clean URL label: the readable path when the url has a scheme, otherwise the
 * slug/relative path itself. Strips protocol + host that only add noise. */
function urlLabel(url: string | null): string {
  if (!url) return "—";
  if (/^https?:\/\//i.test(url)) {
    try {
      const path = new URL(url).pathname;
      return path && path !== "/" ? path : url;
    } catch {
      /* fall through to raw url */
    }
  }
  return url;
}

/** Compact volume label, e.g. "8200" → "8.2k vol", "150" → "150 vol". */
function fmtVol(volume: number | null | undefined): string | null {
  if (volume == null || volume <= 0) return null;
  if (volume >= 1000) {
    const k = (volume / 1000).toFixed(1).replace(/\.0$/, "");
    return `${k}k vol`;
  }
  return `${volume} vol`;
}

/** Kanban card: 3 scannable layers — badges (type + impact), target (clean
 * path + keyword/volume), action (one-line intent). */
export function PipelineCard({
  rec,
  onImplement,
  onOpenDetail,
}: {
  rec: Recommendation;
  onImplement?: (id: string) => void;
  onOpenDetail?: (id: string) => void;
}) {
  const url = rec.target_url ?? rec.proposed_url;
  const { summary } = splitDiagnosis(rec.diagnosis);
  const volume = fmtVol(rec.search_volume);
  const progress = taskProgress(rec);

  return (
    <div
      className="group cursor-pointer rounded-xl border border-base-300 bg-base-100 p-3.5 shadow-sm transition-all hover:-translate-y-0.5 hover:shadow-md"
      onClick={() => onOpenDetail?.(rec.recommendation_id)}
    >
      {/* Layer 1 — action type + impact/score badges */}
      <div className="flex flex-wrap items-center gap-1.5">
        <GeneratorBadge generator={rec.generator} />
        <ActionTypeBadge actionType={rec.action_type} />
        <span className="ml-auto">
          <ImpactBadge impact={rec.impact} />
        </span>
      </div>

      {/* Layer 2 — target: clean path + primary keyword & volume */}
      <p className="mt-2.5 truncate font-mono text-xs font-medium leading-snug text-base-content">
        {urlLabel(url)}
      </p>
      {rec.primary_keyword ? (
        <p className="mt-1 flex items-center gap-1 text-[11px] text-base-content/60">
          <span className="truncate">{rec.primary_keyword}</span>
          {volume ? (
            <span className="shrink-0 font-medium tabular-nums text-primary">
              • {volume}
            </span>
          ) : null}
        </p>
      ) : null}

      {/* Layer 3 — action / intent: one-line reason */}
      <p className="mt-2 line-clamp-2 text-[11px] leading-snug text-base-content/70">
        {summary}
      </p>

      {/* Footer: owner, status, observation, task progress, stage actions */}
      <div className="mt-3 flex flex-wrap items-center gap-1.5 border-t border-base-200 pt-2.5">
        <OwnerBadgeSolid owner={rec.owner} />
        {rec.status !== "approved" || !onImplement ? (
          <StatusBadge status={rec.status} />
        ) : null}
        {rec.status === "in_progress" || rec.status === "live" ? (
          <DueCountdown
            dueAt={rec.measurement_due_at}
            implementedAt={rec.implemented_at}
          />
        ) : null}
        {progress.total > 0 ? (
          <span
            className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium tabular-nums ring-1 ring-inset ${
              progress.done === progress.total
                ? "tag-chip-emerald"
                : "tag-chip-slate"
            }`}
            title={`${progress.done} of ${progress.total} tasks completed (auto-fix tasks count when applied; manual tasks when checked off in detail)`}
          >
            <ListChecks className="size-3" />
            {progress.done}/{progress.total} tasks completed
          </span>
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
    <div className="flex h-full w-72 shrink-0 flex-col overflow-hidden rounded-xl border border-base-300/70 bg-base-200/50">
      <div className="flex shrink-0 items-center justify-between border-b border-base-200 px-3.5 py-3">
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
      <div className="kanban-scroll flex min-h-0 flex-1 flex-col gap-2.5 overflow-y-auto p-2.5">
        {children}
      </div>
    </div>
  );
}
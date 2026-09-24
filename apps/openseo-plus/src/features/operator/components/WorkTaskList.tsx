import { useState } from "react";
import { Link } from "@tanstack/react-router";
import { CheckCircle2, Circle, Wrench, Zap } from "lucide-react";
import type { FixStatus, WorkRequiredItem } from "../data/types";
import {
  manualTaskKey,
  readManualTaskDone,
  writeManualTaskDone,
} from "../data/useOperatorData";
import { Chip } from "./Badge";

/** [⚡ Auto-fix] vs [🛠️ Manual] — the execution split between engine fixes
 * and operator handwork on compound recommendations (improve_page etc.). */
export function ExecutionTypeBadge({
  executionType,
}: {
  executionType: WorkRequiredItem["execution_type"];
}) {
  if (executionType === "automated") {
    return (
      <Chip className="tag-chip-sky">
        <Zap className="size-3" />
        Auto-fix
      </Chip>
    );
  }
  return (
    <Chip className="tag-chip-amber">
      <Wrench className="size-3" />
      Manual
    </Chip>
  );
}

const FIX_STATUS_META: Record<
  FixStatus,
  { label: string; badgeClass: string }
> = {
  generated: { label: "Drafted", badgeClass: "tag-chip-slate" },
  approved: { label: "Approved", badgeClass: "tag-chip-violet" },
  queued: { label: "Queued", badgeClass: "tag-chip-sky" },
  applied: { label: "Applied", badgeClass: "tag-chip-emerald" },
  failed: { label: "Failed", badgeClass: "tag-chip-rose" },
  reverted: { label: "Reverted", badgeClass: "tag-chip-slate" },
  expired: { label: "Expired", badgeClass: "tag-chip-slate" },
};

export function FixStatusBadge({ status }: { status: FixStatus }) {
  const meta = FIX_STATUS_META[status] ?? FIX_STATUS_META.generated;
  return <Chip className={meta.badgeClass}>{meta.label}</Chip>;
}

/** Interactive row-level task list: auto-fix tasks show their fix status +
 * a Review in Action Queue link; manual tasks get a completion checkbox. */
export function WorkTaskList({
  work,
  recommendationId,
  projectId,
}: {
  work: WorkRequiredItem[];
  recommendationId?: string;
  /** Present enables the Review-in-Action-Queue deep link (pipelines card). */
  projectId?: string;
}) {
  const [manualDone, setManualDone] = useState(() => readManualTaskDone());
  const toggleDone = (index: number) => {
    if (!recommendationId) return;
    const key = manualTaskKey(recommendationId, index);
    const next = !manualDone[key];
    writeManualTaskDone(key, next);
    setManualDone(readManualTaskDone());
  };

  return (
    <div className="flex flex-col gap-3">
      {work.map((item, i) => {
        const automated = item.execution_type === "automated";
        const done = automated
          ? item.fix_status === "applied"
          : manualDone[manualTaskKey(recommendationId ?? "", i)] ?? false;
        return (
          <div
            key={i}
            className={`rounded-lg border p-3 transition-colors ${
              done
                ? "border-success/30 bg-success/5"
                : "border-base-200 bg-base-100"
            }`}
          >
            <div className="flex flex-wrap items-center gap-2">
              <span className="rounded bg-base-200 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-base-content/60">
                {item.owner}
              </span>
              <ExecutionTypeBadge executionType={item.execution_type} />
              {automated && item.fix_status ? (
                <FixStatusBadge status={item.fix_status} />
              ) : null}
              <span className="text-xs text-base-content/40">
                task {i + 1}
              </span>
            </div>
            <div className="mt-1.5 flex items-start gap-2.5">
              {/* Manual tasks: interactive completion checkbox. Automated
                  tasks show a fixed status glyph instead (engine-driven). */}
              {!automated && recommendationId ? (
                <button
                  type="button"
                  role="checkbox"
                  aria-checked={done}
                  aria-label={`Mark manual task ${i + 1} as ${done ? "incomplete" : "completed"}`}
                  onClick={() => toggleDone(i)}
                  className="mt-0.5 shrink-0"
                >
                  {done ? (
                    <CheckCircle2 className="size-4 text-success" />
                  ) : (
                    <Circle className="size-4 text-base-content/30" />
                  )}
                </button>
              ) : (
                <CheckCircle2
                  className={`mt-0.5 size-4 shrink-0 ${
                    automated && done ? "text-success" : "text-base-content/30"
                  }`}
                />
              )}
              <div className="min-w-0">
                <p
                  className={`text-sm font-medium ${
                    done ? "text-base-content/50 line-through" : "text-base-content"
                  }`}
                >
                  {item.task}
                </p>
                <p className="mt-1 flex items-start gap-1.5 text-xs leading-relaxed text-base-content/55">
                  <span className="mt-0.5 shrink-0 font-semibold text-success">
                    ✓
                  </span>
                  <span>
                    <span className="font-medium text-base-content/60">
                      Acceptance:
                    </span>{" "}
                    {item.acceptance_criteria}
                  </span>
                </p>
                {automated ? (
                  <div className="mt-2 flex items-center gap-2">
                    {projectId ? (
                      <Link
                        to="/p/$projectId/action-queue"
                        params={{ projectId }}
                        className="btn btn-xs btn-outline btn-primary"
                        onClick={(e) => e.stopPropagation()}
                      >
                        Review in Action Queue
                      </Link>
                    ) : (
                      <span className="text-[11px] text-base-content/40">
                        Manage in Action Queue
                      </span>
                    )}
                  </div>
                ) : null}
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}
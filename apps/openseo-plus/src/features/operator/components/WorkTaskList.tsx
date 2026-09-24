import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "@tanstack/react-router";
import {
  CheckCircle2,
  Circle,
  Loader2,
  Wrench,
  X,
  Zap,
} from "lucide-react";
import { toast } from "sonner";
import { api } from "../data/useOperatorData";
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

/* ── Fix diff preview + gate-2 approval ──────────────────────────────── */

interface FixDto {
  fix_id: string;
  sub_type: string | null;
  status: FixStatus;
  diff_json: Array<{ field: string; old_value?: string; new_value?: string }> | null;
  generation_source: string;
}

const FIELD_LABELS: Record<string, string> = {
  "seo.title": "Title tag",
  "seo.description": "Meta description",
};

function DiffRow({ field, oldValue, newValue }: {
  field: string;
  oldValue?: string;
  newValue?: string;
}) {
  return (
    <div className="rounded-lg border border-base-200 bg-base-200/30 p-2.5">
      <p className="text-[10px] font-semibold uppercase tracking-wider text-base-content/45">
        {FIELD_LABELS[field] ?? field}
      </p>
      <div className="mt-1.5 space-y-1.5 text-xs leading-relaxed">
        {oldValue != null ? (
          <p className="flex items-start gap-1.5">
            <span className="shrink-0 rounded bg-error/10 px-1 font-mono text-[10px] font-bold text-error">
              −
            </span>
            <span className="text-base-content/50 line-through">{oldValue}</span>
          </p>
        ) : null}
        {newValue != null ? (
          <p className="flex items-start gap-1.5">
            <span className="shrink-0 rounded bg-success/10 px-1 font-mono text-[10px] font-bold text-success">
              +
            </span>
            <span className="font-medium text-base-content">{newValue}</span>
          </p>
        ) : null}
      </div>
    </div>
  );
}

/** Drafted old → new diff + gate-2 approve/reject for one auto-fix task. */
function FixDiffPreview({
  recommendationId,
  fixStatus,
}: {
  recommendationId: string;
  fixId?: string;
  fixStatus?: FixStatus;
}) {
  const queryClient = useQueryClient();
  const [busy, setBusy] = useState(false);

  const fixes = useQuery({
    queryKey: ["openseo", "fixes", recommendationId],
    queryFn: () => api<FixDto[]>(`/recommendations/${recommendationId}/fix`),
    staleTime: 15_000,
  });

  const act = async (fix: FixDto, action: "approve" | "reject") => {
    setBusy(true);
    try {
      if (action === "approve") {
        await api(`/fixes/${fix.fix_id}/approve`, { method: "POST" });
        toast.success("Fix approved and queued for execution");
      } else {
        await api(`/fixes/${fix.fix_id}/reject`, {
          method: "POST",
          body: JSON.stringify({ reason: "Operator rejected the drafted diff" }),
        });
        toast.success("Drafted fix rejected");
      }
      void queryClient.invalidateQueries({
        queryKey: ["openseo", "fixes", recommendationId],
      });
      void queryClient.invalidateQueries({ queryKey: ["openseo", "detail", recommendationId] });
    } finally {
      setBusy(false);
    }
  };

  if (fixes.isPending) {
    return (
      <p className="mt-2 flex items-center gap-1.5 text-[11px] text-base-content/40">
        <Loader2 className="size-3 animate-spin" /> Loading drafted fix…
      </p>
    );
  }
  if (fixes.isError) {
    return (
      <p className="mt-2 text-[11px] text-base-content/40">
        Drafted fix could not be loaded.
      </p>
    );
  }

  const rows = fixes.data ?? [];
  if (rows.length === 0) {
    return (
      <p className="mt-2 text-[11px] text-base-content/40">
        No drafted fix yet — the engine drafts the title/meta rewrite once this
        recommendation is approved in the queue.
      </p>
    );
  }

  const decided = fixStatus === "queued" || fixStatus === "applied";

  return (
    <div className="mt-2 space-y-2">
      {rows.map((fix) => {
        const diffs = fix.diff_json ?? [];
        const actionable = fix.status === "generated";
        return (
          <div key={fix.fix_id} className="space-y-2">
            {diffs.map((d, di) => (
              <DiffRow
                key={di}
                field={d.field}
                oldValue={d.old_value}
                newValue={d.new_value}
              />
            ))}
            <div className="flex flex-wrap items-center gap-2">
              {actionable && !decided ? (
                <>
                  <button
                    type="button"
                    className="btn btn-xs btn-primary"
                    disabled={busy}
                    onClick={(e) => {
                      e.stopPropagation();
                      void act(fix, "approve");
                    }}
                  >
                    {busy ? <Loader2 className="size-3 animate-spin" /> : null}
                    Approve &amp; Queue
                  </button>
                  <button
                    type="button"
                    className="btn btn-xs btn-ghost text-error"
                    disabled={busy}
                    onClick={(e) => {
                      e.stopPropagation();
                      void act(fix, "reject");
                    }}
                  >
                    <X className="size-3" />
                    Reject
                  </button>
                </>
              ) : (
                <span className="text-[11px] text-base-content/40">
                  {fix.status === "queued"
                    ? "Queued — the executor will apply this change"
                    : fix.status === "applied"
                      ? "Applied to the store"
                      : "Reviewed"}
                </span>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}

/* ── Task list ───────────────────────────────────────────────────────── */

/** Interactive row-level task list: auto-fix tasks show the drafted
 * old → new diff with inline Approve & Queue / Reject; manual tasks get a
 * completion checkbox. */
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
                {automated && recommendationId ? (
                  <FixDiffPreview
                    recommendationId={recommendationId}
                    fixStatus={item.fix_status}
                  />
                ) : null}
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
import {
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { useState } from "react";
import {
  AlertTriangle,
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
  "collection.description_html": "Collection description",
};

/**
 * Scope task text to what the engine actually does. The agent's
 * work_required copy sometimes bundles extra asks ("…; add structured data
 * (Product schema)", "…; set up analytics") the auto-fix engine does NOT
 * execute (it only writes seo.title / seo.description). On automated tasks,
 * drop trailing semicolon-piped clauses that promise schema/structured-data
 * work so the UI never over-promises.
 */
const AUTOMATED_OUT_OF_SCOPE_CLAUSE = /;\s*[^;]*(structured data|schema markup|product schema|json-ld)[^;]*/gi;

export function scopeTaskText(task: string, automated: boolean): string {
  if (!automated) return task;
  const scoped = task.replace(AUTOMATED_OUT_OF_SCOPE_CLAUSE, "").trim();
  return scoped.length >= 10 ? scoped : task;
}

/** Strip provider HTML (collection descriptionHtml diffs, plan/24 §4.2)
 * to plain text for the drawer: tag-strip + whitespace-collapse. Never
 * dangerouslySetInnerHTML on provider-adjacent copy. */
function stripHtml(value: string): string {
  return value
    .replace(/<[^>]+>/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

const DIFF_PREVIEW_CHAR_CAP = 400;

function DiffPreviewText({ value }: { value?: string }) {
  const [expanded, setExpanded] = useState(false);
  // HTML-valued diffs (collection.description_html) arrive pre-stripped by
  // the generator's diff builder; strip defensively anyway so a raw-HTML
  // row can never render markup into the drawer.
  const text = stripHtml(value ?? "");
  const truncated = !expanded && text.length > DIFF_PREVIEW_CHAR_CAP;
  return (
    <>
      <span>
        {truncated ? text.slice(0, DIFF_PREVIEW_CHAR_CAP) : text}
        {truncated ? "…" : ""}
      </span>
      {text.length > DIFF_PREVIEW_CHAR_CAP ? (
        <button
          type="button"
          className="ml-1 text-[10px] font-semibold text-primary/70 underline underline-offset-2"
          onClick={() => setExpanded((v) => !v)}
        >
          {expanded ? "show less" : "show full"}
        </button>
      ) : null}
    </>
  );
}

function DiffRow({ field, oldValue, newValue }: {
  field: string;
  oldValue?: string;
  newValue?: string;
}) {
  // An empty/absent old value is still worth showing explicitly — the
  // −/+ contrast (old vs new) must read the same on every field, so a
  // missing previous description renders as a muted placeholder line
  // instead of silently hiding the row.
  const hasOld = oldValue != null && oldValue.trim() !== "";
  const hasNew = newValue != null && newValue.trim() !== "";
  return (
    <div className="rounded-lg border border-base-200 bg-base-200/30 p-2.5">
      <p className="text-[10px] font-semibold uppercase tracking-wider text-base-content/45">
        {FIELD_LABELS[field] ?? field}
      </p>
      <div className="mt-1.5 space-y-1.5 text-xs leading-relaxed">
        {hasOld ? (
          <p className="flex items-start gap-1.5">
            <span className="shrink-0 rounded bg-error/10 px-1 font-mono text-[10px] font-bold text-error">
              −
            </span>
            <span className="text-base-content/50 line-through">
              <DiffPreviewText value={oldValue} />
            </span>
          </p>
        ) : (
          <p className="flex items-start gap-1.5">
            <span className="shrink-0 rounded bg-error/10 px-1 font-mono text-[10px] font-bold text-error/50">
              −
            </span>
            <span className="italic text-base-content/35">no previous description</span>
          </p>
        )}
        {hasNew ? (
          <p className="flex items-start gap-1.5">
            <span className="shrink-0 rounded bg-success/10 px-1 font-mono text-[10px] font-bold text-success">
              +
            </span>
            <span className="font-medium text-base-content">
              <DiffPreviewText value={newValue} />
            </span>
          </p>
        ) : null}
      </div>
    </div>
  );
}

/** Drafted old → new diff + gate-2 approve/reject for one auto-fix task.
 *
 * The fix engine drafts lazily: when the drawer opens on an approved
 * recommendation with no generated_fixes rows yet, this component calls
 * POST /recommendations/{id}/draft-fixes once, then reads the diff.
 */
function FixDiffPreview({
  recommendationId,
  fixId,
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

  // Lazy draft: only fire when the fix list came back EMPTY and no fix has
  // been decided yet. Idempotent server-side (uq_fixes_active_per_target).
  const shouldDraft =
    !fixes.isPending &&
    !fixes.isError &&
    (fixes.data ?? []).length === 0 &&
    fixId == null &&
    fixStatus == null;

  const draft = useMutation({
    mutationFn: () =>
      api<{ drafts: FixDto[]; unsupported: Array<{ sub_type: string; reason: string }> }>(
        `/recommendations/${recommendationId}/draft-fixes`,
        { method: "POST" },
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: ["openseo", "fixes", recommendationId],
      });
      void queryClient.invalidateQueries({ queryKey: ["openseo", "detail", recommendationId] });
    },
  });

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

  const act = async (fix: FixDto, action: "approve" | "reject" | "apply") => {
    setBusy(true);
    try {
      if (action === "approve") {
        await api(`/fixes/${fix.fix_id}/approve`, { method: "POST" });
        toast.success("Fix approved and queued for execution");
      } else if (action === "apply") {
        const res = await api<{ status?: string; verified?: boolean; verification_status?: string }>(
          `/fixes/${fix.fix_id}/execute`,
          { method: "POST" },
        );
        toast.success(
          res?.verified || res?.verification_status === "verified"
            ? "Applied to your store — verified by read-back"
            : "Apply requested — check status in a moment",
        );
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
    } catch (err) {
      // Surface the API rejection (409 policy/cap blocks, network, etc.)
      // instead of failing silently — the operator must see WHY nothing
      // happened.
      const detail = err instanceof Error ? err.message : String(err);
      toast.error(`Could not ${action} the fix — ${detail}`);
    } finally {
      setBusy(false);
    }
  };

  if (shouldDraft) {
    if (draft.isPending) {
      return (
        <p className="mt-2 flex items-center gap-1.5 text-[11px] text-base-content/40">
          <Loader2 className="size-3 animate-spin" /> Drafting fix — the engine
          is writing the proposed rewrite…
        </p>
      );
    }
    if (draft.isError) {
      return (
        <p className="mt-2 flex items-center gap-1.5 text-[11px] text-base-content/40">
          <AlertTriangle className="size-3" /> Drafting failed —{" "}
          <button
            type="button"
            className="underline underline-offset-2 hover:text-primary"
            onClick={(e) => {
              e.stopPropagation();
              draft.mutate();
            }}
          >
            retry
          </button>
        </p>
      );
    }
    if (draft.data) {
      if (draft.data.drafts.length > 0) return null; // diff renders below on refetch
      return (
        <div className="mt-2 space-y-1">
          {draft.data.unsupported.map((u) => (
            <p
              key={u.sub_type}
              className="flex items-start gap-1.5 text-[11px] leading-relaxed text-base-content/40"
            >
              <AlertTriangle className="mt-0.5 size-3 shrink-0 text-warning" />
              <span>
                No draft for {u.sub_type === "seo.title" ? "the title tag" : "the meta description"}
                : {u.reason}
              </span>
            </p>
          ))}
        </div>
      );
    }
    return null;
  }

  const rows = fixes.data ?? [];
  if (rows.length === 0) {
    return (
      <p className="mt-2 text-[11px] text-base-content/40">
        No drafted fix yet.
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
              ) : fix.status === "queued" ? (
                <button
                  type="button"
                  className="btn btn-xs btn-primary"
                  disabled={busy}
                  onClick={(e) => {
                    e.stopPropagation();
                    void act(fix, "apply");
                  }}
                >
                  {busy ? <Loader2 className="size-3 animate-spin" /> : null}
                  Apply now
                </button>
              ) : (
                <span className="text-[11px] text-base-content/40">
                  {fix.status === "applied"
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
}: {
  work: WorkRequiredItem[];
  recommendationId?: string;
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
                  {scopeTaskText(item.task, automated)}
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
                    fixId={item.fix_id}
                    fixStatus={item.fix_status}
                  />
                ) : null}
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}
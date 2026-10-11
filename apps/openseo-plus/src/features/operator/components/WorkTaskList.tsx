import {
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { useEffect, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  Circle,
  ExternalLink,
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

/** Client-side mirror of the backend's task classifier (plan/28): the
 * same specific-match precedence so a task's diff preview shows ITS
 * OWN sub_type's fixes. Shopfiy patterns omitted (drawer filtering only
 * needs the netlify surfaces; other platforms rely on fixId linkage). */
export function taskSubTypeFor(taskText: string): string | null {
  return taskSubTypesFor(taskText)[0] ?? null;
}

/** All sub_types a task bundles (backend _sub_types_for_task mirror):
 * title and meta both route to netlify.title_meta_rewrite on netlify
 * surfaces; shopify tasks rely on fixId/fix_refs linkage instead. */
export function taskSubTypesFor(taskText: string): string[] {
  const lowered = (taskText || "").toLowerCase();
  if (lowered.includes("structured") || lowered.includes("json")) {
    return ["netlify.structured_data"];
  }
  if (lowered.includes("canonical")) {
    return ["netlify.canonical_tag"];
  }
  if (lowered.includes("title") || lowered.includes("meta description")) {
    return ["netlify.title_meta_rewrite"];
  }
  return [];
}

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
  "meta.canonical": "Canonical tag",
  "meta.jsonld": "Structured data",
  "meta.title": "Title tag",
  "meta.description": "Meta description",
  "framer.title": "Title tag",
  "collection.description_html": "Collection description",
  "collection.title": "Collection title",
  "collection.handle": "URL handle",
  "collection.seo_title": "SEO title",
  "collection.seo_description": "SEO description",
  "collection.products": "Member products",
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

/** Fields whose diff values are PROSE HTML (a description body), where
 * tag-stripping is the right display. Markup-valued fields — meta.canonical
 * carries a literal `<link rel="canonical" …>` tag — must render verbatim:
 * stripping them produced an empty preview next to the green "+" (the tag
 * was swallowed by the same regex). React escapes text content by default,
 * so the raw tag string renders safely as text. */
const HTML_PROSE_FIELDS = new Set(["collection.description_html"]);

const DIFF_PREVIEW_CHAR_CAP = 400;

function DiffPreviewText({ value, field }: { value?: string; field?: string }) {
  const [expanded, setExpanded] = useState(false);
  // HTML-prose diffs (collection.description_html) arrive pre-stripped by
  // the generator's diff builder; strip defensively anyway. Everything
  // else renders verbatim — a canonical-tag diff IS its markup string.
  const text =
    field && HTML_PROSE_FIELDS.has(field)
      ? stripHtml(value ?? "")
      : (value ?? "").trim();
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

// plan/25: create-type fields have no previous value by design — the
// placeholder reads "creates it" rather than "no previous description".
const EMPTY_OLD_LABELS: Record<string, string> = {
  "collection.title": "nothing yet — this fix creates the collection",
  "collection.handle": "nothing yet — this fix creates the URL",
  "collection.seo_title": "nothing yet — this fix creates the SEO title",
  "collection.seo_description": "nothing yet — this fix creates the SEO description",
  "collection.description_html": "nothing yet — this fix creates the description",
  "collection.products": "no products attached yet",
};

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
              <DiffPreviewText value={oldValue} field={field} />
            </span>
          </p>
        ) : (
          <p className="flex items-start gap-1.5">
            <span className="shrink-0 rounded bg-error/10 px-1 font-mono text-[10px] font-bold text-error/50">
              −
            </span>
            <span className="italic text-base-content/35">
              {EMPTY_OLD_LABELS[field] ?? "no previous value"}
            </span>
          </p>
        )}
        {hasNew ? (
          <p className="flex items-start gap-1.5">
            <span className="shrink-0 rounded bg-success/10 px-1 font-mono text-[10px] font-bold text-success">
              +
            </span>
            <span className="font-medium text-base-content">
              <DiffPreviewText value={newValue} field={field} />
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
 *
 * A task bundling several engine surfaces (title + meta) carries
 * fix_refs — one diff block renders PER ref so every field's before/after
 * is visible (the seo.title row is no longer shadowed by the
 * seo.description precedence match).
 */
function FixDiffPreview({
  recommendationId,
  fixId,
  fixStatus,
  taskText,
  fixRefs,
}: {
  recommendationId: string;
  fixId?: string;
  fixStatus?: FixStatus;
  /** The task's own text: disambiguates WHICH sub_type this task maps to
   * when a recommendation carries multiple automated tasks (structured
   * data AND canonical on netlify), so each task renders ITS OWN fix
   * diff — never a sibling task's diff. */
  taskText?: string;
  /** All fix rows this task bundles (backend enrich_work_tasks). */
  fixRefs?: WorkRequiredItem["fix_refs"];
}) {
  const refs: Array<{ fix_id: string; status?: FixStatus }> =
    fixRefs && fixRefs.length > 0
      ? fixRefs.map((r) => ({ fix_id: r.fix_id, status: r.status }))
      : fixId
        ? [{ fix_id: fixId, status: fixStatus }]
        : [];
  if (refs.length > 1) {
    return (
      <div className="mt-2 space-y-2">
        {refs.map((ref) => (
          <SingleFixPreview
            key={ref.fix_id}
            recommendationId={recommendationId}
            fixId={ref.fix_id}
            fixStatus={ref.status}
            taskText={taskText}
          />
        ))}
      </div>
    );
  }
  return (
    <SingleFixPreview
      recommendationId={recommendationId}
      fixId={refs[0]?.fix_id}
      fixStatus={refs[0]?.status}
      taskText={taskText}
    />
  );
}

function SingleFixPreview({
  recommendationId,
  fixId,
  fixStatus,
  taskText,
}: {
  recommendationId: string;
  fixId?: string;
  fixStatus?: FixStatus;
  taskText?: string;
}) {
  const queryClient = useQueryClient();
  const [busy, setBusy] = useState(false);
  // plan/27+28: version-history toggle. MUST live with the other hooks —
  // a useState after conditional early returns breaks React's hook order
  // (the drawer crashed "something went wrong" on the loading->loaded
  // transition exactly because of that).
  const [showHistory, setShowHistory] = useState(false);

  const fixes = useQuery({
    queryKey: ["openseo", "fixes", recommendationId],
    queryFn: () => api<FixDto[]>(`/recommendations/${recommendationId}/fix`),
    staleTime: 15_000,
  });

  // plan/28 task-scoped filtering: match the fix row to THIS task via the
  // same precedence the backend classifier uses (structured/json >
  // canonical > title/meta). A task only ever sees its own sub_type's
  // fixes — the structured diff can never leak into the canonical task.
  const taskSubType = taskSubTypeFor(taskText ?? "");
  const scopedRows = (fixes.data ?? []).filter((f) => {
    if (fixId) return f.fix_id === fixId;
    if (!taskSubType) return true;
    return f.sub_type === taskSubType;
  });

  // Lazy draft: only fire when this task has NO fix rows (post-filter) and
  // no fix has been decided yet. Idempotent server-side.
  const shouldDraft =
    !fixes.isPending &&
    !fixes.isError &&
    scopedRows.length === 0 &&
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

  // Lazy draft: actually FIRE the mutation when the drawer opens on a
  // recommendation with no fix rows yet. The comment above promised
  // "calls POST …/draft-fixes once" — without this effect the mutation
  // sat unused and auto-fix tasks silently rendered nothing.
  useEffect(() => {
    if (shouldDraft && !draft.isPending && !draft.isError && !draft.data) {
      draft.mutate();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shouldDraft, recommendationId]);

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
      <p className="mt-2 text-[11px] text-base-content/50">
        <AlertTriangle className="mt-0.5 inline size-3 text-warning" /> Drafting
        needs card approval —{" "}
        <button
          type="button"
          className="underline underline-offset-2 hover:text-primary"
          onClick={(e) => {
            e.stopPropagation();
            draft.reset();
          }}
        >
          dismiss
        </button>{" "}
        <span className="text-base-content/35">
          ({String(draft.error).slice(0, 90)})
        </span>
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

  // plan/27+28 UX: only the LATEST ACTIVE fix of THIS task's sub_type
  // renders inline — the retry chain collapses into a muted "version
  // history" line. Priority: applied > queued > generated.
  const allRows = scopedRows;
  if (allRows.length === 0) {
    return (
      <p className="mt-2 text-[11px] text-base-content/40">
        No drafted fix yet.
      </p>
    );
  }

  // plan/27+28 UX: only the LATEST ACTIVE fix renders inline — the retry
  // chain (failed/expired historical rows) collapses into a muted
  // "version history" line. Priority: applied > queued > generated; the
  // first row in that order wins (the API already sorts applied first).
  const active =
    allRows.find((f) => f.status === "applied") ??
    allRows.find((f) => f.status === "queued") ??
    allRows.find((f) => f.status === "generated") ??
    allRows[0];
  const superseded = allRows.filter((f) => f.fix_id !== active.fix_id);

  const decided = fixStatus === "queued" || fixStatus === "applied";

  return (
    <div className="mt-2 space-y-2">
      {superseded.length > 0 ? (
        <button
          type="button"
          className="text-[10px] text-base-content/35 underline underline-offset-2 hover:text-base-content/55"
          onClick={(e) => {
            e.stopPropagation();
            setShowHistory((v) => !v);
          }}
        >
          {showHistory ? "Hide" : "Show"} {superseded.length} superseded
          attempt{superseded.length === 1 ? "" : "s"}
        </button>
      ) : null}
      {showHistory
        ? superseded.map((fix) => (
            <div
              key={fix.fix_id}
              className="rounded-lg border border-base-200/60 bg-base-200/20 p-2 opacity-60"
            >
              <p className="text-[10px] uppercase tracking-wider text-base-content/40">
                superseded · {fix.status}
              </p>
              {(fix.diff_json ?? []).map((d, di) => (
                <DiffRow
                  key={di}
                  field={d.field}
                  oldValue={d.old_value}
                  newValue={d.new_value}
                />
              ))}
            </div>
          ))
        : null}
      <div className="space-y-2">
        {(() => {
          const diffs = active.diff_json ?? [];
          const actionable = active.status === "generated";
          const isNetlify = (active.sub_type ?? "").startsWith("netlify.");
          return (
            <div className="space-y-2">
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
                        void act(active, "approve");
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
                        void act(active, "reject");
                      }}
                    >
                      <X className="size-3" />
                      Reject
                    </button>
                  </>
                ) : active.status === "queued" ? (
                  <button
                    type="button"
                    className="btn btn-xs btn-primary"
                    disabled={busy}
                    onClick={(e) => {
                      e.stopPropagation();
                      void act(active, "apply");
                    }}
                  >
                    {busy ? <Loader2 className="size-3 animate-spin" /> : null}
                    Apply now
                  </button>
                ) : (
                  <span className="text-[11px] text-base-content/40">
                    {appliedCopy(active.status, isNetlify)}
                  </span>
                )}
              </div>
            </div>
          );
        })()}
      </div>
    </div>
  );
}

/** Platform-aware applied copy (plan/27+28): Shopify writes land on the
 * store; netlify fixes land as GitHub PRs on the content branch. */
export function appliedCopy(status: FixStatus, isNetlify: boolean): string {
  if (status === "applied") {
    return isNetlify ? "PR generated — applied to branch" : "Applied to the store";
  }
  return "Reviewed";
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
                    taskText={item.task}
                    fixRefs={item.fix_refs}
                  />
                ) : null}
                {item.fix_pr_url ? (
                  <a
                    href={item.fix_pr_url}
                    target="_blank"
                    rel="noreferrer"
                    className="mt-1 inline-flex items-center gap-1 text-[11px] font-medium text-primary hover:underline"
                    onClick={(e) => e.stopPropagation()}
                  >
                    <ExternalLink className="size-3" />
                    View PR on GitHub
                    {item.fix_status === "applied" ? " (open — merge to publish)" : ""}
                  </a>
                ) : null}
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}
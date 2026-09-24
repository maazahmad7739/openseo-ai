import { useQuery } from "@tanstack/react-query";
import {
  BadgeDollarSign,
  CalendarClock,
  ExternalLink,
  FileText,
  Globe,
  Loader2,
  Package,
  Search,
  Sparkles,
  Target,
  X,
} from "lucide-react";
import { api } from "../data/useOperatorData";
import type {
  ActionType,
  ConfidenceLevel,
  EffortLevel,
  GeneratorType,
  ImpactLevel,
  Owner,
  RecommendationStatus,
  ResultClass,
} from "../data/types";
import { EvidencePanel } from "../queue/evidence";
import {
  GeneratorBadge,
  ImpactBadge,
  OwnerBadge,
  ResultBadge,
  StatusBadge,
} from "./Badge";
import { actionTypeMeta } from "./meta";
import { WorkTaskList } from "./WorkTaskList";

/** Raw detail payload from GET /queue/{recommendation_id}. */
export interface RecommendationDetail {
  recommendation_id: string;
  site_id: string;
  generator: string;
  action_type: string;
  target_url: string | null;
  proposed_url: string | null;
  diagnosis: string;
  evidence_json: Array<{ source: string; finding: string; value?: string }> | null;
  work_required_json: Array<{
    owner: string;
    task: string;
    acceptance_criteria: string;
    execution_type?: "automated" | "manual";
    fix_id?: string;
    fix_status?: string;
  }> | null;
  impact: ImpactLevel;
  confidence: ConfidenceLevel;
  effort: EffortLevel;
  owner: Owner;
  status: RecommendationStatus;
  enriched: boolean;
  result: ResultClass | null;
  measurement_metric: string | null;
  measurement_window_days: number | null;
  measurement_due_at: string | null;
  created_at: string | null;
  approved_at: string | null;
  implemented_at: string | null;
  measured_at: string | null;
  cluster: {
    cluster_id: string;
    primary_keyword: string;
    keywords: string[];
    intent: string;
    search_volume: number;
    commercial_value: string;
  } | null;
  measurement_plan: {
    metric: string | null;
    window_days: number | null;
    window_source: string | null;
    measurement_due_at: string | null;
  } | null;
  catalogue: {
    matching_product_count: number | null;
    in_stock_product_count: number | null;
    average_price: number | null;
    existing_collection_url: string | null;
  } | null;
  serp_context: Array<{
    query: string;
    result_url: string;
    result_domain: string;
    position: number | null;
    is_self: boolean;
    snapshot_date: string;
  }> | null;
}

export function useRecommendationDetail(id: string | null) {
  return useQuery({
    queryKey: ["openseo", "detail", id],
    queryFn: () => api<RecommendationDetail>(`/queue/${id}`),
    enabled: id != null,
    staleTime: 30_000,
  });
}

/**
 * Screen 2: full recommendation detail in a right-hand slide-over.
 * Sections: diagnosis, evidence, work required, catalogue inventory,
 * SERP comparison, measurement plan.
 */
export function DetailDrawer({
  recommendationId,
  onClose,
  projectId,
}: {
  recommendationId: string | null;
  onClose: () => void;
  projectId?: string;
}) {
  const detail = useRecommendationDetail(recommendationId);

  return (
    <div
      className={`fixed inset-0 z-50 transition-opacity duration-200 ${
        recommendationId ? "" : "pointer-events-none"
      }`}
      aria-hidden={recommendationId == null}
    >
      {/* Overlay */}
      <div
        className={`absolute inset-0 bg-black/40 transition-opacity ${
          recommendationId ? "opacity-100" : "opacity-0"
        }`}
        onClick={onClose}
      />
      {/* Sheet */}
      <aside
        role="dialog"
        aria-modal="true"
        aria-label="Recommendation detail"
        className={`absolute right-0 top-0 flex h-full w-full max-w-xl flex-col bg-base-100 shadow-2xl transition-transform duration-300 ${
          recommendationId ? "translate-x-0" : "translate-x-full"
        }`}
      >
        <DrawerBody detail={detail} onClose={onClose} projectId={projectId} />
      </aside>
    </div>
  );
}

function DrawerBody({
  detail,
  onClose,
  projectId,
}: {
  detail: ReturnType<typeof useRecommendationDetail>;
  onClose: () => void;
  projectId?: string;
}) {
  return (
    <>
      <header className="flex items-start justify-between gap-3 border-b border-base-200 px-5 py-4">
        <div className="min-w-0 space-y-2">
          <div className="flex flex-wrap items-center gap-1.5">
            {detail.data ? (
              <>
                <GeneratorBadge generator={detail.data.generator as GeneratorType} />
                <ImpactBadge impact={detail.data.impact} />
                <OwnerBadge owner={detail.data.owner as Owner} />
                <StatusBadge status={detail.data.status} />
              </>
            ) : null}
          </div>
          <p className="truncate font-mono text-xs text-base-content/60">
            {detail.data?.target_url ?? detail.data?.proposed_url ?? ""}
          </p>
        </div>
        <button
          type="button"
          className="btn btn-circle btn-ghost btn-sm"
          onClick={onClose}
          aria-label="Close detail panel"
        >
          <X className="size-4" />
        </button>
      </header>

      <div className="flex-1 overflow-y-auto px-5 py-4">
        {detail.isPending ? (
          <div className="flex h-40 items-center justify-center gap-2 text-sm text-base-content/50">
            <Loader2 className="size-4 animate-spin" />
            Loading detail…
          </div>
        ) : detail.isError || !detail.data ? (
          <div className="alert alert-error text-sm">
            Could not load the recommendation detail.
          </div>
        ) : (
          <div className="flex flex-col gap-5 pb-8">
            <WhyItMatters detail={detail.data} />
            <WorkRequired detail={detail.data} projectId={projectId} />
            <EvidenceBlock detail={detail.data} />
            <SerpComparison detail={detail.data} />
            <CatalogueBlock detail={detail.data} />
            <MeasurementPlan detail={detail.data} />
          </div>
        )}
      </div>
    </>
  );
}

/* ── Sections ─────────────────────────────────────────────────────────── */

function SectionCard({
  title,
  icon,
  children,
}: {
  title: string;
  icon: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section className="rounded-xl border border-base-300 bg-base-100">
      <h3 className="flex items-center gap-2 border-b border-base-200 px-4 py-2.5 text-sm font-semibold text-base-content">
        <span className="text-base-content/40">{icon}</span>
        {title}
      </h3>
      <div className="px-4 py-3">{children}</div>
    </section>
  );
}

function WhyItMatters({ detail }: { detail: RecommendationDetail }) {
  return (
    <SectionCard title="Why this matters" icon={<Sparkles className="size-4" />}>
      <p className="text-sm leading-relaxed text-base-content/85">
        {detail.diagnosis}
      </p>
      <div className="mt-3 flex flex-wrap items-center gap-1.5">
        <span className="text-[11px] uppercase tracking-wider text-base-content/40">
          {actionTypeMeta[detail.action_type as ActionType].label}
        </span>
        <span className="text-base-content/25">·</span>
        <span className="text-xs text-base-content/55">
          {detail.confidence} confidence
        </span>
        <span className="text-base-content/25">·</span>
        <span className="text-xs text-base-content/55">
          effort: {detail.effort === "days" ? "~several days" : "~a day"}
        </span>
        {detail.enriched ? (
          <span className="tag-chip-violet rounded-full px-2 py-0.5 text-[11px] font-medium ring-1 ring-inset">
            agent-validated
          </span>
        ) : null}
      </div>
      {detail.cluster ? (
        <div className="mt-3 flex flex-wrap items-center gap-1.5 rounded-lg bg-base-200/50 px-3 py-2 text-xs">
          <Target className="size-3.5 text-base-content/40" />
          <span className="font-medium text-base-content/80">
            {detail.cluster.primary_keyword}
          </span>
          <span className="text-base-content/40">
            {fmtNum(detail.cluster.search_volume)}/mo ·{" "}
            {detail.cluster.intent} · {detail.cluster.commercial_value} value
          </span>
        </div>
      ) : null}
    </SectionCard>
  );
}

function WorkRequired({
  detail,
  projectId,
}: {
  detail: RecommendationDetail;
  projectId?: string;
}) {
  const work = detail.work_required_json ?? [];
  if (work.length === 0) return null;
  return (
    <SectionCard title="Exact work required" icon={<FileText />}>
      <WorkTaskList
        work={work as never}
        recommendationId={detail.recommendation_id}
        projectId={projectId}
      />
    </SectionCard>
  );
}

function EvidenceBlock({ detail }: { detail: RecommendationDetail }) {
  const evidence = detail.evidence_json ?? [];
  if (evidence.length === 0) return null;
  return (
    <SectionCard title="Supporting evidence" icon={<FileText />}>
      <EvidencePanel evidence={evidence as never} />
    </SectionCard>
  );
}

function CatalogueBlock({ detail }: { detail: RecommendationDetail }) {
  const cat = detail.catalogue;
  if (!cat) return null;
  const inStock = cat.in_stock_product_count;
  return (
    <SectionCard title="Pages & products affected" icon={<Package />}>
      <div className="grid grid-cols-3 gap-px overflow-hidden rounded-lg border border-base-300 bg-base-300/70">
        <MiniStat label="Matching products" value={fmtNum(cat.matching_product_count ?? 0)} />
        <MiniStat
          label="In stock"
          value={fmtNum(inStock ?? 0)}
          tone={inStock != null && inStock > 0 ? "good" : "bad"}
        />
        <MiniStat
          label="Avg price"
          value={cat.average_price != null ? `$${cat.average_price.toFixed(0)}` : "—"}
        />
      </div>
      {cat.existing_collection_url ? (
        <p className="mt-2.5 flex items-center gap-1.5 text-xs text-base-content/55">
          <Globe className="size-3.5 shrink-0" />
          <span className="truncate">
            existing collection: {cat.existing_collection_url}
          </span>
        </p>
      ) : null}
    </SectionCard>
  );
}

function SerpComparison({ detail }: { detail: RecommendationDetail }) {
  const rows = detail.serp_context ?? [];
  if (rows.length === 0) return null;
  return (
    <SectionCard title="SERP — who outranks you" icon={<Search />}>
      <ol className="flex flex-col gap-1.5">
        {rows.map((row) => (
          <li
            key={row.result_url}
            className={`flex items-center gap-3 rounded-lg border px-3 py-2 ${
              row.is_self
                ? "border-primary/40 bg-primary/5"
                : "border-base-200 bg-base-100"
            }`}
          >
            <span
              className={`flex size-6 shrink-0 items-center justify-center rounded-md text-xs font-bold tabular-nums ${
                row.is_self
                  ? "bg-primary text-primary-content"
                  : "bg-base-200 text-base-content/60"
              }`}
            >
              {row.position ?? "?"}
            </span>
            <span className="min-w-0 flex-1">
              <span
                className={`block truncate text-xs font-medium ${
                  row.is_self ? "text-primary" : "text-base-content/80"
                }`}
              >
                {row.result_domain}
              </span>
              <span className="block truncate text-[11px] text-base-content/40">
                {row.result_url}
              </span>
            </span>
            {row.is_self ? (
              <span className="shrink-0 rounded bg-primary/10 px-1.5 py-0.5 text-[10px] font-semibold text-primary">
                you
              </span>
            ) : (
              <a
                href={row.result_url}
                target="_blank"
                rel="noreferrer"
                className="shrink-0 text-base-content/30 hover:text-primary"
                aria-label={`Open ${row.result_domain}`}
              >
                <ExternalLink className="size-3.5" />
              </a>
            )}
          </li>
        ))}
      </ol>
      <p className="mt-2 text-[11px] text-base-content/40">
        Latest snapshot · competitors first
      </p>
    </SectionCard>
  );
}

function MeasurementPlan({ detail }: { detail: RecommendationDetail }) {
  const plan = detail.measurement_plan;
  if (!plan || (plan.window_days == null && !plan.metric)) return null;
  const window = plan.window_days;
  const implDate = detail.implemented_at ? new Date(detail.implemented_at) : null;
  const dayElapsed =
    implDate && window
      ? Math.min(
          Math.max(
            Math.floor((Date.now() - implDate.getTime()) / 86_400_000),
            0,
          ),
          window,
        )
      : null;
  return (
    <SectionCard title="Measurement plan" icon={<BadgeDollarSign />}>
      <div className="grid grid-cols-2 gap-2.5">
        <MiniStat label="Target metric" value={plan.metric ?? "—"} />
        <MiniStat label="Window" value={window != null ? `${window} days` : "—"} />
      </div>
      {dayElapsed != null && window != null ? (
        <div className="mt-2.5">
          <div className="flex items-center justify-between text-xs">
            <span className="font-medium text-base-content/70">
              {dayElapsed >= (window ?? 0)
                ? "Window complete — ready to measure"
                : `Measuring impact (Day ${dayElapsed}/${window})`}
            </span>
            {detail.measurement_due_at ? (
              <span className="flex items-center gap-1 text-base-content/50">
                <CalendarClock className="size-3" />
                due {new Date(detail.measurement_due_at).toLocaleDateString()}
              </span>
            ) : null}
          </div>
          <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-base-300/60">
            <div
              className="h-full rounded-full bg-primary transition-all"
              style={{ width: `${Math.round((dayElapsed / (window ?? 1)) * 100)}%` }}
            />
          </div>
        </div>
      ) : null}
      {detail.status === "measured" ? (
        <div className="mt-2.5 flex items-center gap-2">
          <ResultBadge result={(detail.result ?? "pending") as ResultClass} />
        </div>
      ) : null}
    </SectionCard>
  );
}

function MiniStat({
  label,
  value,
  tone = "neutral",
}: {
  label: string;
  value: string;
  tone?: "good" | "bad" | "neutral";
}) {
  const toneClass =
    tone === "good" ? "text-success" : tone === "bad" ? "text-error" : "";
  return (
    <div className="bg-base-100 px-3 py-2.5 text-center">
      <p className="text-[10px] uppercase tracking-wider text-base-content/45">
        {label}
      </p>
      <p className={`mt-0.5 text-lg font-semibold tabular-nums ${toneClass}`}>
        {value}
      </p>
    </div>
  );
}

function fmtNum(n: number | null | undefined): string {
  if (n == null) return "—";
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return String(n);
}
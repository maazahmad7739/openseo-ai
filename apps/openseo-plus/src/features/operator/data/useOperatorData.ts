import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo } from "react";
import type {
  ActionType,
  CandidateRun,
  ConfidenceLevel,
  EffortLevel,
  EvidenceItem,
  GeneratorType,
  ImpactLevel,
  MeasuredResult,
  Owner,
  Recommendation,
  RecommendationStats,
  RecommendationStatus,
  ResultClass,
  WorkRequiredItem,
} from "./types";

// All API calls must target /api/*. Vercel serves the FastAPI backend at
// /api/* (api/index.py), and the root rewrite leaves /api/* alone. The
// `|| "/api"` fallback guarantees the base is never empty, so
// fetch(`${API_BASE}${path}`) always produces /api/queue… — never a bare
// `queue?…` — even when VITE_API_BASE_URL is missing or set to "".
const raw = (
  (import.meta.env.VITE_API_BASE_URL as string | undefined) ||
  (import.meta.env.VITE_API_BASE as string | undefined) ||
  "/api"
).replace(/\/+$/, "");
const API_BASE: string = raw.endsWith("/api") ? raw : `${raw}/api`;

export { API_BASE };

interface ApiQueueItem {
  recommendation_id: string;
  site_id: string;
  generator: string;
  action_type: string;
  target_url: string | null;
  proposed_url: string | null;
  cluster_id: string | null;
  diagnosis: string;
  impact: string;
  confidence: string;
  effort: string;
  owner: string;
  status: string;
  assigned_to: string | null;
  result: string | null;
  search_volume: number | null;
  primary_keyword: string | null;
  commercial_value: string | null;
  evidence_json: unknown;
}

interface ApiQueueOut {
  site_id: string;
  total_proposed: number;
  showing: number;
  recommendations: ApiQueueItem[];
}

interface ApiPipelineItem {
  recommendation_id: string;
  site_id: string;
  generator: string;
  action_type: string;
  target_url: string | null;
  proposed_url: string | null;
  diagnosis: string;
  impact: string;
  status: string;
  search_volume: number | null;
  primary_keyword: string | null;
  approved_at: string | null;
  implemented_at: string | null;
  assigned_to: string | null;
  observation_window_days: number | null;
  days_remaining: number | null;
  measurement_due_at: string | null;
  work_tasks: WorkRequiredItem[] | null;
}

interface ApiPipelineOut {
  site_id: string;
  total_approved: number;
  total_in_progress: number;
  items: ApiPipelineItem[];
}

interface ApiRecentItem {
  recommendation_id: string;
  generator: string;
  action_type: string;
  target_url: string | null;
  proposed_url: string | null;
  result: string | null;
  measured_at: string | null;
  diagnosis: string | null;
}

interface ApiMeasuredDetail {
  recommendation_id: string;
  generator: string;
  action_type: string;
  url: string | null;
  cluster: string | null;
  verdict: string | null;
  before: {
    position: number | null;
    clicks: number | null;
    impressions?: number | null;
    orders?: number | null;
    revenue?: number | null;
  } | null;
  after: {
    position: number | null;
    clicks: number | null;
    impressions?: number | null;
    orders?: number | null;
    revenue?: number | null;
  } | null;
}

interface ApiResultsOut {
  site_id: string;
  total_measured: number;
  win_rate: number | null;
  verdicts: Record<string, number>;
  by_generator: unknown[];
  by_action_type: unknown[];
  recent: ApiRecentItem[];
  incremental_clicks: number;
  incremental_revenue: number;
  measured_details: ApiMeasuredDetail[];
}

interface OperatorData {
  recommendations: Recommendation[];
  stats: Record<string, RecommendationStats>;
  measured: MeasuredResult[];
  runs: CandidateRun[];
}

export const EMPTY_RECS: Recommendation[] = [];
export const EMPTY_STATS: Record<string, RecommendationStats> = {};
export const EMPTY_MEASURED: MeasuredResult[] = [];
export const EMPTY_RUNS: CandidateRun[] = [];

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = { accept: "application/json" };
  if (init?.body) headers["content-type"] = "application/json";
  const token = import.meta.env.VITE_API_TOKEN as string | undefined;
  if (token) headers.authorization = `Bearer ${token}`;
  const res = await fetch(`${API_BASE}${path}`, { ...init, headers });
  if (!res.ok) {
    throw new Error(`API ${res.status} ${path}`);
  }
  return (await res.json()) as T;
}

function nowIso(): string {
  return new Date().toISOString();
}

function mapQueueItem(
  item: ApiQueueItem,
): { rec: Recommendation; stats?: RecommendationStats } {
  // Defensive evidence parse (Option A): chips hide on malformed payloads.
  const evidence = parseEvidence(item.evidence_json);
  const gsc = extractGscSignals(evidence);
  const position = gsc?.position ?? extractPosition(item.diagnosis);
  const catalogue = evidence.find((e) => e.source === "catalogue");
  const inStockMatch = catalogue
    ? `${catalogue.value ?? ""} ${catalogue.finding}`.match(
        /([\d.,]+)\s*in[- ]stock/i,
      )
    : null;
  const inStockProducts = inStockMatch
    ? Number(inStockMatch[1].replace(/,/g, ""))
    : undefined;

  const rec: Recommendation = {
    recommendation_id: item.recommendation_id,
    site_id: item.site_id,
    generator: item.generator as GeneratorType,
    action_type: item.action_type as ActionType,
    target_url: item.target_url,
    proposed_url: item.proposed_url,
    diagnosis: item.diagnosis,
    evidence_json: evidence,
    work_required_json: [],
    primary_keyword: item.primary_keyword,
    search_volume: item.search_volume,
    impact: item.impact as ImpactLevel,
    confidence: item.confidence as ConfidenceLevel,
    effort: item.effort as EffortLevel,
    owner: item.owner as Owner,
    status: item.status as RecommendationStatus,
    assigned_to: item.assigned_to,
    measurement_metric: null,
    measurement_window_days: null,
    measurement_due_at: null,
    result: (item.result ?? "pending") as ResultClass,
    created_at: nowIso(),
    approved_at: null,
    implemented_at: null,
    rejected_at: null,
    rejection_reason: null,
    measured_at: null,
  };
  const hasVolume = item.search_volume != null && item.search_volume > 0;
  const hasGsc = gsc != null && (gsc.impressions > 0 || gsc.clicks > 0);
  if (!hasVolume && !hasGsc && inStockProducts == null && position == null) {
    return { rec, stats: undefined };
  }
  const stats: RecommendationStats = {
    volume: item.search_volume ?? 0,
    position,
    competitorCount: 0,
    impressions: gsc?.impressions ?? 0,
    clicks: gsc?.clicks ?? 0,
    ...(inStockProducts != null ? { inStockProducts } : {}),
  };
  return { rec, stats };
}

/** Defensive parse of a work_tasks-ish value into typed items.
 * Returns [] on anything malformed — task lists simply hide, never crash. */
export function parseWorkTasks(value: unknown): WorkRequiredItem[] {
  if (!Array.isArray(value)) return [];
  return value.filter(
    (item): item is WorkRequiredItem =>
      item != null &&
      typeof item === "object" &&
      typeof (item as WorkRequiredItem).task === "string",
  );
}

/* Manual task completion tracking. Automated tasks derive their status from
 * generated_fixes; manual tasks are operator-tracked client-side (localStorage
 * keyed by recommendation + task index). */
const MANUAL_DONE_KEY = "openseo.manualTaskDone";

export function readManualTaskDone(): Record<string, boolean> {
  try {
    return JSON.parse(
      localStorage.getItem(MANUAL_DONE_KEY) ?? "{}",
    ) as Record<string, boolean>;
  } catch {
    return {};
  }
}

export function writeManualTaskDone(key: string, done: boolean) {
  const current = readManualTaskDone();
  if (done) current[key] = true;
  else delete current[key];
  try {
    localStorage.setItem(MANUAL_DONE_KEY, JSON.stringify(current));
  } catch {
    /* storage unavailable — checkbox is best-effort UI state */
  }
}

export function manualTaskKey(recommendationId: string, index: number): string {
  return `${recommendationId}:${index}`;
}

/** Count completed tasks for a recommendation: automated tasks count when
 * their fix reached 'applied', manual tasks when the operator ticked them. */
export function taskProgress(
  rec: Pick<Recommendation, "recommendation_id" | "work_required_json">,
  manualDone: Record<string, boolean> = readManualTaskDone(),
): { done: number; total: number } {
  const tasks = rec.work_required_json ?? [];
  const done = tasks.filter((task, index) => {
    if (task.execution_type === "automated") {
      return task.fix_status === "applied";
    }
    return manualDone[manualTaskKey(rec.recommendation_id, index)] ?? false;
  }).length;
  return { done, total: tasks.length };
}

/** Defensive parse of an evidence_json-ish value into typed items.
 * Returns [] on anything malformed — chips simply hide, never crash. */
function parseEvidence(value: unknown): EvidenceItem[] {
  if (!Array.isArray(value)) return [];
  return value.filter(
    (item): item is EvidenceItem =>
      item != null &&
      typeof item === "object" &&
      typeof (item as EvidenceItem).finding === "string",
  );
}

/** Extract a rank position from evidence values or diagnosis text. Tolerant of
 * `=`, `:`, and phrasing like "avg_position = 6.53" or "ranked position 5.8";
 * returns null when no position is present. */
export function extractPosition(text: string | null | undefined): number | null {
  const match = String(text ?? "").match(
    /(?:avg_position|position)\s*[:=]?\s*(\d+(?:\.\d+)?)/i,
  );
  return match ? Number(match[1]) : null;
}

/** Pull compact GSC metrics out of structured evidence `value` strings for
 * card chips (Option A defensive parse). Evidence values look like
 * "56,790 impressions | 1,713 clicks | pos 5.07"; sources may be "GSC" or
 * "Google Search Console" — tolerant of format drift; returns null whenever
 * nothing usable is found, and chips hide. */
export function extractGscSignals(
  evidence: EvidenceItem[],
): { impressions: number; clicks: number; position: number | null } | null {
  for (const item of evidence) {
    if (item.source !== "GSC" && String(item.source) !== "Google Search Console") continue;
    const raw = `${item.value ?? ""} ${item.finding}`;
    const impMatch = raw.match(/([\d.,]+\s*k?)\s*impressions?/i);
    const clkMatch = raw.match(/([\d.,]+\s*k?)\s*clicks?/i);
    const parse = (s: string) =>
      Number(s.replace(/[,\s]/g, "").replace(/k$/i, "000"));
    const impressions = impMatch ? parse(impMatch[1]) : null;
    const clicks = clkMatch ? parse(clkMatch[1]) : null;
    const position = extractPosition(raw);
    if (impressions != null || clicks != null) {
      return { impressions: impressions ?? 0, clicks: clicks ?? 0, position };
    }
  }
  return null;
}

function mapPipelineItem(item: ApiPipelineItem): Recommendation {
  return {
    recommendation_id: item.recommendation_id,
    site_id: item.site_id,
    generator: item.generator as GeneratorType,
    action_type: item.action_type as ActionType,
    target_url: item.target_url,
    proposed_url: item.proposed_url,
    diagnosis: item.diagnosis,
    evidence_json: [],
    work_required_json: parseWorkTasks(item.work_tasks),
    primary_keyword: item.primary_keyword,
    search_volume: item.search_volume,
    impact: item.impact as ImpactLevel,
    confidence: "medium" as ConfidenceLevel,
    effort: "days" as EffortLevel,
    owner: "SEO" as Owner,
    status: item.status as RecommendationStatus,
    assigned_to: item.assigned_to,
    measurement_metric: null,
    measurement_window_days: item.observation_window_days,
    measurement_due_at: item.measurement_due_at,
    result: "pending" as ResultClass,
    created_at: item.approved_at ?? nowIso(),
    approved_at: item.approved_at,
    implemented_at: item.implemented_at,
    rejected_at: null,
    rejection_reason: null,
    measured_at: null,
  };
}

function mapMeasuredDetail(detail: ApiMeasuredDetail): MeasuredResult {
  const beforeClicks = detail.before?.clicks ?? null;
  const afterClicks = detail.after?.clicks ?? null;
  return {
    recommendation_id: detail.recommendation_id,
    generator: detail.generator as GeneratorType,
    action_type: detail.action_type as ActionType,
    target_url: detail.url ?? "",
    metric: "organic clicks",
    result: (detail.verdict ?? "neutral") as Exclude<ResultClass, "pending">,
    delta:
      beforeClicks != null && afterClicks != null
        ? afterClicks - beforeClicks
        : 0,
    relative_change: null,
    significance: null,
    measured_at: nowIso(),
    implemented_at: nowIso(),
    before: detail.before
      ? [
          {
            snapshot_id: `${detail.recommendation_id}-before`,
            recommendation_id: detail.recommendation_id,
            comparison_type: "target" as const,
            metric: "organic clicks",
            baseline_value: beforeClicks,
            current_value: beforeClicks,
            relative_change: null,
            impressions: detail.before.impressions ?? null,
            orders: detail.before.orders ?? null,
            revenue: detail.before.revenue ?? null,
          },
        ]
      : [],
    after: detail.after
      ? [
          {
            snapshot_id: `${detail.recommendation_id}-after`,
            recommendation_id: detail.recommendation_id,
            comparison_type: "target" as const,
            metric: "organic clicks",
            baseline_value: afterClicks,
            current_value: afterClicks,
            relative_change: null,
            impressions: detail.after.impressions ?? null,
            orders: detail.after.orders ?? null,
            revenue: detail.after.revenue ?? null,
          },
        ]
      : [],
    summary: detail.cluster ?? "",
  };
}

function mapRecentItem(item: ApiRecentItem): MeasuredResult {
  return {
    recommendation_id: item.recommendation_id,
    generator: item.generator as GeneratorType,
    action_type: item.action_type as ActionType,
    target_url: item.target_url ?? item.proposed_url ?? "",
    metric: "",
    result: (item.result ?? "neutral") as Exclude<ResultClass, "pending">,
    delta: 0,
    relative_change: null,
    significance: null,
    measured_at: item.measured_at ?? nowIso(),
    implemented_at: nowIso(),
    before: [],
    after: [],
    summary: item.diagnosis ?? "",
  };
}

async function loadQueue(siteId: string) {
  const out = await api<ApiQueueOut>(
    `/queue?site_id=${encodeURIComponent(siteId)}&limit=100`,
  );
  const recommendations: Recommendation[] = [];
  const stats: Record<string, RecommendationStats> = {};
  for (const item of out.recommendations) {
    const { rec, stats: itemStats } = mapQueueItem(item);
    recommendations.push(rec);
    if (itemStats) stats[rec.recommendation_id] = itemStats;
  }
  return { recommendations, stats };
}

async function loadPipeline(siteId: string) {
  const out = await api<ApiPipelineOut>(
    `/pipeline?site_id=${encodeURIComponent(siteId)}`,
  );
  return { recommendations: out.items.map(mapPipelineItem) };
}

async function loadResults(siteId: string) {
  const out = await api<ApiResultsOut>(
    `/results?site_id=${encodeURIComponent(siteId)}`,
  );
  const byId = new Map<string, MeasuredResult>();
  for (const detail of out.measured_details) {
    byId.set(detail.recommendation_id, mapMeasuredDetail(detail));
  }
  for (const item of out.recent) {
    if (!byId.has(item.recommendation_id)) {
      byId.set(item.recommendation_id, mapRecentItem(item));
    }
  }
  return { measured: [...byId.values()] };
}

export function useOperatorData(siteId: string) {
  const queryClient = useQueryClient();
  const queueKey = ["openseo", siteId, "queue"] as const;
  const pipelineKey = ["openseo", siteId, "pipeline"] as const;
  const queue = useQuery({
    queryKey: queueKey,
    queryFn: () => loadQueue(siteId),
  });
  const pipeline = useQuery({
    queryKey: pipelineKey,
    queryFn: () => loadPipeline(siteId),
  });
  const results = useQuery({
    queryKey: ["openseo", siteId, "results"],
    queryFn: () => loadResults(siteId),
  });

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ["openseo", siteId] });
  };

  /** Optimistically mutate a recommendation row in cache (approve/advance). */
  const patchRecommendation = (id: string, patch: Partial<Recommendation>) => {
    const caches: readonly {
      key: readonly [string, string, string];
    }[] = [{ key: queueKey }, { key: pipelineKey }];
    for (const { key } of caches) {
      queryClient.setQueryData<{ recommendations: Recommendation[] }>(key, (current) => {
        if (!current) return current;
        return {
          ...current,
          recommendations: current.recommendations.map((rec) =>
            rec.recommendation_id === id ? { ...rec, ...patch } : rec,
          ),
        };
      });
    }
  };

  /** Optimistically REMOVE a card from the Action Queue cache. Approve/reject
   * take the row out of the operator's decision list instantly — the approved
   * card re-appears only in the Pipeline's APPROVED column (via the pipeline
   * cache patch + refetch). */
  const removeFromQueue = (id: string) => {
    queryClient.setQueryData<{ recommendations: Recommendation[] }>(
      queueKey,
      (current) =>
        current
          ? {
              ...current,
              recommendations: current.recommendations.filter(
                (rec) => rec.recommendation_id !== id,
              ),
            }
          : current,
    );
  };

  const approveMutation = useMutation({
    mutationFn: (id: string) =>
      api<{ recommendation_id: string; status: string }>(
        `/recommendations/${id}/approve`,
        { method: "POST" },
      ),
    onMutate: (id) => {
      removeFromQueue(id);
      patchRecommendation(id, {
        status: "approved",
        approved_at: nowIso(),
      });
    },
    onSettled: invalidate,
  });

  const rejectMutation = useMutation({
    mutationFn: ({ id, reason }: { id: string; reason?: string }) =>
      api<{ recommendation_id: string; status: string }>(
        `/recommendations/${id}/reject`,
        {
          method: "POST",
          body: JSON.stringify({ reason: reason ?? "No reason provided" }),
        },
      ),
    onMutate: ({ id, reason }) => {
      removeFromQueue(id);
      patchRecommendation(id, {
        status: "rejected",
        rejected_at: nowIso(),
        rejection_reason: reason ?? "No reason provided",
      });
    },
    onSettled: invalidate,
  });

  const implementMutation = useMutation({
    mutationFn: (id: string) =>
      api<{ recommendation_id: string; status: string; implemented_at?: string }>(
        `/recommendations/${id}/implement-safe`,
        { method: "POST", body: JSON.stringify({}) },
      ),
    onMutate: (id) => {
      patchRecommendation(id, {
        status: "in_progress",
        implemented_at: nowIso(),
      });
    },
    onSettled: invalidate,
  });

  const markLiveMutation = useMutation({
    mutationFn: (id: string) =>
      api<{ recommendation_id: string; status: string }>(
        `/recommendations/${id}/live`,
        { method: "POST" },
      ),
    onMutate: (id) => {
      patchRecommendation(id, { status: "live" });
    },
    onSettled: invalidate,
  });

  const data = useMemo<OperatorData | undefined>(() => {
    if (!queue.data || !pipeline.data || !results.data) return undefined;
    // Pipeline rows are the board truth (strict gating: only explicitly
    // approved / in_progress / measured). Queue rows (proposed) fill gaps
    // for the Action Queue and are filtered out by the board's stage maps.
    const seen = new Set<string>();
    const recommendations: Recommendation[] = [];
    for (const rec of pipeline.data.recommendations) {
      if (seen.has(rec.recommendation_id)) continue;
      seen.add(rec.recommendation_id);
      recommendations.push(rec);
    }
    for (const rec of queue.data.recommendations) {
      if (seen.has(rec.recommendation_id)) continue;
      seen.add(rec.recommendation_id);
      recommendations.push(rec);
    }
    return {
      recommendations,
      stats: queue.data.stats,
      measured: results.data.measured,
      runs: EMPTY_RUNS,
    };
  }, [queue.data, pipeline.data, results.data]);

  return {
    isPending: queue.isPending || pipeline.isPending || results.isPending,
    isError: queue.isError || pipeline.isError || results.isError,
    error: queue.error ?? pipeline.error ?? results.error,
    data,
    patchRecommendation,
    approve: (id: string) => approveMutation.mutate(id),
    reject: (id: string, reason?: string) => rejectMutation.mutate({ id, reason }),
    implement: (id: string) => implementMutation.mutate(id),
    markLive: (id: string) => markLiveMutation.mutate(id),
  };
}

/** Index a recommendation's barometer stats by id for O(1) row lookup. */
export function useRecStatIndex(
  stats: Record<string, RecommendationStats> | undefined,
) {
  return useMemo(() => stats ?? {}, [stats]);
}
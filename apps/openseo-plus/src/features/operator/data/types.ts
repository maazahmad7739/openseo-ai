// OpenSEO++ operator data shapes.
//
// These are the client-side contracts the three operator tabs render. They
// mirror the planned tables in plan/00-schema.sql (recommendations,
// measurement_snapshots, rejection_log, candidate_runs). The read side is
// backed by the live FastAPI API (see useOperatorData.ts); the component layer
// only depends on these types so swapping the data layer later is a

export type GeneratorType =
  | "missing_page"
  | "existing_opportunity"
  | "technical_fix"
  | "cannibalization";

export type ActionType =
  | "create_page"
  | "improve_page"
  | "consolidate"
  | "technical_fix";

export type ImpactLevel = "high" | "medium" | "low";

export type ConfidenceLevel = "high" | "medium" | "low";

export type EffortLevel = "hours" | "days";

export type Owner = "SEO" | "content" | "engineering";

export type RecommendationStatus =
  | "proposed"
  | "approved"
  | "in_progress"
  | "live"
  | "measured"
  | "rejected";

export type ResultClass =
  | "pending"
  | "won"
  | "neutral"
  | "lost"
  | "inconclusive";

export interface EvidenceItem {
  source: "GSC" | "catalogue" | "SERP" | "crawl";
  finding: string;
  /** Optional structured number the UI can surface as a stat chip. */
  value?: string;
}

/** Fix-engine lifecycle status for automated tasks (generated_fixes.status).
 * 'generated' is rendered as Drafted. */
export type FixStatus =
  | "generated"
  | "approved"
  | "queued"
  | "applied"
  | "failed"
  | "reverted"
  | "expired";

export interface WorkRequiredItem {
  owner: Owner;
  task: string;
  acceptance_criteria: string;
  /** automated = fix engine supports it (title, meta description, 301);
   * manual = operator does it by hand (Liquid theme, schema injection…). */
  execution_type?: "automated" | "manual";
  /** generated_fixes row linked to this task (automated only). */
  fix_id?: string;
  fix_status?: FixStatus;
}

export interface Recommendation {
  recommendation_id: string;
  site_id: string;
  generator: GeneratorType;
  action_type: ActionType;
  target_url: string | null;
  proposed_url: string | null;
  diagnosis: string;
  /**
   * Structured evidence (GSC / catalogue / SERP / crawl findings). Displayed
   * inline as an expandable "why" panel in the queue.
   */
  evidence_json: EvidenceItem[];
  work_required_json: WorkRequiredItem[];
  /** Primary keyword the recommendation clusters on, from keyword_clusters. */
  primary_keyword: string | null;
  /** Searches/mo for the primary keyword, from keyword_clusters. */
  search_volume: number | null;
  impact: ImpactLevel;
  confidence: ConfidenceLevel;
  effort: EffortLevel;
  owner: Owner;
  status: RecommendationStatus;
  assigned_to: string | null;
  measurement_metric: string | null;
  measurement_window_days: number | null;
  measurement_due_at: string | null;
  result: ResultClass;
  created_at: string;
  approved_at: string | null;
  implemented_at: string | null;
  rejected_at: string | null;
  rejection_reason: string | null;
  measured_at: string | null;
}

/** Card-level stats derived from live lookups (GSC volume/position, competitor
 * presence). Surfaces the same numbers the agent sees without burying them in
 * prose. Kept flat for the table/stat-chip rendering and populated by the data
 * layer alongside evidence. */
export interface RecommendationStats {
  volume: number;
  position: number | null;
  competitorCount: number;
  impressions: number;
  clicks: number;
  /** In-stock catalogue depth (from evidence) when the agent recorded it. */
  inStockProducts?: number;
}

export type QueueStatusFilter = "all" | RecommendationStatus;

export type QueueSortKey =
  | "impact"
  | "volume"
  | "generator"
  | "created_at";

export interface CandidateRun {
  run_id: string;
  run_type: "daily_sync" | "candidate_generation" | "agent_evaluation" | "measurement";
  status: "pending" | "running" | "completed" | "failed";
  started_at: string;
  finished_at: string | null;
  note: string | null;
}

export interface MeasurementSnapshot {
  snapshot_id: string;
  recommendation_id: string;
  comparison_type: "target" | "control" | "yoy";
  metric: string;
  baseline_value: number | null;
  current_value: number | null;
  relative_change: number | null;
  /** Business metrics (GA4) when the snapshot carried them. */
  impressions?: number | null;
  orders?: number | null;
  revenue?: number | null;
}

export interface MeasuredResult {
  recommendation_id: string;
  generator: GeneratorType;
  action_type: ActionType;
  target_url: string;
  metric: string;
  result: Exclude<ResultClass, "pending">;
  /** Absolute delta of the primary metric (e.g. clicks +312). */
  delta: number;
  /** Relative change of the primary metric (e.g. +18.4%). */
  relative_change: number | null;
  significance: number | null;
  measured_at: string;
  implemented_at: string;
  before: MeasurementSnapshot[];
  after: MeasurementSnapshot[];
  /** Human-readable one-line takeaway from the classification step. */
  summary: string;
}

export interface PipelineStage {
  key: RecommendationStatus | "raw";
  label: string;
  count: number;
}
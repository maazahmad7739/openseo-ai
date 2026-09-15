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

/**
 * OpenSEO++ display vocabulary.
 *
 * One source of truth for how every attribute (impact, generator, owner,
 * result, status) is labelled and colored so the badge system stays coherent
 * across the three tabs.
 */

export const impactMeta: Record<
  ImpactLevel,
  { label: string; badgeClass: string; dotClass: string }
> = {
  high: {
    label: "High impact",
    badgeClass: "tag-chip-rose",
    dotClass: "bg-error",
  },
  medium: {
    label: "Medium impact",
    badgeClass: "tag-chip-amber",
    dotClass: "bg-warning",
  },
  low: {
    label: "Low impact",
    badgeClass: "tag-chip-slate",
    dotClass: "bg-base-content/40",
  },
};

export const generatorMeta: Record<
  GeneratorType,
  { label: string; badgeClass: string }
> = {
  missing_page: { label: "Missing page", badgeClass: "tag-chip-violet" },
  existing_opportunity: {
    label: "Existing page",
    badgeClass: "tag-chip-emerald",
  },
  technical_fix: { label: "Technical", badgeClass: "tag-chip-sky" },
  cannibalization: { label: "Cannibalization", badgeClass: "tag-chip-fuchsia" },
};

export const actionTypeMeta: Record<ActionType, { label: string }> = {
  create_page: { label: "Create page" },
  improve_page: { label: "Improve page" },
  consolidate: { label: "Consolidate" },
  technical_fix: { label: "Technical fix" },
};

export const ownerMeta: Record<Owner, { label: string; badgeClass: string }> = {
  SEO: { label: "SEO", badgeClass: "tag-chip-slate" },
  content: { label: "Content", badgeClass: "tag-chip-emerald" },
  engineering: { label: "Engineering", badgeClass: "tag-chip-sky" },
};

/** White-on-color when the chip sits on a colored surface (kanban cards). */
export const ownerSolidClass: Record<Owner, string> = {
  SEO: "bg-slate-500",
  content: "bg-emerald-600",
  engineering: "bg-sky-600",
};

export const resultMeta: Record<
  ResultClass,
  { label: string; badgeClass: string; dotClass: string }
> = {
  won: { label: "Won", badgeClass: "tag-chip-emerald", dotClass: "bg-success" },
  neutral: {
    label: "Neutral",
    badgeClass: "tag-chip-slate",
    dotClass: "bg-base-content/40",
  },
  lost: { label: "Lost", badgeClass: "tag-chip-rose", dotClass: "bg-error" },
  inconclusive: {
    label: "Inconclusive",
    badgeClass: "tag-chip-amber",
    dotClass: "bg-warning",
  },
  pending: {
    label: "Pending",
    badgeClass: "tag-chip-slate",
    dotClass: "bg-base-content/30",
  },
};

export const statusMeta: Record<
  RecommendationStatus,
  { label: string; badgeClass: string; dotClass: string }
> = {
  proposed: {
    label: "Proposed",
    badgeClass: "tag-chip-violet",
    dotClass: "bg-violet-400",
  },
  approved: {
    label: "Approved",
    badgeClass: "tag-chip-emerald",
    dotClass: "bg-success",
  },
  in_progress: {
    label: "In progress",
    badgeClass: "tag-chip-sky",
    dotClass: "bg-info",
  },
  live: {
    label: "Live",
    badgeClass: "tag-chip-amber",
    dotClass: "bg-warning",
  },
  measured: {
    label: "Measured",
    badgeClass: "tag-chip-slate",
    dotClass: "bg-base-content/40",
  },
  rejected: {
    label: "Rejected",
    badgeClass: "tag-chip-rose",
    dotClass: "bg-error",
  },
};

export const confidenceMeta: Record<ConfidenceLevel, { label: string }> = {
  high: { label: "High confidence" },
  medium: { label: "Medium confidence" },
  low: { label: "Low confidence" },
};

export const effortMeta: Record<EffortLevel, { label: string }> = {
  hours: { label: "~hours" },
  days: { label: "~days" },
};

/** Shared badge shell: muted tinted chip with a ring, consistent sizing. */
export const chipBaseClass =
  "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium ring-1 ring-inset whitespace-nowrap";
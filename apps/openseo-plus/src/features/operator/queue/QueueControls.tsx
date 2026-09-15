import {
  CheckCheck,
  Search,
  SlidersHorizontal,
  X,
} from "lucide-react";
import type {
  ActionType,
  GeneratorType,
  ImpactLevel,
  QueueSortKey,
  RecommendationStatus,
} from "../data/types";
import { generatorMeta, impactMeta } from "../components/meta";

export type StatusFilter = "all" | RecommendationStatus;
export type GeneratorFilter = "all" | GeneratorType;
export type ImpactFilter = "all" | ImpactLevel;
export type OwnerFilter = "all" | "content" | "engineering" | "seo";
export type ActionTypeFilter = "all" | ActionType;

const STATUS_OPTIONS: Array<{ value: StatusFilter; label: string }> = [
  { value: "all", label: "All statuses" },
  { value: "proposed", label: "Proposed" },
  { value: "approved", label: "Approved" },
  { value: "in_progress", label: "In progress" },
  { value: "live", label: "Live" },
  { value: "measured", label: "Measured" },
  { value: "rejected", label: "Rejected" },
];

const OWNER_OPTIONS: Array<{ value: OwnerFilter; label: string }> = [
  { value: "all", label: "All owners" },
  { value: "content", label: "Content" },
  { value: "engineering", label: "Engineering" },
  { value: "seo", label: "SEO" },
];

const ACTION_TYPE_OPTIONS: Array<{ value: ActionTypeFilter; label: string }> = [
  { value: "all", label: "All action types" },
  { value: "create_page", label: "Create Page" },
  { value: "improve_page", label: "Improve Page" },
  { value: "consolidate", label: "Consolidate" },
  { value: "technical_fix", label: "Technical Fix" },
];

const SORT_OPTIONS: Array<{ value: QueueSortKey; label: string }> = [
  { value: "impact", label: "Sort by impact" },
  { value: "volume", label: "Sort by volume" },
  { value: "generator", label: "Sort by generator" },
  { value: "created_at", label: "Sort by date proposed" },
];

export function QueueToolbar({
  query,
  onQueryChange,
  status,
  onStatusChange,
  generator,
  onGeneratorChange,
  owner,
  onOwnerChange,
  actionType,
  onActionTypeChange,
  impact,
  onImpactChange,
  sortKey,
  onSortKeyChange,
}: {
  query: string;
  onQueryChange: (v: string) => void;
  status: StatusFilter;
  onStatusChange: (v: StatusFilter) => void;
  generator: GeneratorFilter;
  onGeneratorChange: (v: GeneratorFilter) => void;
  owner: OwnerFilter;
  onOwnerChange: (v: OwnerFilter) => void;
  actionType: ActionTypeFilter;
  onActionTypeChange: (v: ActionTypeFilter) => void;
  impact: ImpactFilter;
  onImpactChange: (v: ImpactFilter) => void;
  sortKey: QueueSortKey;
  onSortKeyChange: (v: QueueSortKey) => void;
}) {
  return (
    <div className="flex flex-col gap-3 rounded-xl border border-base-300 bg-base-100 p-3 shadow-sm lg:flex-row lg:items-center">
      <label className="input input-sm w-full lg:max-w-xs">
        <Search className="size-3.5 shrink-0 text-base-content/40" />
        <input
          type="text"
          value={query}
          placeholder="Search keyword, URL…"
          onChange={(e) => onQueryChange(e.target.value)}
        />
        {query ? (
          <button
            type="button"
            className="text-base-content/40 hover:text-base-content"
            onClick={() => onQueryChange("")}
            aria-label="Clear search"
          >
            <X className="size-3.5" />
          </button>
        ) : null}
      </label>

      <div className="flex flex-wrap items-center gap-2">
        <select
          className="select select-sm select-bordered"
          value={status}
          onChange={(e) => onStatusChange(e.target.value as StatusFilter)}
          aria-label="Filter by status"
        >
          {STATUS_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
        <select
          className="select select-sm select-bordered"
          value={generator}
          onChange={(e) => onGeneratorChange(e.target.value as GeneratorFilter)}
          aria-label="Filter by generator"
        >
          <option value="all">All generators</option>
          {Object.entries(generatorMeta).map(([value, meta]) => (
            <option key={value} value={value}>
              {meta.label}
            </option>
          ))}
        </select>
        <select
          className="select select-sm select-bordered"
          value={actionType}
          onChange={(e) => onActionTypeChange(e.target.value as ActionTypeFilter)}
          aria-label="Filter by action type"
        >
          {ACTION_TYPE_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
        <select
          className="select select-sm select-bordered"
          value={owner}
          onChange={(e) => onOwnerChange(e.target.value as OwnerFilter)}
          aria-label="Filter by owner"
        >
          {OWNER_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
        <select
          className="select select-sm select-bordered"
          value={impact}
          onChange={(e) => onImpactChange(e.target.value as ImpactFilter)}
          aria-label="Filter by impact"
        >
          <option value="all">All impact</option>
          {Object.entries(impactMeta).map(([value, meta]) => (
            <option key={value} value={value}>
              {meta.label}
            </option>
          ))}
        </select>

        <div className="mx-1 hidden h-5 w-px bg-base-300 lg:block" />

        <label className="flex items-center gap-1.5 text-xs text-base-content/60">
          <SlidersHorizontal className="size-3.5" />
          <select
            className="select select-sm select-bordered"
            value={sortKey}
            onChange={(e) => onSortKeyChange(e.target.value as QueueSortKey)}
            aria-label="Sort recommendations"
          >
            {SORT_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </label>
      </div>
    </div>
  );
}

export function QueueSummary({
  count,
  total,
  hasFilters,
  allSelected,
  hasProposed,
  onToggleAll,
  onReset,
}: {
  count: number;
  total?: number;
  hasFilters: boolean;
  allSelected: boolean;
  hasProposed: boolean;
  onToggleAll: () => void;
  onReset: () => void;
}) {
  return (
    <div className="flex items-center justify-between gap-3 text-sm text-base-content/60">
      <p className="tabular-nums">
        <span className="font-semibold text-base-content">
          {total != null && total > count ? `${count} of ${total}` : count}
        </span>{" "}
        recommendation{count === 1 ? "" : "s"}
        {total != null && total > count ? (
          <span className="text-base-content/40"> (top {count})</span>
        ) : null}
        {hasFilters ? (
          <span className="text-base-content/40">
            {" "}
            (filtered)
            <button type="button" className="ml-2 link link-primary" onClick={onReset}>
              Reset
            </button>
          </span>
        ) : null}
      </p>
      {hasProposed ? (
        <label className="flex cursor-pointer items-center gap-2 text-xs">
          <input
            type="checkbox"
            className="checkbox checkbox-sm"
            checked={allSelected}
            onChange={onToggleAll}
          />
          Select all proposed
        </label>
      ) : null}
    </div>
  );
}

export function QueueBulkBar({
  count,
  onClear,
  onReject,
  onApprove,
}: {
  count: number;
  onClear: () => void;
  onReject: () => void;
  onApprove: () => void;
}) {
  return (
    <div className="sticky bottom-4 z-20 flex flex-wrap items-center justify-between gap-3 rounded-xl border border-primary/30 bg-base-100 px-4 py-3 shadow-lg">
      <p className="text-sm font-medium">
        <span className="tabular-nums font-semibold">{count}</span> selected
      </p>
      <div className="flex items-center gap-2">
        <button type="button" className="btn btn-ghost btn-sm" onClick={onClear}>
          <X className="size-3.5" />
          Clear
        </button>
        <button type="button" className="btn btn-error btn-sm" onClick={onReject}>
          <X className="size-3.5" />
          Reject selected
        </button>
        <button type="button" className="btn btn-primary btn-sm" onClick={onApprove}>
          <CheckCheck className="size-3.5" />
          Approve selected
        </button>
      </div>
    </div>
  );
}
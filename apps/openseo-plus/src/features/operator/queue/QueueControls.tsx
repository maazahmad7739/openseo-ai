import { useCallback, useEffect, useRef, useState } from "react";
import {
  ArrowUpDown,
  CheckCheck,
  Plus,
  Search,
  X,
} from "lucide-react";
import type {
  ActionType,
  EffortLevel,
  GeneratorType,
  ImpactLevel,
  Owner,
  QueueSortKey,
  RecommendationStatus,
} from "../data/types";
import {
  actionTypeMeta,
  generatorMeta,
  impactMeta,
  ownerMeta,
  statusMeta,
} from "../components/meta";

export type StatusFilter = "all" | RecommendationStatus;
export type GeneratorFilter = "all" | GeneratorType;
export type ImpactFilter = "all" | ImpactLevel;
export type OwnerFilter = "all" | Owner;
export type ActionTypeFilter = "all" | ActionType;
export type EffortFilter = "all" | EffortLevel;

const FILTER_GROUPS = [
  {
    key: "status" as const,
    label: "Status",
    options: [
      { value: "proposed" as const, label: "Proposed" },
      { value: "approved" as const, label: "Approved" },
      { value: "in_progress" as const, label: "In progress" },
      { value: "live" as const, label: "Live" },
      { value: "measured" as const, label: "Measured" },
      { value: "rejected" as const, label: "Rejected" },
    ],
  },
  {
    key: "generator" as const,
    label: "Generator",
    options: (Object.entries(generatorMeta) as [GeneratorType, { label: string; badgeClass: string }][]).map(
      ([value, meta]) => ({ value, label: meta.label }),
    ),
  },
  {
    key: "actionType" as const,
    label: "Action type",
    options: (Object.entries(actionTypeMeta) as [ActionType, { label: string }][]).map(
      ([value, meta]) => ({ value, label: meta.label }),
    ),
  },
  {
    key: "owner" as const,
    label: "Owner",
    options: (Object.entries(ownerMeta) as [Owner, { label: string; badgeClass: string }][]).map(
      ([value, meta]) => ({ value, label: meta.label }),
    ),
  },
  {
    key: "impact" as const,
    label: "Impact",
    options: (Object.entries(impactMeta) as [ImpactLevel, { label: string; badgeClass: string; dotClass: string }][]).map(
      ([value, meta]) => ({ value, label: meta.label }),
    ),
  },
  {
    key: "effort" as const,
    label: "Effort",
    options: [
      { value: "hours" as const, label: "< 1 day" },
      { value: "days" as const, label: "Several days" },
    ],
  },
] as const;

const SORT_OPTIONS: Array<{ value: QueueSortKey; label: string }> = [
  { value: "impact", label: "Impact" },
  { value: "volume", label: "Volume" },
  { value: "generator", label: "Generator" },
  { value: "created_at", label: "Date proposed" },
];

type FilterPill = {
  group: (typeof FILTER_GROUPS)[number]["key"];
  value: string;
  label: string;
};

function buildPills(
  status: StatusFilter,
  generator: GeneratorFilter,
  owner: OwnerFilter,
  actionType: ActionTypeFilter,
  impact: ImpactFilter,
  effort: EffortFilter,
): FilterPill[] {
  const pills: FilterPill[] = [];
  if (status !== "all") pills.push({ group: "status", value: status, label: statusMeta[status as RecommendationStatus].label });
  if (generator !== "all") pills.push({ group: "generator", value: generator, label: generatorMeta[generator as GeneratorType].label });
  if (owner !== "all") pills.push({ group: "owner", value: owner, label: ownerMeta[owner as Owner]?.label ?? owner });
  if (actionType !== "all") pills.push({ group: "actionType", value: actionType, label: actionTypeMeta[actionType as ActionType].label });
  if (impact !== "all") pills.push({ group: "impact", value: impact, label: impactMeta[impact as ImpactLevel].label });
  if (effort !== "all") pills.push({ group: "effort", value: effort, label: effort === "hours" ? "< 1 day" : "Several days" });
  return pills;
}

const GROUP_LABEL: Record<(typeof FILTER_GROUPS)[number]["key"], string> = {
  status: "Status",
  generator: "Generator",
  actionType: "Action",
  owner: "Owner",
  impact: "Impact",
  effort: "Effort",
};

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
  effort,
  onEffortChange,
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
  effort: EffortFilter;
  onEffortChange: (v: EffortFilter) => void;
  sortKey: QueueSortKey;
  onSortKeyChange: (v: QueueSortKey) => void;
}) {
  const searchRef = useRef<HTMLInputElement>(null);
  const [openPopover, setOpenPopover] = useState(false);
  const popoverRef = useRef<HTMLDivElement>(null);
  const popoverBtnRef = useRef<HTMLButtonElement>(null);

  const pills = buildPills(status, generator, owner, actionType, impact, effort);
  const hasActiveFilters = pills.length > 0;

  const getFilterValue = useCallback(
    (group: (typeof FILTER_GROUPS)[number]["key"]): string => {
      switch (group) {
        case "status": return status;
        case "generator": return generator;
        case "owner": return owner;
        case "actionType": return actionType;
        case "impact": return impact;
        case "effort": return effort;
      }
    },
    [status, generator, owner, actionType, impact, effort],
  );

  const setFilterValue = useCallback(
    (group: (typeof FILTER_GROUPS)[number]["key"], value: string) => {
      switch (group) {
        case "status": onStatusChange(value as StatusFilter); break;
        case "generator": onGeneratorChange(value as GeneratorFilter); break;
        case "owner": onOwnerChange(value as OwnerFilter); break;
        case "actionType": onActionTypeChange(value as ActionTypeFilter); break;
        case "impact": onImpactChange(value as ImpactFilter); break;
        case "effort": onEffortChange(value as EffortFilter); break;
      }
    },
    [onStatusChange, onGeneratorChange, onOwnerChange, onActionTypeChange, onImpactChange, onEffortChange],
  );

  const removeFilter = useCallback(
    (group: (typeof FILTER_GROUPS)[number]["key"]) => {
      setFilterValue(group, "all");
    },
    [setFilterValue],
  );

  const clearAll = useCallback(() => {
    onStatusChange("all");
    onGeneratorChange("all");
    onOwnerChange("all");
    onActionTypeChange("all");
    onImpactChange("all");
    onEffortChange("all");
  }, [onStatusChange, onGeneratorChange, onOwnerChange, onActionTypeChange, onImpactChange, onEffortChange]);

  // Keyboard shortcuts: Ctrl+K anywhere, / when nothing focused
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement) return;
      if ((e.ctrlKey || e.metaKey) && e.key === "k") {
        e.preventDefault();
        searchRef.current?.focus();
        searchRef.current?.select();
        return;
      }
      if (e.key === "/" && !e.ctrlKey && !e.metaKey && !e.altKey) {
        e.preventDefault();
        searchRef.current?.focus();
        searchRef.current?.select();
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, []);

  // Close popover on outside click
  useEffect(() => {
    if (!openPopover) return;
    const handler = (e: MouseEvent) => {
      const target = e.target as Node;
      if (
        popoverRef.current &&
        !popoverRef.current.contains(target) &&
        popoverBtnRef.current &&
        !popoverBtnRef.current.contains(target)
      ) {
        setOpenPopover(false);
      }
    };
    window.addEventListener("mousedown", handler);
    return () => window.removeEventListener("mousedown", handler);
  }, [openPopover]);

  return (
    <div className="relative flex flex-col gap-2 rounded-lg border border-base-300 bg-base-100 px-3 py-2 shadow-sm lg:flex-row lg:items-center lg:gap-3">
      {/* Search */}
      <label className="input input-sm input-bordered flex min-w-0 flex-1 items-center gap-2 lg:max-w-none">
        <Search className="size-3.5 shrink-0 text-base-content/35" />
        <input
          ref={searchRef}
          type="text"
          value={query}
          placeholder="Search keyword, URL..."
          className="min-w-0 flex-1 bg-transparent text-sm outline-none placeholder:text-base-content/30"
          onChange={(e) => onQueryChange(e.target.value)}
        />
        {query ? (
          <button
            type="button"
            className="shrink-0 text-base-content/35 transition-colors hover:text-base-content"
            onClick={() => onQueryChange("")}
            aria-label="Clear search"
          >
            <X className="size-3.5" />
          </button>
        ) : (
          <kbd className="pointer-events-none hidden shrink-0 select-none rounded border border-base-300 bg-base-200 px-1.5 py-0.5 font-mono text-[10px] text-base-content/40 lg:inline-block">
            /
          </kbd>
        )}
      </label>

      {/* Quick presets */}
      <button
        type="button"
        className={`inline-flex shrink-0 items-center gap-1 whitespace-nowrap rounded-md border px-2.5 py-1 text-xs font-medium transition-colors ${
          impact === "high"
            ? "border-rose-300 bg-rose-50 text-rose-700"
            : "border-base-300 bg-base-100 text-base-content/60 hover:border-base-content/20 hover:text-base-content"
        }`}
        onClick={() => onImpactChange(impact === "high" ? "all" : "high")}
      >
        <span
          className={`size-1.5 rounded-full ${impact === "high" ? "bg-rose-500" : "bg-rose-400/50"}`}
        />
        High Impact
      </button>
      <button
        type="button"
        className={`inline-flex shrink-0 items-center gap-1 whitespace-nowrap rounded-md border px-2.5 py-1 text-xs font-medium transition-colors ${
          effort === "hours"
            ? "border-emerald-300 bg-emerald-50 text-emerald-700"
            : "border-base-300 bg-base-100 text-base-content/60 hover:border-base-content/20 hover:text-base-content"
        }`}
        onClick={() => onEffortChange(effort === "hours" ? "all" : "hours")}
      >
        <span
          className={`size-1.5 rounded-full ${effort === "hours" ? "bg-emerald-500" : "bg-emerald-400/50"}`}
        />
        Quick Wins
      </button>

      {/* Active filter pills */}
      {pills.map((pill) => (
        <span
          key={`${pill.group}:${pill.value}`}
          className="inline-flex shrink-0 items-center gap-1 whitespace-nowrap rounded-full border border-base-300 bg-base-200 px-2 py-0.5 text-xs font-medium text-base-content/70"
        >
          <span className="text-base-content/45">{GROUP_LABEL[pill.group]}:</span>
          {pill.label}
          <button
            type="button"
            className="ml-0.5 rounded-full p-px text-base-content/35 transition-colors hover:bg-base-content/10 hover:text-base-content"
            onClick={() => removeFilter(pill.group)}
            aria-label={`Remove ${pill.label} filter`}
          >
            <X className="size-3" />
          </button>
        </span>
      ))}

      {/* Clear all */}
      {hasActiveFilters ? (
        <button
          type="button"
          className="inline-flex shrink-0 items-center whitespace-nowrap text-xs text-base-content/40 transition-colors hover:text-base-content/70"
          onClick={clearAll}
        >
          Clear all
        </button>
      ) : null}

      {/* Filter popover trigger */}
      <button
        ref={popoverBtnRef}
        type="button"
        className={`inline-flex shrink-0 items-center gap-1 whitespace-nowrap rounded-md border px-2.5 py-1 text-xs font-medium transition-colors ${
          openPopover
            ? "border-primary/50 bg-primary/5 text-primary"
            : "border-base-300 bg-base-100 text-base-content/60 hover:border-base-content/20 hover:text-base-content"
        }`}
        onClick={() => setOpenPopover((v) => !v)}
      >
        <Plus className="size-3" />
        Filter
      </button>

      {/* Filter popover dropdown */}
      {openPopover ? (
        <div
          ref={popoverRef}
          className="absolute left-0 top-full z-30 mt-1.5 w-72 rounded-lg border border-base-300 bg-base-100 p-1.5 shadow-lg"
        >
          {FILTER_GROUPS.map((group, gi) => {
            const currentValue = getFilterValue(group.key);
            return (
              <div key={group.key}>
                {gi > 0 && <div className="my-1 h-px bg-base-200" />}
                <div className="px-2 py-1 text-[10px] font-semibold uppercase tracking-wider text-base-content/40">
                  {group.label}
                </div>
                <div className="flex flex-wrap gap-1 px-1 py-1">
                  {group.options.map((opt) => {
                    const active = currentValue === opt.value;
                    return (
                      <button
                        key={opt.value}
                        type="button"
                        className={`rounded-md border px-2 py-0.5 text-xs transition-colors ${
                          active
                            ? "border-primary/50 bg-primary/10 font-medium text-primary"
                            : "border-transparent bg-base-200/60 text-base-content/60 hover:bg-base-200 hover:text-base-content"
                        }`}
                        onClick={() =>
                          setFilterValue(group.key, active ? "all" : opt.value)
                        }
                      >
                        {active && <span className="mr-0.5">✓ </span>}
                        {opt.label}
                      </button>
                    );
                  })}
                </div>
              </div>
            );
          })}
        </div>
      ) : null}

      {/* Sort */}
      <div className="hidden shrink-0 lg:block">
        <div className="h-5 w-px bg-base-300" />
      </div>
      <label className="flex shrink-0 items-center gap-1.5 text-xs text-base-content/50">
        <ArrowUpDown className="size-3" />
        <select
          className="select select-sm select-bordered border-0 bg-transparent py-0.5 pl-0.5 pr-5 text-xs font-medium text-base-content/70 focus:border-0 focus:bg-transparent focus:outline-none"
          value={sortKey}
          onChange={(e) => onSortKeyChange(e.target.value as QueueSortKey)}
          aria-label="Sort recommendations"
        >
          {SORT_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>
              Sort: {o.label}
            </option>
          ))}
        </select>
      </label>
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

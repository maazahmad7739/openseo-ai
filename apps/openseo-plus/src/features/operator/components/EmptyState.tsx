import type { ComponentType, ReactNode } from "react";

const ACTION_SUGGESTIONS = {
  search: [
    "Try a different keyword or URL",
    "Reset to the full queue",
    "Loosen the filters above",
  ],
  filters: [
    "Clear the active filters",
    "Include rejected recommendations",
    "Switch to a different stage column",
  ],
  none: [
    "Run the weekly candidate pipeline to generate new recommendations",
    "Check that GSC / Shopify / GA4 connectors are connected",
  ],
} as const;

/**
 * Real empty state, not a bare "No recommendations." line. Includes an icon,
 * a short call-to-action appropriate to why the list is empty, and reset
 * affordances.
 */
export function EmptyState({
  icon: Icon,
  title,
  body,
  kind = "none",
  action,
}: {
  icon: ComponentType<{ className?: string }>;
  title: string;
  body?: ReactNode;
  kind?: keyof typeof ACTION_SUGGESTIONS;
  action?: ReactNode;
}) {
  const suggestions = ACTION_SUGGESTIONS[kind];
  return (
    <div className="flex flex-col items-center gap-4 rounded-2xl border border-dashed border-base-300 bg-base-200/30 px-6 py-14 text-center">
      <div className="flex size-14 items-center justify-center rounded-2xl bg-base-100 shadow-sm ring-1 ring-base-300">
        <Icon className="size-6 text-base-content/35" />
      </div>
      <div className="space-y-1.5 max-w-sm">
        <h3 className="text-base font-semibold text-base-content">{title}</h3>
        {body ? (
          <p className="text-sm leading-relaxed text-base-content/60">{body}</p>
        ) : null}
      </div>
      {suggestions.length > 0 ? (
        <ul className="space-y-1 text-xs text-base-content/45">
          {suggestions.map((s) => (
            <li key={s} className="flex items-center gap-1.5">
              <span className="size-1 rounded-full bg-base-content/25" />
              {s}
            </li>
          ))}
        </ul>
      ) : null}
      {action ? <div className="mt-1">{action}</div> : null}
    </div>
  );
}
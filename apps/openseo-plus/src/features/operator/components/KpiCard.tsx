import type { ReactNode } from "react";

/** Stat card used across Pipeline/Results pages: label, big value, sub line. */
export function KpiCard({
  label,
  value,
  sub,
  icon,
  tone = "neutral",
}: {
  label: string;
  value: ReactNode;
  sub?: string;
  icon?: ReactNode;
  tone?: "good" | "bad" | "neutral";
}) {
  const toneClass =
    tone === "good" ? "text-success" : tone === "bad" ? "text-error" : "";
  return (
    <div className="app-kpi">
      <div className="flex items-center justify-between gap-2">
        <p className="app-kpi-label">{label}</p>
        {icon ? <span className="text-base-content/30">{icon}</span> : null}
      </div>
      <p className={`app-kpi-value ${toneClass}`}>{value}</p>
      {sub ? <p className="app-kpi-sub">{sub}</p> : null}
    </div>
  );
}
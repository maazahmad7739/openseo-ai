import {
  ArrowDownRight,
  ArrowRight,
  ArrowUpRight,
  BadgeDollarSign,
  FlaskConical,
  Minus,
  X,
} from "lucide-react";
import type { MeasuredResult } from "../data/types";
import { Modal } from "@/components/Modal";
import { ResultBadge, GeneratorBadge, ActionTypeBadge } from "../components/Badge";
import { actionTypeMeta } from "../components/meta";

/**
 * Drill-down for an individual measured result. Surfaces the before/after
 * snapshots, the delta, and the significance level the measurement module
 * computed — not just a win/loss count.
 */
export function ResultDetailModal({
  result,
  onClose,
}: {
  result: MeasuredResult;
  onClose: () => void;
}) {
  const before = result.before[0];
  const after = result.after[0];
  const diff = deltaClass(result);

  return (
    <Modal maxWidth="max-w-2xl" onClose={onClose} labelledBy="result-detail-title">
      <div className="space-y-4">
        <div className="flex items-start justify-between gap-4">
          <div>
            <h3
              id="result-detail-title"
              className="text-lg font-semibold leading-tight"
            >
              <span className="font-mono text-sm text-base-content/60">
                {result.target_url}
              </span>
            </h3>
            <div className="mt-2 flex flex-wrap items-center gap-1.5">
              <GeneratorBadge generator={result.generator} />
              <ActionTypeBadge actionType={result.action_type} />
              <ResultBadge result={result.result} />
            </div>
          </div>
          <button
            type="button"
            className="btn btn-circle btn-ghost btn-sm"
            onClick={onClose}
            aria-label="Close"
          >
            <X className="size-4" />
          </button>
        </div>

        <p className="text-sm leading-relaxed text-base-content/70">{result.summary}</p>

        <div className="grid gap-px overflow-hidden rounded-lg border border-base-300 bg-base-300/70 sm:grid-cols-3">
          <MetricCell label="Primary metric" value={result.metric} />
          <MetricCell
            label="Change (target)"
            value={formatDelta(result)}
            valueTone={diff}
            sub={`${result.relative_change != null ? signPct(result.relative_change) : "—"} vs baseline`}
          />
          <MetricCell
            label="Significance"
            value={
              result.significance == null
                ? "Below threshold"
                : `${Math.round(result.significance * 100)}%`
            }
            valueTone={
              result.significance == null ? "neutral"
              : result.significance >= 0.95 ? "good"
              : result.significance >= 0.9 ? "warn"
              : "neutral"
            }
            sub={
              result.result === "inconclusive"
                ? "Extend the observation window"
                : undefined
            }
          />
        </div>

        {/* Before → after */}
        <div className="space-y-2.5">
          <div className="flex items-center gap-2 text-xs font-medium text-base-content/60">
            <FlaskConical className="size-3.5" />
            Before / after snapshots
          </div>
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
            <SnapshotTile
              label="Baseline"
              value={before?.baseline_value ?? null}
              metric={result.metric}
            />
            <ArrowRight className="mx-auto size-4 shrink-0 text-base-content/35 sm:mx-0" />
            <SnapshotTile
              label="After window"
              value={after?.current_value ?? null}
              metric={result.metric}
              tone={diff}
            />
          </div>
          {result.before.length > 1 ? (
            <ControlNote
              targetChange={result.before[0]?.relative_change ?? 0}
              controlChange={result.before[1]?.relative_change ?? null}
            />
          ) : null}
        </div>

        {/* Business impact (GA4): orders + revenue movement */}
        <BusinessImpact result={result} />

        <div className="text-xs text-base-content/45">
          Measured {new Date(result.measured_at).toLocaleDateString()} · implemented{" "}
          {new Date(result.implemented_at).toLocaleDateString()} ·{" "}
          {actionTypeMeta[result.action_type].label.toLowerCase()}
        </div>
      </div>
    </Modal>
  );
}

/** Orders + revenue movement from the GA4-fed snapshots. Hides entirely
 * when neither snapshot carries business metrics (honest absence). */
function BusinessImpact({ result }: { result: MeasuredResult }) {
  const beforeOrders = result.before[0]?.orders ?? null;
  const afterOrders = result.after[0]?.orders ?? null;
  const beforeRevenue = result.before[0]?.revenue ?? null;
  const afterRevenue = result.after[0]?.revenue ?? null;
  const hasOrders = beforeOrders != null || afterOrders != null;
  const hasRevenue = beforeRevenue != null || afterRevenue != null;
  if (!hasOrders && !hasRevenue) return null;

  const revDelta =
    beforeRevenue != null && afterRevenue != null
      ? afterRevenue - beforeRevenue
      : null;
  return (
    <div className="space-y-2.5">
      <div className="flex items-center gap-2 text-xs font-medium text-base-content/60">
        <BadgeDollarSign className="size-3.5" />
        Business impact (organic)
      </div>
      <div className="grid gap-px overflow-hidden rounded-lg border border-base-300 bg-base-300/70 sm:grid-cols-2">
        <div className="bg-base-100 px-4 py-3">
          <p className="text-[11px] uppercase tracking-wider text-base-content/50">
            Orders
          </p>
          <p className="mt-0.5 text-lg font-semibold tabular-nums">
            {fmtNum(beforeOrders)} → {fmtNum(afterOrders)}
          </p>
          {hasOrders && beforeOrders != null && afterOrders != null ? (
            <p
              className={`mt-0.5 text-[11px] font-medium tabular-nums ${
                afterOrders - beforeOrders >= 0 ? "text-success" : "text-error"
              }`}
            >
              {afterOrders - beforeOrders >= 0 ? "+" : "−"}
              {Math.abs(afterOrders - beforeOrders)} orders
            </p>
          ) : null}
        </div>
        <div className="bg-base-100 px-4 py-3">
          <p className="text-[11px] uppercase tracking-wider text-base-content/50">
            Organic revenue
          </p>
          <p className="mt-0.5 text-lg font-semibold tabular-nums">
            {fmtMoney(beforeRevenue)} → {fmtMoney(afterRevenue)}
          </p>
          {revDelta != null ? (
            <p
              className={`mt-0.5 text-[11px] font-medium tabular-nums ${
                revDelta >= 0 ? "text-success" : "text-error"
              }`}
            >
              {revDelta >= 0 ? "+" : "−"}${Math.abs(revDelta).toFixed(2)}
            </p>
          ) : null}
        </div>
      </div>
    </div>
  );
}

function fmtNum(n: number | null): string {
  return n == null ? "—" : String(n);
}

function fmtMoney(n: number | null): string {
  if (n == null) return "—";
  return `$${n.toFixed(0)}`;
}

function MetricCell({
  label,
  value,
  valueTone = "neutral",
  sub,
}: {
  label: string;
  value: string;
  valueTone?: "good" | "warn" | "bad" | "neutral";
  sub?: string;
}) {
  const tone = {
    good: "text-success",
    warn: "text-warning",
    bad: "text-error",
    neutral: "text-base-content",
  }[valueTone];
  return (
    <div className="bg-base-100 px-4 py-3">
      <p className="text-[11px] uppercase tracking-wider text-base-content/50">
        {label}
      </p>
      <p className={`mt-0.5 text-lg font-semibold tabular-nums ${tone}`}>{value}</p>
      {sub ? (
        <p className="mt-0.5 text-[11px] text-base-content/45">{sub}</p>
      ) : null}
    </div>
  );
}

function SnapshotTile({
  label,
  value,
  metric,
  tone = "neutral",
}: {
  label: string;
  value: number | null;
  metric: string;
  tone?: "good" | "warn" | "bad" | "neutral";
}) {
  return (
    <div className="flex flex-1 items-center gap-3 rounded-xl border border-base-300 bg-base-200/40 px-4 py-3">
      <span
        className={`flex size-9 items-center justify-center rounded-lg ${
          tone === "good"
            ? "bg-success/15 text-success"
            : tone === "bad"
              ? "bg-error/15 text-error"
              : "bg-base-300/50 text-base-content/50"
        }`}
      >
        {tone === "good" ? (
          <ArrowUpRight className="size-4" />
        ) : tone === "bad" ? (
          <ArrowDownRight className="size-4" />
        ) : (
          <Minus className="size-4" />
        )}
      </span>
      <div className="min-w-0">
        <p className="text-[11px] uppercase tracking-wider text-base-content/50">
          {label}
        </p>
        <p className="text-lg font-semibold tabular-nums">
          {value == null ? "—" : fmtValue(value)}
        </p>
        <p className="truncate text-[11px] text-base-content/45">{metric}</p>
      </div>
    </div>
  );
}

function ControlNote({
  targetChange,
  controlChange,
}: {
  targetChange: number;
  controlChange: number | null;
}) {
  return (
    <div className="rounded-xl border border-base-300/80 bg-base-200/30 px-3.5 py-2.5 text-xs text-base-content/60">
      <span className="font-medium">Control group: </span>
      {controlChange == null ? (
        "no control comparison for this window."
      ) : (
        <>
          control {signPct(controlChange)} vs target {signPct(targetChange)} —
          {targetChange > controlChange ? (
            <span className="ml-1 font-medium text-success">
              the change clears the baseline band.
            </span>
          ) : (
            <span className="ml-1 font-medium text-base-content/80">
              the change does not clear the baseline band.
            </span>
          )}
        </>
      )}
    </div>
  );
}

function deltaClass(result: MeasuredResult): "good" | "warn" | "bad" | "neutral" {
  switch (result.result) {
    case "won":
      return "good";
    case "lost":
      return "bad";
    default:
      return "neutral";
  }
}

function formatDelta(result: MeasuredResult) {
  const sign = result.delta >= 0 ? "+" : "−";
  return `${sign}${fmtValue(Math.abs(result.delta))}`;
}

function signPct(n: number) {
  return `${n >= 0 ? "+" : "−"}${Math.abs(n).toFixed(1)}%`;
}

function fmtValue(n: number) {
  return n.toLocaleString("en-US", { maximumFractionDigits: 0 });
}
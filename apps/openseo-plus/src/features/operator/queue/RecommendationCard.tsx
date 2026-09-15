import { useState } from "react";
import {
  Check,
  ChevronDown,
  ExternalLink,
  FileText,
  X,
} from "lucide-react";
import type { Recommendation, RecommendationStats } from "../data/types";
import {
  ImpactBadge,
  GeneratorBadge,
  ActionTypeBadge,
  StatusBadge,
  OwnerBadge,
} from "../components/Badge";
import { StatChip } from "../components/StatChip";
import { EvidencePanel, WorkRequiredList } from "./evidence";
import { actionTypeMeta } from "../components/meta";

/** First sentence of the diagnosis becomes the scannable one-liner; the rest
 * lives in the collapsible "why" section with the evidence. */
export function splitDiagnosis(diagnosis: string) {
  const match = diagnosis.match(/(^[^.!?]+[.!?])\s+/);
  if (!match || match[1].length > 240) {
    return { summary: diagnosis, detail: "" };
  }
  return {
    summary: match[1],
    detail: diagnosis.slice(match[1].length).trim(),
  };
}

export function RecommendationCard({
  rec,
  stats,
  selected,
  expanded,
  domain,
  onToggleSelect,
  onToggleExpand,
  onApprove,
  onReject,
  onOpenDetail,
}: {
  rec: Recommendation;
  stats: RecommendationStats | undefined;
  selected: boolean;
  expanded: boolean;
  domain: string | null;
  onToggleSelect: () => void;
  onToggleExpand: () => void;
  onApprove: (reason?: string) => void;
  onReject: (reason?: string) => void;
  onOpenDetail?: () => void;
}) {
  const [rejecting, setRejecting] = useState(false);
  const [rejectReason, setRejectReason] = useState("");
  const { summary, detail } = splitDiagnosis(rec.diagnosis);
  const selectable = rec.status === "proposed";

  const url = rec.target_url ?? rec.proposed_url;
  const actionable = rec.status === "proposed";

  return (
    <div
      className={`card cursor-pointer border bg-base-100 transition-shadow hover:shadow-md ${
        selected
          ? "border-primary/50 shadow-[0_0_0_1px] shadow-primary/25"
          : "border-base-300"
      }`}
      onClick={() => onOpenDetail?.()}
    >
      <div className="card-body gap-3 p-4 md:p-5">
        {/* Header row: selection + badges */}
        <div className="flex items-start gap-3">
          <input
            type="checkbox"
            className="checkbox checkbox-sm mt-0.5 shrink-0"
            aria-label={`Select ${rec.recommendation_id}`}
            checked={selected}
            disabled={!selectable}
            onClick={(e) => e.stopPropagation()}
            onChange={onToggleSelect}
          />
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-1.5">
              <GeneratorBadge generator={rec.generator} />
              <ActionTypeBadge actionType={rec.action_type} />
              <ImpactBadge impact={rec.impact} />
              <StatusBadge status={rec.status} />
              <OwnerBadge owner={rec.owner} />
            </div>

            {/* Title / URL */}
            {url ? (
              <p className="mt-2 flex items-center gap-1.5 text-sm font-medium text-base-content">
                <FileText className="size-3.5 shrink-0 text-base-content/40" />
                <span className="truncate font-mono text-[13px]">{url}</span>
                {rec.target_url ? (
                  <span className="shrink-0 rounded bg-base-200 px-1.5 py-0.5 text-[10px] font-medium text-base-content/50">
                    {actionTypeMeta[rec.action_type].label}
                  </span>
                ) : null}
              </p>
            ) : null}
          </div>
        </div>

        {/* One-line summary */}
        <p className="text-sm leading-relaxed text-base-content/85">
          {summary}
        </p>

        {/* Stat chips — key numbers pulled out of the prose */}
        {stats ? (
          <div className="flex flex-wrap gap-2">
            {stats.volume > 0 ? <StatChip label="Volume" value={fmt(stats.volume)} /> : null}
            {stats.impressions > 0 ? (
              <StatChip label="Impressions" value={fmt(stats.impressions)} />
            ) : null}
            {stats.clicks > 0 ? (
              <StatChip label="Clicks" value={fmt(stats.clicks)} />
            ) : null}
            <StatChip
              label="Position"
              value={stats.position ?? "—"}
              tone={stats.position != null && stats.position <= 3 ? "good" : "neutral"}
            />
            {stats.inStockProducts != null ? (
              <StatChip
                label="In stock"
                value={stats.inStockProducts}
                tone={stats.inStockProducts > 0 ? "good" : "bad"}
              />
            ) : null}
            <StatChip
              label="Effort"
              value={rec.effort === "days" ? "~several days" : "~a day"}
            />
          </div>
        ) : null}

        {/* Toggling evidence + work required inline */}
        {expanded ? (
          <>
            {detail ? (
              <p className="text-sm leading-relaxed text-base-content/70">
                {detail}
              </p>
            ) : null}
            {rec.evidence_json.length > 0 ? (
              <EvidencePanel evidence={rec.evidence_json} />
            ) : null}
            {rec.work_required_json.length > 0 ? (
              <WorkRequiredList work={rec.work_required_json} />
            ) : null}
          </>
        ) : null}

        {/* Footer: expand + actions */}
        <div className="flex flex-wrap items-center justify-between gap-2 pt-1">
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              onToggleExpand();
            }}
            className="btn btn-ghost btn-xs"
            aria-expanded={expanded}
          >
            <ChevronDown
              className={`size-3.5 transition-transform ${
                expanded ? "rotate-180" : ""
              }`}
            />
            {expanded ? "Hide details" : "Show evidence"}
          </button>

          <div className="flex items-center gap-2">
            {rejecting ? (
              <div className="flex items-center gap-2" onClick={(e) => e.stopPropagation()}>
                <input
                  type="text"
                  className="input input-xs w-52"
                  placeholder="Rejection reason…"
                  value={rejectReason}
                  autoFocus
                  onChange={(e) => setRejectReason(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") {
                      onReject(rejectReason.trim() || undefined);
                    }
                  }}
                />
                <button
                  type="button"
                  className="btn btn-error btn-xs"
                  disabled={!rejectReason.trim()}
                  onClick={() => onReject(rejectReason.trim())}
                >
                  Confirm
                </button>
                <button
                  type="button"
                  className="btn btn-ghost btn-xs"
                  onClick={() => setRejecting(false)}
                >
                  <X className="size-3.5" />
                </button>
              </div>
            ) : (
              <>
                {actionable ? (
                  <>
                    <button
                      type="button"
                      onClick={(e) => {
                        e.stopPropagation();
                        setRejecting(true);
                      }}
                      className="btn btn-ghost btn-xs text-error"
                    >
                      <X className="size-3.5" />
                      Reject
                    </button>
                    <button
                      type="button"
                      onClick={(e) => {
                        e.stopPropagation();
                        onApprove();
                      }}
                      className="btn btn-primary btn-xs"
                    >
                      <Check className="size-3.5" />
                      Approve
                    </button>
                  </>
                ) : rec.status === "approved" ? (
                  <button
                    type="button"
                    onClick={() => setRejecting(true)}
                    className="btn btn-ghost btn-xs text-error"
                  >
                    <X className="size-3.5" />
                    Reject
                  </button>
                ) : null}

                {url && domain ? (
                  <a
                    href={`https://${domain}${url.startsWith("/") ? url : `/${url}`}`}
                    target="_blank"
                    rel="noreferrer"
                    className="btn btn-ghost btn-xs"
                    title={url}
                  >
                    <ExternalLink className="size-3.5" />
                  </a>
                ) : null}
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

function fmt(n: number) {
  if (n >= 1000) return `${(n / 1000).toFixed(1)}K`;
  return String(n);
}
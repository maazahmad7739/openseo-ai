import type { ReactNode } from "react";
import {
  actionTypeMeta,
  chipBaseClass,
  confidenceMeta,
  effortMeta,
  generatorMeta,
  impactMeta,
  ownerMeta,
  resultMeta,
  statusMeta,
} from "./meta";
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

export function Chip({ className, children }: { className: string; children: ReactNode }) {
  return (
    <span className={`${chipBaseClass} ${className}`}>
      {children}
    </span>
  );
}

export function ImpactBadge({ impact }: { impact: ImpactLevel }) {
  const meta = impactMeta[impact];
  return <Chip className={meta.badgeClass}>{meta.label}</Chip>;
}

export function GeneratorBadge({ generator }: { generator: GeneratorType }) {
  const meta = generatorMeta[generator];
  return <Chip className={meta.badgeClass}>{meta.label}</Chip>;
}

export function ActionTypeBadge({ actionType }: { actionType: ActionType }) {
  return (
    <Chip className="tag-chip-slate">{actionTypeMeta[actionType].label}</Chip>
  );
}

export function OwnerBadge({ owner }: { owner: Owner }) {
  const meta = ownerMeta[owner] ?? { label: owner, badgeClass: "tag-chip-slate" };
  return <Chip className={meta.badgeClass}>{meta.label}</Chip>;
}

/** Solid (white-on-color) owner chip for colored surfaces like kanban cards. */
export function OwnerBadgeSolid({ owner }: { owner: Owner }) {
  const meta = ownerMeta[owner];
  const solid =
    meta && "solidClass" in meta ? meta.solidClass : "bg-slate-500";
  const label = meta?.label ?? owner;
  return (
    <span className={`${chipBaseClass} ${solid} text-white`}>{label}</span>
  );
}

export function ResultBadge({ result }: { result: ResultClass }) {
  const meta = resultMeta[result];
  return (
    <Chip className={meta.badgeClass}>
      <span className={`size-1.5 rounded-full ${meta.dotClass}`} />
      {meta.label}
    </Chip>
  );
}

export function StatusBadge({ status }: { status: RecommendationStatus }) {
  const meta = statusMeta[status];
  return (
    <Chip className={meta.badgeClass}>
      <span className={`size-1.5 rounded-full ${meta.dotClass}`} />
      {meta.label}
    </Chip>
  );
}

export function ConfidenceBadge({ confidence }: { confidence: ConfidenceLevel }) {
  return <Chip className="tag-chip-slate">{confidenceMeta[confidence].label}</Chip>;
}

export function EffortBadge({ effort }: { effort: EffortLevel }) {
  return <Chip className="tag-chip-slate">{effortMeta[effort].label}</Chip>;
}
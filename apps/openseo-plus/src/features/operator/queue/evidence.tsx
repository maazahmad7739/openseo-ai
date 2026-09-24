import { CheckCircle2, ScrollText, Wrench } from "lucide-react";
import type { EvidenceItem, WorkRequiredItem } from "../data/types";
import { Chip } from "../components/Badge";
import { ExecutionTypeBadge } from "../components/WorkTaskList";

/** Badge for the source of an evidence finding (GSC / catalogue / SERP / crawl). */
export function SourceBadge({ source }: { source: EvidenceItem["source"] }) {
  const classBySource: Record<EvidenceItem["source"], string> = {
    GSC: "tag-chip-sky",
    catalogue: "tag-chip-emerald",
    SERP: "tag-chip-violet",
    crawl: "tag-chip-amber",
  };
  const labelBySource: Record<EvidenceItem["source"], string> = {
    GSC: "Google Search Console",
    catalogue: "Catalogue",
    SERP: "SERP",
    crawl: "Crawl",
  };
  return <Chip className={classBySource[source]}>{labelBySource[source]}</Chip>;
}

/**
 * Inline preview of `evidence_json` — the structured per-source findings that
 * back a recommendation. This is what the old queue hid behind a "Review"
 * click-through; here it renders as a collapsible panel on the card.
 */
export function EvidencePanel({ evidence }: { evidence: EvidenceItem[] }) {
  return (
    <div className="mt-3 space-y-2 rounded-xl border border-base-300/80 bg-base-200/30 p-3">
      <div className="flex items-center gap-2 text-xs font-medium text-base-content/70">
        <ScrollText className="size-3.5 text-base-content/40" />
        Why this recommendation
      </div>
      <ul className="space-y-2.5">
        {evidence.map((item, index) => (
          <li key={index} className="flex items-start gap-2.5 text-sm leading-relaxed">
            <span className="mt-1 size-1.5 shrink-0 rounded-full bg-primary/60" />
            <span className="min-w-0">
              <SourceBadge source={item.source} />
              <span className="ml-2 text-base-content/80">{item.finding}</span>
              {item.value ? (
                <span className="ml-1.5 rounded bg-base-100 px-1.5 py-0.5 font-mono text-[11px] font-medium text-primary ring-1 ring-inset ring-base-300">
                  {item.value}
                </span>
              ) : null}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

/** Flat work-required breakdown (owner · task · acceptance criteria). */
export function WorkRequiredList({
  work,
}: {
  work: WorkRequiredItem[];
}) {
  return (
    <div className="mt-3 flex flex-col gap-2.5 rounded-xl border border-base-300/80 bg-base-200/30 p-3">
      <div className="flex items-center gap-2 text-xs font-medium text-base-content/70">
        <Wrench className="size-3.5 text-base-content/40" />
        Work required
      </div>
      {work.map((item, index) => (
        <div key={index} className="flex items-start gap-2.5 text-sm">
          <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-base-content/30" />
          <div className="min-w-0 leading-relaxed">
            <p className="text-base-content/85">
              <ExecutionTypeBadge executionType={item.execution_type} />
              <span className="ml-2 font-medium">{item.task}</span>
            </p>
            <p className="text-xs text-base-content/50">
              <span className="font-medium text-base-content/60">{item.owner}</span>
              {" — "}
              {item.acceptance_criteria}
            </p>
          </div>
        </div>
      ))}
    </div>
  );
}
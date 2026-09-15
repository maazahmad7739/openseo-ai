import { useMemo, useState } from "react";
import { ArrowUpDown } from "lucide-react";
import { sort } from "remeda";
import type { MeasuredResult } from "../data/types";
import { actionTypeMeta, generatorMeta } from "../components/meta";
import { Chip } from "../components/Badge";

export interface BreakdownRow {
  id: string;
  generator: string;
  actionType: string;
  measured: number;
  won: number;
  neutral: number;
  lost: number;
  winRate: number | null;
}

type SortKey = "winRate" | "measured" | "won" | "lost";
type SortDir = "desc" | "asc";

/**
 * "Improvement rate by generator × action type" — rebuilt as a real sortable
 * table with a visual win-rate bar instead of unstyled text rows.
 */
export function useBreakdown(results: MeasuredResult[]): BreakdownRow[] {
  return useMemo(() => {
    const map = new Map<string, BreakdownRow>();
    for (const r of results) {
      const g = generatorMeta[r.generator].label;
      const a = actionTypeMeta[r.action_type].label;
      const id = `${r.generator}::${r.action_type}`;
      const row = map.get(id) ?? {
        id,
        generator: g,
        actionType: a,
        measured: 0,
        won: 0,
        neutral: 0,
        lost: 0,
        winRate: null,
      };
      row.measured += 1;
      if (r.result === "won") row.won += 1;
      if (r.result === "neutral") row.neutral += 1;
      if (r.result === "lost") row.lost += 1;
      row.winRate = row.measured > 0 ? row.won / row.measured : null;
      map.set(id, row);
    }
    return sort([...map.values()], (a, b) => (b.winRate ?? -1) - (a.winRate ?? -1));
  }, [results]);
}

const COLUMNS: Array<{ key: SortKey; label: string }> = [
  { key: "winRate", label: "Win rate" },
  { key: "measured", label: "Measured" },
  { key: "won", label: "Won" },
  { key: "lost", label: "Lost" },
];

export function BreakdownTable({
  results,
  onDrill,
}: {
  results: MeasuredResult[];
  onDrill: (id: string) => void;
}) {
  const rows = useBreakdown(results);
  const [sortKey, setSortKey] = useState<SortKey>("winRate");
  const [sortDir, setSortDir] = useState<SortDir>("desc");

  const sorted = useMemo(() => {
    const dir = sortDir === "desc" ? -1 : 1;
    return sort(rows, (a, b) => {
      const av = a[sortKey] ?? -1;
      const bv = b[sortKey] ?? -1;
      return (Number(av) - Number(bv)) * dir;
    });
  }, [rows, sortKey, sortDir]);

  const toggleSort = (key: SortKey) => {
    if (key === sortKey) {
      setSortDir((d) => (d === "desc" ? "asc" : "desc"));
    } else {
      setSortKey(key);
      setSortDir("desc");
    }
  };

  if (rows.length === 0) {
    return (
      <div className="rounded-lg border border-dashed border-base-300 py-10 text-center text-sm text-base-content/50">
        No measured results to break down yet
      </div>
    );
  }

  return (
    <div className="overflow-x-auto">
      <table className="table table-sm">
        <thead>
          <tr className="text-[11px] uppercase tracking-wider text-base-content/50">
            <th>Generator</th>
            <th>Action type</th>
            {COLUMNS.map((col) => (
              <th key={col.key} className="text-right">
                <button
                  type="button"
                  onClick={() => toggleSort(col.key)}
                  className={`inline-flex items-center gap-1 ${
                    sortKey === col.key ? "text-primary" : "hover:text-base-content"
                  }`}
                >
                  {col.label}
                  <ArrowUpDown className="size-3" />
                </button>
              </th>
            ))}
            <th className="w-40">Improvement</th>
            <th aria-label="Actions" />
          </tr>
        </thead>
        <tbody>
          {sorted.map((row) => (
            <tr key={row.id} className="border-base-300/70">
              <td className="font-medium">{row.generator}</td>
              <td className="text-base-content/70">{row.actionType}</td>
              <td className="text-right tabular-nums">{row.measured}</td>
              <td className="text-right tabular-nums text-success">{row.won}</td>
              <td className="text-right tabular-nums text-error">{row.lost}</td>
              <td>
                <BarCell value={row.winRate} />
              </td>
              <td className="text-right">
                <button
                  type="button"
                  className="btn btn-ghost btn-xs"
                  onClick={() => onDrill(row.id)}
                >
                  Drill
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function BarCell({ value }: { value: number | null }) {
  if (value == null) {
    return <span className="text-xs text-base-content/40">—</span>;
  }
  const pct = Math.round(value * 100);
  const tone =
    pct >= 60 ? "bg-success" : pct >= 30 ? "bg-warning" : "bg-error";
  return (
    <div className="flex items-center gap-2">
      <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-base-300/60">
        <div
          className={`h-full rounded-full ${tone}`}
          style={{ width: `${Math.max(pct, 4)}%` }}
        />
      </div>
      <span className="w-8 text-right text-xs font-medium tabular-nums">
        {pct}%
      </span>
    </div>
  );
}

export function ResultCountChips({ results }: { results: MeasuredResult[] }) {
  const won = results.filter((r) => r.result === "won").length;
  const neutral = results.filter((r) => r.result === "neutral").length;
  const lost = results.filter((r) => r.result === "lost").length;
  return (
    <div className="flex flex-wrap gap-1.5">
      <Chip className="tag-chip-emerald">Won {won}</Chip>
      <Chip className="tag-chip-slate">Neutral {neutral}</Chip>
      <Chip className="tag-chip-rose">Lost {lost}</Chip>
    </div>
  );
}
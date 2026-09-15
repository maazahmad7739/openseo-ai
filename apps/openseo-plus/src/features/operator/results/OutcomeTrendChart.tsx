import { useMemo } from "react";
import {
  Area,
  CartesianGrid,
  AreaChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { TooltipContentProps } from "recharts";
import { resultMeta } from "../components/meta";
import type { MeasuredResult, ResultClass } from "../data/types";
import { useChartWidth } from "@/hooks/useChartWidth";

const SERIES: Array<{ key: ResultClass; color: string; stackId: string }> = [
  { key: "won", color: "var(--color-success)", stackId: "a" },
  { key: "neutral", color: "var(--color-base-content)", stackId: "a" },
  { key: "lost", color: "var(--color-error)", stackId: "a" },
  { key: "inconclusive", color: "var(--color-warning)", stackId: "a" },
];

const DOT_COLORS: Record<ResultClass, string> = {
  won: "var(--color-success)",
  neutral: "var(--color-base-content)",
  lost: "var(--color-error)",
  inconclusive: "var(--color-warning)",
  pending: "var(--color-base-content)",
};

/**
 * Cumulative outcome trend: each point is the total number of measured
 * recommendations in each class by that week. Bucketed by Monday so the data
 * stays legible even when the sample is small.
 */
export function useOutcomeTrend(results: MeasuredResult[]) {
  return useMemo(() => {
    if (results.length === 0) return [];
    const first = new Date(
      Math.min(...results.map((r) => new Date(r.measured_at).getTime())),
    );
    const last = new Date();
    const week = (d: Date) => {
      const date = new Date(d);
      const day = (date.getDay() + 6) % 7;
      date.setDate(date.getDate() - day);
      date.setHours(0, 0, 0, 0);
      return date.getTime();
    };
    const start = week(first);
    const end = week(last);
    const totals: Record<ResultClass, number> = {
      won: 0,
      neutral: 0,
      lost: 0,
      inconclusive: 0,
      pending: 0,
    };
    const rows: Array<Record<string, number | string>> = [];
    for (let t = start; t <= end; t += 7 * 86_400_000) {
      rows.push({
        week: t,
        won: totals.won,
        neutral: totals.neutral,
        lost: totals.lost,
        inconclusive: totals.inconclusive,
      });
    }
    for (const r of results) {
      totals[r.result] += 1;
      const bucket = week(new Date(r.measured_at));
      const row = rows.find((x) => x.week === bucket);
      if (row) {
        row.won = totals.won;
        row.neutral = totals.neutral;
        row.lost = totals.lost;
        row.inconclusive = totals.inconclusive;
      }
    }
    return rows;
  }, [results]);
}

export function OutcomeTrendChart({ results }: { results: MeasuredResult[] }) {
  const data = useOutcomeTrend(results);
  const { containerRef, width } = useChartWidth();
  const total = results.length;

  if (total === 0) {
    return (
      <div className="flex h-56 items-center justify-center rounded-lg border border-dashed border-base-300 text-sm text-base-content/50">
        No measured outcomes yet
      </div>
    );
  }

  return (
    <div ref={containerRef} className="h-56 w-full min-w-0">
      {width > 0 ? (
        <ResponsiveContainer width={width} height="100%">
          <AreaChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
            <defs>
              {SERIES.map((s) => (
                <linearGradient key={s.key} id={`grad-${s.key}`} x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={s.color} stopOpacity={0.35} />
                  <stop offset="100%" stopColor={s.color} stopOpacity={0.04} />
                </linearGradient>
              ))}
            </defs>
            <CartesianGrid
              strokeDasharray="3 3"
              stroke="currentColor"
              opacity={0.1}
              vertical={false}
            />
            <XAxis
              dataKey="week"
              type="number"
              scale="time"
              domain={["dataMin", "dataMax"]}
              tickFormatter={(v: number) =>
                new Date(v).toLocaleDateString("en-US", { month: "short", day: "numeric" })
              }
              tick={{ fontSize: 10, fill: "#888" }}
              tickLine={false}
              axisLine={false}
              minTickGap={32}
            />
            <YAxis
              allowDecimals={false}
              tick={{ fontSize: 10, fill: "#888" }}
              tickLine={false}
              axisLine={false}
              width={28}
            />
            <Tooltip
              content={
                ((props: TooltipContentProps<number, string>) => {
                  const { active, payload, label } = props;
                  if (!active || !payload?.length || typeof label !== "number") return null;
                const idx = data.findIndex((row) => row.week === label);
                return (
                  <div className="rounded-lg border border-base-300 bg-base-100 px-3 py-2 text-xs shadow-lg">
                    <p className="mb-1.5 font-medium text-base-content">
                      {new Date(label).toLocaleDateString("en-US", {
                        month: "short",
                        day: "numeric",
                        year: "numeric",
                      })}
                    </p>
                    {payload.map((entry) => {
                      const key = String(entry.dataKey) as ResultClass;
                      const value = Number(entry.value ?? 0);
                      const prev = idx > 0 ? data[idx - 1][key] : 0;
                      const step = value - Number(prev ?? 0);
                      return (
                        <div
                          key={String(entry.dataKey)}
                          className="flex items-center justify-between gap-4 tabular-nums"
                        >
                          <span className="flex items-center gap-1.5 text-base-content/70">
                            <span
                              className="size-2 rounded-full"
                              style={{ backgroundColor: DOT_COLORS[key] }}
                            />
                            {resultMeta[key].label}
                          </span>
                          <span className="font-semibold text-base-content">
                            {value}
                            <span className="ml-1.5 font-normal text-base-content/40">
                              {step > 0 ? `+${step} this week` : ""}
                            </span>
                          </span>
                        </div>
                      );
                    })}
                  </div>
                );
                }) as never}
              />
            {SERIES.map((s) => (
              <Area
                key={s.key}
                type="monotone"
                dataKey={s.key}
                stackId={s.stackId}
                stroke={s.color}
                strokeWidth={2}
                fill={`url(#grad-${s.key})`}
                isAnimationActive={false}
              />
            ))}
          </AreaChart>
        </ResponsiveContainer>
      ) : null}
    </div>
  );
}
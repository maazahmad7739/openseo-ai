/**
 * Small stat chip that pulls a key number out of the diagnosis prose
 * (volume, position, competitor count, …) so the queue is scannable instead of
 * a wall of text.
 */
export function StatChip({
  label,
  value,
  tone = "neutral",
}: {
  label: string;
  value: string | number;
  tone?: "neutral" | "good" | "bad" | "accent";
}) {
  const toneClass: Record<string, string> = {
    neutral: "text-base-content",
    good: "text-success",
    bad: "text-error",
    accent: "text-primary",
  };
  return (
    <div className="flex min-w-0 flex-col gap-0.5 rounded-lg border border-base-300/70 bg-base-200/40 px-2.5 py-1.5">
      <span className="text-[10px] uppercase tracking-wider text-base-content/50 truncate">
        {label}
      </span>
      <span
        className={`text-sm font-semibold tabular-nums leading-tight ${toneClass[tone]}`}
      >
        {value}
      </span>
    </div>
  );
}

export function ChipRow({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex flex-wrap items-center gap-1.5">{children}</div>
  );
}
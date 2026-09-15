import type { ReactNode } from "react";

/** Block-level skeleton used while operator data loads. Matches DaisyUI's
 * shimmer so it reads as the same system as the rest of the app. */
export function SkeletonCard({
  lines = 3,
  className = "",
}: {
  lines?: number;
  className?: string;
}) {
  return (
    <div className={`card border border-base-300 bg-base-100 ${className}`}>
      <div className="card-body gap-3">
        <div className="flex items-start justify-between gap-4">
          <div className="flex-1 space-y-2">
            <div className="skeleton h-4 w-2/3" />
            <div className="skeleton h-3 w-1/3" />
          </div>
          <div className="skeleton size-16 rounded-lg" />
        </div>
        {Array.from({ length: lines }).map((_, i) => (
          <div key={i} className="skeleton h-3 w-full" />
        ))}
        <div className="flex gap-2 pt-1">
          {[20, 28, 16].map((w) => (
            <div key={w} className="skeleton h-5 w-20 rounded-full" />
          ))}
        </div>
      </div>
    </div>
  );
}

export function SkeletonStrip() {
  return (
    <div className="grid gap-px overflow-hidden rounded-lg border border-base-300 bg-base-300/70">
      {Array.from({ length: 4 }).map((_, i) => (
        <div key={i} className="bg-base-100 px-4 py-3">
          <div className="skeleton h-2.5 w-16" />
          <div className="skeleton mt-2 h-5 w-12" />
        </div>
      ))}
    </div>
  );
}

export function PageSkeleton() {
  return (
    <div className="mx-auto flex max-w-6xl flex-col gap-5 px-4 py-4 md:px-6 md:py-6" aria-busy>
      <div className="skeleton h-7 w-56" />
      <SkeletonStrip />
      <div className="grid gap-4 lg:grid-cols-2">
        <SkeletonCard />
        <SkeletonCard />
      </div>
      <SkeletonCard lines={4} />
    </div>
  );
}

export function DotBounceLoading({ label }: { label?: string }) {
  return (
    <span className="inline-flex items-center gap-2 text-sm text-base-content/60">
      <span className="loading loading-dots loading-sm" />
      {label}
    </span>
  );
}

export function InlineLabel({ children }: { children: ReactNode }) {
  return (
    <span className="text-[11px] uppercase tracking-wider text-base-content/50">
      {children}
    </span>
  );
}
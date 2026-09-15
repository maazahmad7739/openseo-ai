import { Link } from "@tanstack/react-router";
import { BarChart3, ClipboardList, GitBranch, Globe, Moon, Sun } from "lucide-react";
import { useTheme } from "@/hooks/useTheme";

const OPERATOR_TABS = [
  {
    to: "/p/$projectId/action-queue" as const,
    label: "Action Queue",
    icon: ClipboardList,
  },
  {
    to: "/p/$projectId/pipelines" as const,
    label: "Pipeline",
    icon: GitBranch,
  },
  {
    to: "/p/$projectId/results" as const,
    label: "Results",
    icon: BarChart3,
  },
];

/**
 * Page header shared by the three operator tabs.
 *
 * - Title + subtitle on the left.
 * - Brand/domain badge on the right (fixture brand for now; real site details
 *   once the backend connects).
 * - A tab strip linking the three operator tabs with an explicit active state.
 */
export function PageHeader({
  projectId,
  title,
  subtitle,
}: {
  projectId: string;
  title: string;
  subtitle: string;
}) {
  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div className="space-y-1">
          <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
          <p className="text-sm text-base-content/60">{subtitle}</p>
        </div>

        <div className="flex items-center gap-2">
          <ThemeToggle />
          <SiteBadge name={null} domain={null} siteId={projectId} />
        </div>
      </div>

      <div
        role="tablist"
        aria-label="Operator views"
        className="tabs tabs-border w-fit border-b border-base-300"
      >
        {OPERATOR_TABS.map(({ to, label, icon: Icon }) => (
          <Link
            key={to}
            to={to}
            params={{ projectId }}
            search={{}}
            role="tab"
            className="tab gap-1.5"
            activeOptions={{ includeSearch: false }}
            activeProps={{ className: "tab tab-active gap-1.5" }}
          >
            <Icon className="size-3.5" />
            {label}
          </Link>
        ))}
      </div>
    </div>
  );
}

function ThemeToggle() {
  const { theme, toggleTheme } = useTheme();
  const dark = theme === "openseo-dark";
  const label = dark ? "Switch to light mode" : "Switch to dark mode";
  return (
    <button
      type="button"
      onClick={toggleTheme}
      className="btn btn-ghost btn-square border border-base-300"
      aria-label={label}
      title={label}
    >
      {dark ? <Sun className="size-4.5" /> : <Moon className="size-4.5" />}
    </button>
  );
}

function SiteBadge({
  name,
  domain,
  siteId,
}: {
  name: string | null;
  domain: string | null;
  siteId: string;
}) {
  return (
    <div className="flex items-center gap-3 rounded-xl border border-base-300 bg-base-100 px-3.5 py-2.5 shadow-sm" title={siteId}>
      <div className="flex size-9 items-center justify-center rounded-lg bg-primary/10 text-primary">
        <Globe className="size-4.5" />
      </div>
      <div className="min-w-0 leading-tight">
        <p className="truncate text-sm font-semibold">
          {name ?? domain ?? "Site"}
        </p>
        {domain ? (
          <p className="truncate text-xs text-base-content/50">
            {domain}
            <span className="mx-1.5 text-base-content/25">·</span>
            <span className="font-mono text-[10px]">{siteId.slice(0, 8)}</span>
          </p>
        ) : (
          <p className="truncate text-xs text-base-content/50">
            <span className="font-mono text-[10px]">{siteId.slice(0, 8)}</span>
          </p>
        )}
      </div>
    </div>
  );
}
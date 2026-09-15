import { Link, useMatchRoute } from "@tanstack/react-router";
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
 * Sticky app navbar shared by the three operator views.
 *
 * - Brand + page context on the left.
 * - Segmented tab control centered (active tab lifts on a white pill).
 * - Theme toggle + site badge on the right.
 * Page title/subtitle render below the navbar on the page itself.
 */
export function PageHeader({
  projectId,
  title,
}: {
  projectId: string;
  title: string;
}) {
  const matchRoute = useMatchRoute();

  return (
    <>
      <nav className="app-navbar">
        <div className="mx-auto flex h-16 w-full max-w-6xl items-center justify-between gap-3 px-4 md:px-6">
          <div className="flex min-w-0 items-center gap-3">
            <Link
              to="/"
              className="flex size-9 shrink-0 items-center justify-center rounded-xl bg-primary text-primary-content shadow-sm"
              aria-label="OpenSEO home"
            >
              <Globe className="size-4.5" />
            </Link>
            <div className="min-w-0 leading-tight">
              <p className="truncate text-sm font-bold tracking-tight">
                ActionSEO
              </p>
              <p className="truncate text-[11px] text-base-content/50">
                {title}
              </p>
            </div>
          </div>

          <nav
            role="tablist"
            aria-label="Operator views"
            className="app-tabs hidden md:flex"
          >
            {OPERATOR_TABS.map(({ to, label, icon: Icon }) => {
              const match = matchRoute({
                to,
                params: { projectId },
                fuzzy: false,
              });
              const active = match !== false;
              return (
                <Link
                  key={to}
                  to={to}
                  params={{ projectId }}
                  search={{}}
                  role="tab"
                  aria-selected={active}
                  className={`app-tab ${active ? "app-tab-active" : ""}`}
                >
                  <Icon className="size-4" />
                  {label}
                </Link>
              );
            })}
          </nav>

          <div className="flex shrink-0 items-center gap-2">
            <ThemeToggle />
            <SiteBadge siteId={projectId} />
          </div>
        </div>

        {/* Mobile tabs */}
        <div className="border-t border-base-200 px-4 py-2 md:hidden">
          <nav role="tablist" aria-label="Operator views" className="app-tabs w-full">
            {OPERATOR_TABS.map(({ to, label, icon: Icon }) => {
              const match = matchRoute({
                to,
                params: { projectId },
                fuzzy: false,
              });
              const active = match !== false;
              return (
                <Link
                  key={to}
                  to={to}
                  params={{ projectId }}
                  search={{}}
                  role="tab"
                  aria-selected={active}
                  className={`app-tab flex-1 justify-center ${active ? "app-tab-active" : ""}`}
                >
                  <Icon className="size-4" />
                  {label}
                </Link>
              );
            })}
          </nav>
        </div>
      </nav>
    </>
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
      className="btn btn-ghost btn-square btn-sm border border-base-300 bg-base-100"
      aria-label={label}
      title={label}
    >
      {dark ? <Sun className="size-4" /> : <Moon className="size-4" />}
    </button>
  );
}

function SiteBadge({ siteId }: { siteId: string }) {
  return (
    <div
      className="flex items-center gap-2 rounded-xl border border-base-300 bg-base-100 py-1.5 pl-1.5 pr-3 shadow-sm"
      title={siteId}
    >
      <div className="flex size-7 items-center justify-center rounded-lg bg-primary/10 text-primary">
        <Globe className="size-4" />
      </div>
      <div className="hidden leading-tight sm:block">
        <p className="text-xs font-semibold">Aurora Audio</p>
        <p className="font-mono text-[10px] text-base-content/50">
          {siteId.slice(0, 8)}
        </p>
      </div>
    </div>
  );
}

/** Page title block rendered under the sticky navbar. */
export function PageTitle({
  title,
  subtitle,
  actions,
}: {
  title: string;
  subtitle: string;
  actions?: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
      <div className="space-y-1">
        <h1 className="text-2xl font-bold tracking-tight">{title}</h1>
        <p className="text-sm text-base-content/60">{subtitle}</p>
      </div>
      {actions ? <div className="flex items-center gap-2">{actions}</div> : null}
    </div>
  );
}
import { Link, useMatchRoute, useNavigate } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import { BarChart3, ChevronDown, ClipboardList, GitBranch, Globe, Moon, Sun } from "lucide-react";
import { useTheme } from "@/hooks/useTheme";
import { API_BASE } from "../data/useOperatorData";

const LAST_SITE_KEY = "openseo.lastSiteId";

interface SiteOption {
  site_id: string;
  site_name: string;
  domain: string;
}

/** Fetch the configured sites for the header switcher. */
function useSites(): SiteOption[] {
  const [sites, setSites] = useState<SiteOption[]>([]);
  useEffect(() => {
    let cancelled = false;
    fetch(`${API_BASE}/queue/sites`)
      .then((res) => (res.ok ? res.json() : { sites: [] }))
      .then((body: { sites?: SiteOption[] }) => {
        if (!cancelled) setSites(body.sites ?? []);
      })
      .catch(() => {
        if (!cancelled) setSites([]);
      });
    return () => {
      cancelled = true;
    };
  }, []);
  return sites;
}

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
            <SiteSwitcher projectId={projectId} />
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

/** Site switcher: dropdown of configured sites; changing navigates the
 * route param, which re-keys every site-scoped react-query cache. */
function SiteSwitcher({ projectId }: { projectId: string }) {
  const sites = useSites();
  const navigate = useNavigate();
  const current = sites.find((s) => s.site_id === projectId);
  const label = current?.site_name ?? "Site";
  const known = sites.length > 0;

  const onChange = (event: React.ChangeEvent<HTMLSelectElement>) => {
    const next = event.target.value;
    if (!next || next === projectId) return;
    localStorage.setItem(LAST_SITE_KEY, next);
    void navigate({
      to: "/p/$projectId/action-queue",
      params: { projectId: next },
    });
  };

  return (
    <div
      className="flex items-center gap-2 rounded-xl border border-base-300 bg-base-100 py-1.5 pl-1.5 pr-3 shadow-sm"
      title={current ? `${current.site_name} (${current.domain})` : projectId}
    >
      <div className="flex size-7 items-center justify-center rounded-lg bg-primary/10 text-primary">
        <Globe className="size-4" />
      </div>
      <div className="hidden leading-tight sm:block">
        {known ? (
          <>
            <select
              aria-label="Switch site"
              className="max-w-[160px] cursor-pointer truncate border-0 bg-transparent p-0 text-xs font-semibold outline-none"
              value={projectId}
              onChange={onChange}
            >
              {sites.map((site) => (
                <option key={site.site_id} value={site.site_id}>
                  {site.site_name || site.site_id.slice(0, 8)}
                </option>
              ))}
              {!current ? <option value={projectId}>{projectId.slice(0, 8)}</option> : null}
            </select>
            <p className="font-mono text-[10px] text-base-content/50">
              {projectId.slice(0, 8)}
            </p>
          </>
        ) : (
          <>
            <p className="text-xs font-semibold">{label}</p>
            <p className="font-mono text-[10px] text-base-content/50">
              {projectId.slice(0, 8)}
            </p>
          </>
        )}
      </div>
      {known ? <ChevronDown className="size-3.5 text-base-content/40 sm:hidden" /> : null}
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
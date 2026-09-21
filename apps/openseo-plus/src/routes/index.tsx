import { createFileRoute, redirect } from "@tanstack/react-router";
import { API_BASE } from "@/features/operator/data/useOperatorData";

const LAST_SITE_KEY = "openseo.lastSiteId";

/**
 * Resolve the landing project: the last site the operator viewed (kept in
 * localStorage), else the first site from /queue/sites. Falls back to the
 * legacy fixture-site UUID when the API is unreachable so the demo
 * deep-link keeps working offline.
 */
async function resolveLandingProject(): Promise<string> {
  const stored = localStorage.getItem(LAST_SITE_KEY);
  try {
    const res = await fetch(`${API_BASE}/queue/sites`);
    if (res.ok) {
      const body = (await res.json()) as {
        sites?: { site_id: string; site_name?: string }[];
      };
      const sites = body.sites ?? [];
      if (stored && sites.some((s) => s.site_id === stored)) return stored;
      if (sites.length > 0) {
        const first = sites[0];
        localStorage.setItem(LAST_SITE_KEY, first.site_id);
        return first.site_id;
      }
    }
  } catch {
    // fall through to legacy fallback
  }
  return stored ?? SITE_ID;
}

export const Route = createFileRoute("/")({
  beforeLoad: () =>
    resolveLandingProject().then((projectId) => {
      throw redirect({ to: "/p/$projectId/action-queue", params: { projectId } });
    }),
});

// Legacy Aurora fixture site — only used when /queue/sites is unreachable.
const SITE_ID = "c171d087-2264-4be8-b827-16fb663ba986";
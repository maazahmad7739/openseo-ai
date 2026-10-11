import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../data/useOperatorData";

/**
 * Data layer for the Off-page Queue tab (plan/34). Mirrors the backend
 * schemas in src/api/models/schemas.py. Copy/stage decisions and the
 * permalink submission are distinct mutations because the operator flow is
 * strictly manual: choose (copy) -> paste on the platform -> submit permalink.
 */

export interface OffpageGate {
  gate: string;
  outcome: "pass" | "refuse" | "skipped" | "unknown" | string;
  reason?: string | null;
}

export interface OffpageCaps {
  account_handle: string;
  account_promotional_cap_30d: number;
  account_promotional_30d: number;
  account_cap_reached: boolean;
  community_promotional_cap_30d: number;
  community_promotional_30d: number;
  community_cap_reached: boolean;
}

export interface OffpageQueueItem {
  draft_id: string;
  site_id: string;
  recommendation_id: string;
  version: number;
  status: "drafted" | "approved" | string;
  approved_at: string | null;
  created_at: string;
  client_name: string | null;
  domain: string | null;
  community_key: string | null;
  platform: "reddit" | "quora" | "medium" | "x" | "shopify_community" | string;
  post_kind: "reply" | "article" | string;
  thread_url: string | null;
  thread_title: string | null;
  thread_snippet: string | null;
  discovery_query: string | null;
  context_source: string | null;
  context_chars: number | null;
  reply_char_limit: number | null;
  post_char_limit: number | null;
  min_words: number | null;
  advice_md: string | null;
  mention_md: string | null;
  body_md: string;
  post_type: "helpful" | "promotional" | string;
  helpfulness_score: number | null;
  promo_density: number | null;
  brand_mention_count: number | null;
  link_count: number | null;
  gates: OffpageGate[];
  account_handle: string | null;
  caps: OffpageCaps | null;
  operator_choice: "helpful" | "promotional" | null;
}

export interface OffpageQueueOut {
  site_id: string;
  active_cap: number;
  total_active: number;
  total_pending: number;
  showing: number;
  items: OffpageQueueItem[];
}

export interface OffpageChooseOut {
  draft_id: string;
  status: string;
  action: "helpful" | "promotional";
  copied_md: string;
  mention_allowed: boolean;
  caps: OffpageCaps | null;
}

export interface OffpageSkipOut {
  draft_id: string;
  status: string;
  recommendation_id: string;
  recommendation_status: string;
  rejection_log_id: string | null;
}

export interface OffpageCheckpoint {
  checkpoint_index: number;
  offset_minutes: number;
  checkpoint_due_at: string;
}

/** One (site_id, platform) control row — the fail-closed knobs the discovery
 * job and drafter gates read (plan/35 §3). An absent row means "off". */
export interface OffpageChannelSettings {
  site_id: string;
  platform: string;
  enabled: boolean;
  discovery_seams: string[];
  discovery_calls_per_day: number;
  reply_char_limit: number | null;
  post_char_limit: number | null;
  min_words: number | null;
  promotional_posts_per_30d: number | null;
  targeting: Record<string, unknown>;
}

export interface OffpagePlatformSettingsOut {
  site_id: string;
  items: OffpageChannelSettings[];
  active_channels: number;
}

/** Discovery seams the job understands (provider keys in offpage_discovery).
 * `operator_import` is a data source, not a seam — it is not listed here. */
export const OFFPAGE_SEAMS = ["dataforseo", "web_search"] as const;

/** Every platform a channel row can exist for, in settings display order. */
export const OFFPAGE_PLATFORMS = [
  "reddit",
  "quora",
  "medium",
  "x",
  "shopify_community",
] as const;

export function useOffpagePlatformSettings(siteId: string) {
  const queryClient = useQueryClient();
  const settingsKey = ["openseo", siteId, "offpage-settings"] as const;

  const settings = useQuery({
    queryKey: settingsKey,
    queryFn: () =>
      api<OffpagePlatformSettingsOut>(
        `/offpage/platform-settings?site_id=${encodeURIComponent(siteId)}`,
      ),
  });

  const upsert = useMutation({
    mutationFn: (body: OffpageChannelSettings) =>
      api<OffpagePlatformSettingsOut>(
        `/offpage/platform-settings?site_id=${encodeURIComponent(siteId)}`,
        { method: "PUT", body: JSON.stringify(body) },
      ),
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: settingsKey });
      // Gates read the settings rows, so a saved channel re-keys drafts.
      void queryClient.invalidateQueries({
        queryKey: ["openseo", siteId, "offpage"],
      });
    },
  });

  return {
    settings,
    save: (body: OffpageChannelSettings) => upsert.mutateAsync(body),
    isSaving: upsert.isPending,
  };
}

export interface OffpagePermalinkOut {
  publication_id: string;
  draft_id: string;
  site_id: string;
  status: string;
  post_type: "helpful" | "promotional";
  live_permalink: string;
  submitted_at: string;
  recommendation_status: string;
  checkpoints: OffpageCheckpoint[];
}

/** "T+15m · T+2h · T+24h · T+72h" label for the four checkpoints. */
export function checkpointScheduleLabel(checkpoints: OffpageCheckpoint[]): string {
  const parts = [...checkpoints]
    .sort((a, b) => a.checkpoint_index - b.checkpoint_index)
    .map((c) => {
      const minutes = c.offset_minutes;
      if (minutes < 60) return `T+${minutes}m`;
      if (minutes < 1440) return `T+${minutes / 60}h`;
      return `T+${minutes / 1440}d`;
    });
  return parts.length ? parts.join(" · ") : "";
}

export function useOffpageQueue(siteId: string) {
  const queryClient = useQueryClient();
  const queueKey = ["openseo", siteId, "offpage"] as const;

  const queue = useQuery({
    queryKey: queueKey,
    queryFn: () =>
      api<OffpageQueueOut>(
        `/offpage/queue?site_id=${encodeURIComponent(siteId)}`,
      ),
  });

  /** Remove a draft card from the queue cache (skip / published). */
  const removeItem = (draftId: string) => {
    queryClient.setQueryData<OffpageQueueOut>(
      queueKey,
      (current) =>
        current
          ? {
              ...current,
              showing: Math.max(current.showing - 1, 0),
              total_active: Math.max(current.total_active - 1, 0),
              items: current.items.filter((i) => i.draft_id !== draftId),
            }
          : current,
    );
  };

  const choose = useMutation({
    mutationFn: ({
      draftId,
      action,
    }: {
      draftId: string;
      action: "helpful" | "promotional";
    }) =>
      api<OffpageChooseOut>(
        `/offpage/drafts/${draftId}/choose?site_id=${encodeURIComponent(
          siteId,
        )}`,
        {
          method: "POST",
          body: JSON.stringify({ action }),
        },
      ),
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: queueKey });
    },
  });

  const skip = useMutation({
    mutationFn: ({ draftId, reason }: { draftId: string; reason: string }) =>
      api<OffpageSkipOut>(
        `/offpage/drafts/${draftId}/skip?site_id=${encodeURIComponent(siteId)}`,
        {
          method: "POST",
          body: JSON.stringify({ reason }),
        },
      ),
    onSuccess: (out) => removeItem(out.draft_id),
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: queueKey });
    },
  });

  const publish = useMutation({
    mutationFn: ({
      draftId,
      permalink,
      operator,
    }: {
      draftId: string;
      permalink: string;
      operator?: string;
    }) =>
      api<OffpagePermalinkOut>(
        `/offpage/drafts/${draftId}/permalink?site_id=${encodeURIComponent(
          siteId,
        )}`,
        {
          method: "POST",
          body: JSON.stringify({ permalink, operator: operator || null }),
        },
      ),
    onSuccess: (out) => removeItem(out.draft_id),
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: queueKey });
    },
  });

  return {
    queue,
    isBusy: choose.isPending || skip.isPending || publish.isPending,
    choose: (draftId: string, action: "helpful" | "promotional") =>
      choose.mutateAsync({ draftId, action }),
    skip: (draftId: string, reason: string) =>
      skip.mutateAsync({ draftId, reason }),
    publish: (draftId: string, permalink: string, operator?: string) =>
      publish.mutateAsync({ draftId, permalink, operator }),
  };
}

/** Copy text to the clipboard with a fallback for non-secure contexts.
 * Returns true on success. */
export async function copyToClipboard(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    /* clipboard API blocked (insecure origin / permissions) — fallback */
  }
  try {
    const area = document.createElement("textarea");
    area.value = text;
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(area);
    return ok;
  } catch {
    return false;
  }
}
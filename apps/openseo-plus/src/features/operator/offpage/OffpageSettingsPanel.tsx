import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";
import { Gauge, RotateCcw, Save } from "lucide-react";
import {
  OFFPAGE_PLATFORMS,
  OFFPAGE_SEAMS,
  type OffpageChannelSettings,
  type OffpagePlatformSettingsOut,
  useOffpagePlatformSettings,
} from "./data";

const PLATFORM_LABELS: Record<string, string> = {
  reddit: "Reddit",
  quora: "Quora",
  medium: "Medium",
  x: "X / Twitter",
  shopify_community: "Shopify Community",
};

const SEAM_LABELS: Record<string, string> = {
  dataforseo: "SERP (DataForSEO)",
  web_search: "Web search (rate-limited)",
};

function defaultChannel(site: string, platform: string): OffpageChannelSettings {
  return {
    site_id: site,
    platform,
    enabled: false,
    discovery_seams: [],
    discovery_calls_per_day: 20,
    reply_char_limit: null,
    post_char_limit: null,
    min_words: null,
    promotional_posts_per_30d: null,
    targeting: {},
  };
}

function buildDrafts(out: OffpagePlatformSettingsOut) {
  const drafts: Record<string, OffpageChannelSettings> = {};
  for (const platform of OFFPAGE_PLATFORMS) {
    const found = out.items.find((i) => i.platform === platform);
    drafts[platform] = found
      ? { ...found, discovery_seams: [...(found.discovery_seams ?? [])] }
      : defaultChannel(out.site_id, platform);
  }
  return drafts;
}

/* Number-ish fields live as form text so the operator can clear them back
 * to null; only written as numbers (or null) on save. */
type NumField =
  | "discovery_calls_per_day"
  | "reply_char_limit"
  | "post_char_limit"
  | "min_words"
  | "promotional_posts_per_30d";

const NUM_FIELDS: NumField[] = [
  "discovery_calls_per_day",
  "reply_char_limit",
  "post_char_limit",
  "min_words",
  "promotional_posts_per_30d",
];

const NUM_LABELS: Record<NumField, string> = {
  discovery_calls_per_day: "Calls / day",
  reply_char_limit: "Reply char ceiling",
  post_char_limit: "Article char ceiling",
  min_words: "Article min words",
  promotional_posts_per_30d: "Promos / 30d",
};

function numToString(value: number | null | undefined): string {
  return value == null ? "" : String(value);
}

function parseNum(text: string): number | null {
  const trimmed = text.trim();
  if (trimmed === "") return null;
  const value = Number(trimmed);
  return Number.isFinite(value) && value >= 0 ? value : null;
}

export function OffpageSettingsPanel({ projectId }: { projectId: string }) {
  const { settings, save, isSaving } = useOffpagePlatformSettings(projectId);
  const [drafts, setDrafts] = useState<Record<string, OffpageChannelSettings> | null>(null);
  const [numText, setNumText] = useState<Record<string, Record<string, string>>>({});
  const [targetingText, setTargetingText] = useState<Record<string, string>>({});

  useEffect(() => {
    if (!settings.data || drafts) return;
    const built = buildDrafts(settings.data);
    setDrafts(built);
    const nums: Record<string, Record<string, string>> = {};
    const targeting: Record<string, string> = {};
    for (const platform of OFFPAGE_PLATFORMS) {
      const channel = built[platform];
      nums[platform] = {};
      for (const field of NUM_FIELDS) {
        nums[platform][field] = numToString(channel[field] as number | null);
      }
      const raw =
        channel.targeting && Object.keys(channel.targeting).length > 0
          ? channel.targeting
          : { shopify: { fit_keywords: [] } };
      targeting[platform] = JSON.stringify(raw, null, 2);
    }
    setNumText(nums);
    setTargetingText(targeting);
  }, [settings.data, drafts]);

  const summary = useMemo(() => {
    const enabled = OFFPAGE_PLATFORMS.filter((p) => drafts?.[p]?.enabled).length;
    return enabled;
  }, [drafts]);

  if (settings.isError) {
    return (
      <div className="alert alert-error">
        Could not load off-page platform settings.
      </div>
    );
  }

  if (!drafts) {
    return <div className="skeleton h-24 w-full" />;
  }

  const patchChannel = (platform: string, patch: Partial<OffpageChannelSettings>) => {
    setDrafts((current) =>
      current
        ? { ...current, [platform]: { ...current[platform], ...patch } }
        : current,
    );
  };

  const setNum = (platform: string, field: NumField, text: string) => {
    setNumText((current) => ({
      ...current,
      [platform]: { ...current[platform], [field]: text },
    }));
  };

  const toggleSeam = (platform: string, seam: string) => {
    setDrafts((current) => {
      if (!current) return current;
      const channel = current[platform];
      const has = channel.discovery_seams.includes(seam);
      const seams = has
        ? channel.discovery_seams.filter((s) => s !== seam)
        : [...channel.discovery_seams, seam];
      return { ...current, [platform]: { ...channel, discovery_seams: seams } };
    });
  };

  const saveChannel = async (platform: string) => {
    const channel = drafts[platform];
    const nums = numText[platform] ?? {};
    const merged: OffpageChannelSettings = {
      ...channel,
      discovery_calls_per_day: parseNum(nums.discovery_calls_per_day) ?? 20,
      reply_char_limit: parseNum(nums.reply_char_limit),
      post_char_limit: parseNum(nums.post_char_limit),
      min_words: parseNum(nums.min_words),
      promotional_posts_per_30d: parseNum(nums.promotional_posts_per_30d),
    };
    try {
      const raw = targetingText[platform]?.trim();
      if (raw && raw !== "{}") {
        merged.targeting = JSON.parse(raw) as Record<string, unknown>;
      } else {
        merged.targeting = {};
      }
    } catch {
      toast.error(`Targeting JSON for ${PLATFORM_LABELS[platform] ?? platform} is invalid`);
      return;
    }
    try {
      const out = await save(merged);
      setDrafts(buildDrafts(out));
      toast.success(
        `${PLATFORM_LABELS[platform] ?? platform} ${merged.enabled ? "enabled" : "disabled"}`,
      );
    } catch (err) {
      toast.error(
        `Save failed: ${err instanceof Error ? err.message : "request failed"}`,
      );
    }
  };

  return (
    <div className="card border border-base-300 bg-base-100 shadow-sm">
      <div className="card-body gap-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <span className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wider text-base-content/50">
            <Gauge className="size-3.5" />
            Platform channels
          </span>
          <p className="text-xs text-base-content/50">
            {summary} of {OFFPAGE_PLATFORMS.length} active in generation &amp;
            discovery — fail-closed: an absent row means off
          </p>
        </div>

        <div className="grid gap-3 lg:grid-cols-2">
          {OFFPAGE_PLATFORMS.map((platform) => {
            const channel = drafts[platform];
            const nums = numText[platform] ?? {};
            return (
              <div key={platform} className="rounded-xl border border-base-300 bg-base-100 p-3">
                <div className="mb-2 flex items-center justify-between gap-2">
                  <span className="text-sm font-semibold">
                    {PLATFORM_LABELS[platform] ?? platform}
                  </span>
                  <label className="flex cursor-pointer items-center gap-2 text-xs text-base-content/70">
                    <input
                      type="checkbox"
                      className="toggle toggle-sm toggle-success"
                      checked={channel.enabled}
                      onChange={(e) =>
                        patchChannel(platform, { enabled: e.target.checked })
                      }
                    />
                    enabled
                  </label>
                </div>

                <div className="mb-2 flex flex-wrap gap-2">
                  {OFFPAGE_SEAMS.map((seam) => (
                    <label
                      key={seam}
                      className="flex cursor-pointer items-center gap-1.5 text-xs text-base-content/70"
                    >
                      <input
                        type="checkbox"
                        className="checkbox checkbox-xs"
                        checked={channel.discovery_seams.includes(seam)}
                        onChange={() => toggleSeam(platform, seam)}
                      />
                      {SEAM_LABELS[seam] ?? seam}
                    </label>
                  ))}
                </div>

                <div className="grid grid-cols-2 gap-2 gap-x-3">
                  {NUM_FIELDS.map((field) => (
                    <label key={field} className="flex items-center justify-between gap-2 text-xs">
                      <span className="text-base-content/60">{NUM_LABELS[field]}</span>
                      <input
                        type="number"
                        min={0}
                        className="input input-bordered input-xs w-24 text-right font-mono"
                        value={nums[field] ?? ""}
                        onChange={(e) => setNum(platform, field, e.target.value)}
                        placeholder={field === "discovery_calls_per_day" ? "20" : "—"}
                      />
                    </label>
                  ))}
                </div>

                <textarea
                  className="textarea textarea-bordered mt-2 w-full font-mono text-[10px] leading-tight"
                  rows={3}
                  value={targetingText[platform] ?? ""}
                  onChange={(e) =>
                    setTargetingText((current) => ({
                      ...current,
                      [platform]: e.target.value,
                    }))
                  }
                  aria-label={`Targeting JSON for ${platform}`}
                />

                <div className="mt-2 flex items-center justify-between gap-2">
                  <span className="text-[10px] text-base-content/40">
                    targeting (JSONB) · fit_keywords gate the shopify channel
                  </span>
                  <div className="flex gap-2">
                    <button
                      type="button"
                      className="btn btn-ghost btn-xs"
                      onClick={() =>
                        setTargetingText((current) => ({
                          ...current,
                          [platform]:
                            '{\n  "shopify": {\n    "fit_keywords": []\n  }\n}',
                        }))
                      }
                      disabled={isSaving}
                      title="Reset targeting JSON"
                    >
                      <RotateCcw className="size-3" />
                      Reset
                    </button>
                    <button
                      type="button"
                      className="btn btn-primary btn-xs"
                      onClick={() => void saveChannel(platform)}
                      disabled={isSaving}
                    >
                      <Save className="size-3" />
                      Save
                    </button>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
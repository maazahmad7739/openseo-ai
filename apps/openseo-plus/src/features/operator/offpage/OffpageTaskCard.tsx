import { useState } from "react";
import { toast } from "sonner";
import {
  AlertTriangle,
  Check,
  Clipboard,
  Copy,
  ExternalLink,
  Link2,
  MessageSquareText,
  Send,
  ShieldAlert,
  SkipForward,
} from "lucide-react";
import { Modal } from "@/components/Modal";
import { Chip } from "../components/Badge";
import {
  checkpointScheduleLabel,
  copyToClipboard,
  type OffpageChooseOut,
  type OffpageQueueItem,
  type OffpagePermalinkOut,
  type OffpageSkipOut,
} from "./data";

function permalinkPlaceholder(platform?: string): string {
  switch (platform) {
    case "x":
      return "https://x.com/yourhandle/status/…";
    case "shopify_community":
      return "https://community.shopify.com/c/…/m-p/123456";
    case "medium":
      return "https://medium.com/@handle/showpiece-…";
    case "quora":
      return "https://www.quora.com/…";
    default:
      return "https://www.reddit.com/r/…/comments/…";
  }
}

export function OffpageTaskCard({
  item,
  onChoose,
  onSkip,
  onPublish,
  busy,
}: {
  item: OffpageQueueItem;
  onChoose: (
    draftId: string,
    action: "helpful" | "promotional",
  ) => Promise<OffpageChooseOut>;
  onSkip: (draftId: string, reason: string) => Promise<OffpageSkipOut>;
  onPublish: (
    draftId: string,
    permalink: string,
    operator?: string,
  ) => Promise<OffpagePermalinkOut>;
  busy: boolean;
}) {
  const [skipOpen, setSkipOpen] = useState(false);
  const [skipReason, setSkipReason] = useState("");
  const [permalink, setPermalink] = useState("");
  const [operator, setOperator] = useState<string>(() => {
    try {
      return localStorage.getItem("openseo.offpageOperator") ?? "";
    } catch {
      return "";
    }
  });
  const [published, setPublished] = useState<OffpagePermalinkOut | null>(null);

  const caps = item.caps;
  const hasMention = Boolean(item.mention_md && item.mention_md.trim());
  const isPromotionalDraft = item.post_type === "promotional";
  const isArticle = item.post_kind === "article";
  const charLimit = isArticle
    ? (item.post_char_limit ?? null)
    : (item.reply_char_limit ?? null);
  const capBlocked = Boolean(
    caps &&
      (caps.account_cap_reached || caps.community_cap_reached),
  );
  const mentionGated = !hasMention || !isPromotionalDraft || !item.account_handle;
  const canCopyFull = hasMention && isPromotionalDraft && !capBlocked && !!item.account_handle;

  const adviceText = item.advice_md ?? item.body_md;
  const adviceChars = adviceText.length;

  const copyAdvice = async () => {
    try {
      const copied = await onChoose(item.draft_id, "helpful");
      const ok = await copyToClipboard(copied.copied_md);
      toast.success(ok ? "Advice copied — helpful" : "Copied (mark as helpful)");
    } catch (err) {
      toast.error(
        `Advice copy failed: ${err instanceof Error ? err.message : "request failed"}`,
      );
    }
  };

  const copyFull = async () => {
    try {
      const copied = await onChoose(item.draft_id, "promotional");
      const ok = await copyToClipboard(copied.copied_md);
      toast.success(
        ok
          ? "Full response copied (advice + mention) — promotional"
          : "Full response (promotional)",
      );
    } catch (err) {
      toast.error(
        `Full response copy failed: ${
          err instanceof Error ? err.message : "request failed"
        }`,
      );
    }
  };

  const confirmSkip = async () => {
    const reason = (skipReason ?? "").trim();
    if (!reason) {
      toast.error("Give a short reason for the skip (it feeds the learning loop).");
      return;
    }
    try {
      await onSkip(item.draft_id, reason);
      setSkipOpen(false);
      toast.success("Draft skipped — recommendation rejected");
    } catch (err) {
      toast.error(
        `Skip failed: ${err instanceof Error ? err.message : "request failed"}`,
      );
    }
  };

  const submitPermalink = async () => {
    const value = (permalink ?? "").trim();
    if (!value) {
      toast.error("Paste the public permalink before submitting.");
      return;
    }
    try {
      localStorage.setItem("openseo.offpageOperator", operator ?? "");
    } catch {
      /* storage unavailable — operator is best-effort */
    }
    try {
      const out = await onPublish(item.draft_id, value, operator);
      setPublished(out);
      toast.success("Permalink submitted — verification scheduled");
    } catch (err) {
      toast.error(
        `Permalink submit failed: ${
          err instanceof Error ? err.message : "request failed"
        }`,
      );
    }
  };

  const chipPostType =
    item.post_type === "promotional" ? "tag-chip-amber" : "tag-chip-emerald";
  const refusedGates = item.gates.filter((g) => g.outcome === "refuse");

  return (
    <article className="card border border-base-300 bg-base-100 shadow-sm">
      <div className="card-body gap-4">
        {/* Header: client + thread + type */}
        <header className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-semibold trailing-tight">
                {item.client_name ?? "Client"}
              </span>
              <Chip className="tag-chip-slate">{item.community_key ?? "community"}</Chip>
              <Chip className="tag-chip-slate">{item.platform}</Chip>
              <Chip className={chipPostType}>{item.post_type}</Chip>
              <Chip className="tag-chip-slate">
                {isArticle ? "article (long-form)" : "reply"}
              </Chip>
              {item.helpfulness_score != null ? (
                <Chip className="tag-chip-slate">
                  helpfulness {Number(item.helpfulness_score).toFixed(2)}
                </Chip>
              ) : null}
            </div>
            <a
              href={item.thread_url ?? undefined}
              target="_blank"
              rel="noreferrer noopener"
              className="mt-1.5 inline-flex max-w-full items-center gap-1.5 truncate text-sm font-medium text-primary hover:underline"
            >
              <ExternalLink className="size-3.5 shrink-0" />
              <span className="truncate">
                {item.thread_title ?? item.thread_url}
              </span>
            </a>
            <p className="mt-0.5 line-clamp-2 text-xs text-base-content/60">
              {item.thread_snippet ?? "No thread snippet available."}
            </p>
            <p className="mt-0.5 flex items-center gap-1 text-[11px] text-base-content/45">
              <MessageSquareText className="size-3" />
              {item.context_source === "serp" ? "SERP context" : "Operator-imported"}
              {item.context_chars != null ? ` · ${item.context_chars} chars` : ""}
              {item.discovery_query
                ? ` · discovered for "${item.discovery_query}"`
                : ""}
              {item.link_count != null && item.link_count > 0
                ? ` · ${item.link_count} link${item.link_count === 1 ? "" : "s"}`
                : " · 0 links"}
            </p>
          </div>
          <div className="flex flex-col items-end gap-1 text-right">
            <span className="font-mono text-[10px] text-base-content/40">
              v{item.version} · {item.status}
            </span>
            {item.operator_choice ? (
              <Chip className="tag-chip-slate">
                staged: {item.operator_choice}
              </Chip>
            ) : null}
          </div>
        </header>

        {refusedGates.length > 0 ? (
          <div className="rounded-xl border border-warning/30 bg-warning/5 px-3 py-2 text-xs text-warning">
            <span className="inline-flex items-center gap-1.5 font-medium">
              <ShieldAlert className="size-3.5" />
              {refusedGates.length} gate{refusedGates.length === 1 ? "" : "s"} refused
              on generation — {item.post_type} copy only
            </span>
            <span className="ml-1 text-base-content/50">
              {refusedGates.map((g) => g.gate).join(", ")}
            </span>
          </div>
        ) : null}

        {/* Advice segment */}
        <section className="rounded-xl border border-base-300 bg-base-100 p-3">
          <div className="mb-2 flex items-center justify-between gap-2">
            <span className="text-[11px] font-semibold uppercase tracking-wider text-base-content/50">
              {isArticle
                ? "Article body (long-form)"
                : `Advice ${hasMention ? "(paste first)" : "(your reply)"}`}
            </span>
            <button
              type="button"
              className="btn btn-primary btn-sm"
              disabled={busy}
              onClick={copyAdvice}
            >
              <Copy className="size-3.5" />
              {isArticle ? "Copy Body Only" : "Copy Advice Only"}
            </button>
          </div>
          <p className="whitespace-pre-wrap text-sm leading-relaxed text-base-content/85">
            {adviceText}
          </p>
          {charLimit != null || (isArticle && item.min_words != null) ? (
            <p className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-base-200 pt-2 text-[11px] text-base-content/50">
              {charLimit != null ? (
                <span
                  className={
                    adviceChars > charLimit
                      ? "inline-flex items-center gap-1 font-medium text-warning"
                      : undefined
                  }
                >
                  {adviceChars}/{charLimit} chars
                  {adviceChars > charLimit ? " — over the channel ceiling" : ""}
                </span>
              ) : null}
              {isArticle && item.min_words != null ? (
                <span>min {item.min_words} words</span>
              ) : null}
            </p>
          ) : null}
        </section>

        {/* Mention segment (full response path) */}
        {hasMention ? (
          <section
            className={`rounded-xl border p-3 ${
              canCopyFull
                ? "border-base-300 bg-base-100"
                : "border-dashed border-warning/40 bg-warning/5"
            }`}
          >
            <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
              <span className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wider text-base-content/50">
                <Clipboard className="size-3.5" />
                {isArticle ? "Contextual backlink" : "Brand mention"}
              </span>
              <button
                type="button"
                className={`btn btn-sm ${canCopyFull ? "btn-primary" : "btn-ghost"}`}
                disabled={busy || !canCopyFull}
                onClick={copyFull}
                title={
                  mentionGated
                    ? "Mention only appears when the draft is promotional and generation gates passed"
                    : capBlocked
                      ? "Promotional cap reached for this account or community"
                      : undefined
                }
              >
                <Copy className="size-3.5" />
                {isArticle
                  ? "Copy Full Article (Body + Backlink)"
                  : "Copy Full Response (Advice + Mention)"}
              </button>
            </div>
            <p className="mb-2 whitespace-pre-wrap border-l-2 border-primary/30 pl-3 text-sm leading-relaxed text-base-content/85">
              {item.mention_md}
            </p>
            <p className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-base-content/50">
              {caps ? (
                <>
                  <span>
                    account {caps.account_handle} ·{" "}
                    {caps.account_promotional_30d}/{caps.account_promotional_cap_30d}
                    promotional
                  </span>
                  <span>
                    community · {caps.community_promotional_30d}/
                    {caps.community_promotional_cap_30d} promotional
                  </span>
                </>
              ) : (
                <span>no eligible account resolved for this community</span>
              )}
              {capBlocked ? (
                <span className="inline-flex items-center gap-1 text-warning">
                  <AlertTriangle className="size-3" />
                  cap reached — copy disabled
                </span>
              ) : null}
            </p>
          </section>
        ) : null}

        {/* Actions */}
        <div className="flex flex-wrap items-center justify-between gap-2 border-t border-base-200 pt-3">
          <button
            type="button"
            className="btn btn-ghost btn-sm text-error"
            disabled={busy}
            onClick={() => setSkipOpen(true)}
          >
            <SkipForward className="size-3.5" />
            Skip / Ineligible
          </button>
          {item.link_count != null && item.link_count > 0 && !hasMention ? (
            <span className="text-[11px] text-base-content/40">
              Draft contains a link — paste with care
            </span>
          ) : null}
        </div>

        {/* Permalink submission + verification loop */}
        <section className="rounded-xl border border-base-300 bg-base-100 p-3">
          <span className="mb-2 flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wider text-base-content/50">
            <Link2 className="size-3.5" />
            Permalink submission &amp; verification
          </span>

          {published ? (
            <div className="space-y-1.5 text-sm">
              <p className="inline-flex items-center gap-1.5 text-success">
                <Check className="size-4" />
                {published.live_permalink} — {published.status}
              </p>
              <p className="text-xs text-base-content/50">
                {published.post_type} publication · verification schedule:{" "}
                {checkpointScheduleLabel(published.checkpoints)}
              </p>
            </div>
          ) : (
            <>
              <p className="mb-2 text-xs text-base-content/50">
                After you paste the reply on the platform, submit the public
                permalink. The reply is then checked at{" "}
                <span className="font-medium text-base-content/70">
                  T+15m · T+2h · T+24h · T+72h
                </span>{" "}
                and a removal trips the community kill-switch.
              </p>
              <div className="flex flex-col gap-2 sm:flex-row">
                <input
                  className="input input-bordered input-sm flex-1 font-mono text-xs"
                  value={permalink}
                  onChange={(e) => setPermalink(e.target.value)}
                  placeholder={permalinkPlaceholder(item.platform)}
                  disabled={busy}
                />
                <input
                  className="input input-bordered input-sm w-full sm:w-32"
                  value={operator}
                  onChange={(e) => setOperator(e.target.value)}
                  placeholder="Operator"
                  disabled={busy}
                  aria-label="Operator"
                />
                <button
                  type="button"
                  className="btn btn-primary btn-sm"
                  disabled={busy}
                  onClick={submitPermalink}
                >
                  <Send className="size-3.5" />
                  Submit permalink
                </button>
              </div>
            </>
          )}
        </section>

        {/* Gate transcript (collapsible) */}
        <details className="group">
          <summary className="cursor-pointer list-none text-[11px] font-medium text-base-content/50">
            Gate transcript ({item.gates.length}) —{" "}
            <span className="text-success">passes</span> /{" "}
            <span className="text-warning">refusals</span> /{" "}
            <span className="text-base-content/40">skips</span>
          </summary>
          <ul className="mt-2 grid gap-1 text-xs text-base-content/70 sm:grid-cols-2">
            {item.gates.map((g, i) => (
              <li key={`${g.gate}-${i}`} className="flex items-start gap-1.5">
                <span
                  className={`mt-1 size-1.5 shrink-0 rounded-full ${
                    g.outcome === "pass"
                      ? "bg-success"
                      : g.outcome === "refuse"
                        ? "bg-warning"
                        : "bg-base-content/30"
                  }`}
                />
                <span>
                  <span className="font-medium">{g.gate}</span>
                  {g.reason ? (
                    <span className="text-base-content/50"> — {g.reason}</span>
                  ) : null}
                </span>
              </li>
            ))}
          </ul>
        </details>
      </div>

      {skipOpen ? (
        <Modal onClose={() => setSkipOpen(false)} labelledBy="skip-draft-title">
          <div className="space-y-3">
            <h2 id="skip-draft-title" className="text-base font-semibold">
              Skip this draft?
            </h2>
            <p className="text-sm text-base-content/60">
              The recommendation is rejected and the reason is recorded for the
              discovery agent's learning loop ({item.community_key}).
            </p>
            <textarea
              className="textarea textarea-bordered w-full text-sm"
              rows={3}
              value={skipReason}
              onChange={(e) => setSkipReason(e.target.value)}
              placeholder="Why is this ineligible? (inappropriate thread, duplicate, rules changed…)"
              autoFocus
            />
            <div className="flex justify-end gap-2">
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                onClick={() => setSkipOpen(false)}
              >
                Cancel
              </button>
              <button
                type="button"
                className="btn btn-error btn-sm"
                disabled={busy}
                onClick={confirmSkip}
              >
                Skip &amp; reject recommendation
              </button>
            </div>
          </div>
        </Modal>
      ) : null}
    </article>
  );
}
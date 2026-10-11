-- ============================================================
-- 35 - Off-page multi-platform expansion (X / Medium long-form /
--      Shopify Community)
-- ============================================================
-- DRAFT FOR REVIEW - NOT YET APPLIED.
--
-- Extends the plan/33 off-page pipeline (built for Reddit / Quora /
-- Medium community REPlies) to three new or deepened channels:
--
--   * X (Twitter)             - NEW platform. Reply-to-thread discovery
--                               via the existing DataForSEO SERP contract;
--                               hard character ceilings; aged-account +
--                               standing gates; no automated posting.
--   * Medium long-form        - EXISTING platform, new content kind. The
--                               pipeline currently only drafts community
--                               replies. This adds post_kind='article':
--                               long-form authority pieces grounded on the
--                               tenant's own pages.body_html corpus with a
--                               contextual backlink only from
--                               grounding_page_ids. Same operator
--                               human-gate; publishing stays manual.
--   * Shopify Community       - NEW platform. Discovery through a NEW,
--                               disabled-by-default, rate-limited web-
--                               search seam (decision, plan/35 §decisions)
--                               plus target_keywords; fit gate refuses
--                               promotional mention when the client's
--                               Shopify app does not genuinely answer the
--                               merchant question.
--
-- SAFETY PRINCIPLES CARRIED FORWARD UNSHADOWED (plan/33):
--   * Tenant isolation is the invariant. Every new FK is COMPOSITE
--     (site_id, <parent_pk>); site_id is NOT NULL on every new table;
--     there is no NULL escape (plan/33 §header).
--   * HUMAN-IN-THE-LOOP: publishing remains strictly manual. Nothing in
--     this migration or in the corresponding specs pushes a button on any
--     platform. Discovery + drafting are automated; posting is pasted by
--     a human, permalink is submitted, verification is the existing sweep.
--   * STRICT GROUNDING: long-form and replies draw facts only from the
--     tenant's own corpus (fixes/offpage_grounding.py); grounding_corpus_
--     hash / grounding_page_ids freeze provenance at draft time.
--   * ZERO API-IDENTITY FINGERPRINTING: no platform write API is ever
--     called. X discovery is a DataForSEO SERP read (our existing never-
--     raise contract) - never a scrape of an authenticated surface. The
--     new Shopify Community web-search seam is rate-limited, cost-ledgered
--     and ships DISABLED (fail-closed), mirroring fix_policy.
--
-- SHAPE: this migration widens platform enumerations, adds a content-kind
-- dimension (reply vs article), adds per-tenant platform control rows and
-- the discovery-seam ledger/quota, and records the discovery query on
-- threads. It creates no fix_policy rows and no generated_fixes rows -
-- off-page stays outside fix_executor's claim path (plan/33 §header).
--
-- Idempotent: every object is IF NOT EXISTS, every widened CHECK is a
-- guarded drop/add, every seed is ON CONFLICT DO NOTHING.

-- ------------------------------------------------------------
-- §1. platform enumeration belongs in TWO places
-- ------------------------------------------------------------
-- offpage_communities.platform and offpage_accounts.platform carry the
-- plan/33 CHECK ('reddit','quora','medium'). Widening must cover both or
-- an account required for 'x'/'shopify_community' work could never be
-- created. The default-generated constraint names are
-- offpage_communities_platform_check / offpage_accounts_platform_check.

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_constraint
               WHERE conname = 'offpage_communities_platform_check') THEN
        ALTER TABLE offpage_communities DROP CONSTRAINT
            offpage_communities_platform_check;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'offpage_communities_platform_check') THEN
        ALTER TABLE offpage_communities
            ADD CONSTRAINT offpage_communities_platform_check
            CHECK (platform IN ('reddit','quora','medium',
                                'x','shopify_community'));
    END IF;
END $$;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_constraint
               WHERE conname = 'offpage_accounts_platform_check') THEN
        ALTER TABLE offpage_accounts DROP CONSTRAINT
            offpage_accounts_platform_check;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'offpage_accounts_platform_check') THEN
        ALTER TABLE offpage_accounts
            ADD CONSTRAINT offpage_accounts_platform_check
            CHECK (platform IN ('reddit','quora','medium',
                                'x','shopify_community'));
    END IF;
END $$;

-- ------------------------------------------------------------
-- §2. offpage_threads: a content-kind dimension (reply vs article)
-- ------------------------------------------------------------
-- One pipeline, two artifacts. `post_kind` splits "reply to a discovered
-- thread" from "long-form article on a topic". The latter is what Medium
-- long-form distribution needs, and keeping it in offpage_threads reuses
-- the entire draft / operator-queue / publication / verification
-- machinery instead of duplicating seven tables.
--
-- For post_kind='article' the thread row is the TOPIC: thread_url is the
-- discovery reference (the Medium tag or topic search URL), title/snippet
-- are the topic framing, community_id is the target publication/tag, and
-- the "thread" eventually resolves to the article permalink at submit
-- time. The queue card renders comment-style for 'reply' and an editor
-- preview for 'article'.
--
-- `discovery_query` records WHICH tenant keyword surfaced the row, so
-- "why did this opportunity enter the queue" is answerable from the row,
-- not from an unexplained URL.
ALTER TABLE offpage_threads
    ADD COLUMN IF NOT EXISTS post_kind TEXT NOT NULL DEFAULT 'reply';

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_constraint
               WHERE conname = 'offpage_threads_post_kind_check') THEN
        ALTER TABLE offpage_threads DROP CONSTRAINT
            offpage_threads_post_kind_check;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'offpage_threads_post_kind_check') THEN
        ALTER TABLE offpage_threads
            ADD CONSTRAINT offpage_threads_post_kind_check
            CHECK (post_kind IN ('reply','article'));
    END IF;
END $$;

ALTER TABLE offpage_threads
    ADD COLUMN IF NOT EXISTS discovery_query TEXT;

-- plan/33 §2 deliberately limited context_source to ('serp',
-- 'operator_import'): v1 was SERP-ONLY by decision. plan/35 adds the rate-
-- limited web-search seam (User decision, plan/35 §decisions), so a
-- thread whose context came from that seam must be distinguishable from a
-- SERP read for the drafter's context-sufficiency gate and for audit.
-- NULL-safety note: the widened enum is backward compatible - existing
-- rows keep their values and NOT NULL is preserved by ALTER (column stays
-- NOT NULL DEFAULT 'serp').
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_constraint
               WHERE conname = 'offpage_threads_context_source_check') THEN
        ALTER TABLE offpage_threads DROP CONSTRAINT
            offpage_threads_context_source_check;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'offpage_threads_context_source_check') THEN
        ALTER TABLE offpage_threads
            ADD CONSTRAINT offpage_threads_context_source_check
            CHECK (context_source IN ('serp','web_search','operator_import'));
    END IF;
END $$;

-- Long-form reads the same proven corpus loader, so Article drafts keep
-- the FROZEN CORPUS PROVENANCE contract (grounding_corpus_hash +
-- grounding_page_ids) with no schema change.

-- ------------------------------------------------------------
-- §3. offpage_platform_settings - per-tenant platform control + gates
-- ------------------------------------------------------------
-- The home of platform-specific gate adjustment, keyed (site_id, platform)
-- just like fix_policy's (site_id, sub_type): every brake is per-tenant,
-- because what one client's X account can safely do costs another's be
-- banned. Mirrors the plan/22-27 fix_policy seeding pattern:
--
--   enabled = FALSE is the DEFAULT. No tenant gets a new platform until an
--   operator flips it on. The same fail-closed posture as fix_policy and
--   offpage_communities.enabled.
--
--   discovery_seams: which discovery sources this tenant opted into per
--   platform. 'dataforseo' is the existing contract; 'web_search' is the
--   new guarded seam (§5); 'operator_import' needs no enable flag (a human
--   is already the seam). Empty = no automated discovery for this channel.
--
--   char ceilings: X is character-constrained in a way Reddit/Quora are
--   not. reply_char_limit / post_char_limit are CONSERVATIVE ceilings
--   (plan/33 deliberately did NOT derive caps from platform limits; the
--   same rule applies - we brake on the safe side of what is published).
--   NULL means "no ceiling" (Medium/Shopify Community).
--
--   promotional_posts_per_30d is a PLATFORM-level ceiling in addition to
--   the account-level and community-level caps already enforced at
--   choose/permalink time (src/api/routes/offpage.py). Three independent
--   brakes so a busy tenant cannot saturate one channel.
CREATE TABLE IF NOT EXISTS offpage_platform_settings (
    site_id        UUID NOT NULL REFERENCES site_config(site_id),
    platform       TEXT NOT NULL
                   CHECK (platform IN ('reddit','quora','medium',
                                       'x','shopify_community')),

    -- Kill-switch for the whole channel. FALSE blocks discovery seams AND
    -- the draft path for draft post_kind/community on this platform per
    -- tenant. Mirrors offpage_communities.enabled semantics.
    enabled        BOOLEAN NOT NULL DEFAULT false,

    discovery_seams TEXT[] NOT NULL DEFAULT '{}',
    discovery_calls_per_day INT NOT NULL DEFAULT 20,

    reply_char_limit   INT,            -- X: 280 (conservative, see §3)
    post_char_limit    INT,            -- X: 280; Medium: NULL (long-form)
    min_words          INT,            -- Medium long-form floor (e.g. 800)
    promotional_posts_per_30d INT,

    -- Targeting knobs for discovery seams, kept as JSONB so the seam
    -- code can evolve without another migration. Mirrors the
    -- validation/grounding JSONB pattern already on offpage_drafts.
    --   { "x":        {"accounts": [...], "hashtags": [...] },
    --     "shopify":   {"app_name": "...", "app_category": "...",
    --                    "fit_keywords": [...] },
    --     "medium":    {"publications": [...] } }
    targeting      JSONB NOT NULL DEFAULT '{}',

    notes          TEXT,
    created_at     TIMESTAMPTZ DEFAULT now(),
    updated_at     TIMESTAMPTZ DEFAULT now(),

    PRIMARY KEY (site_id, platform)
);

-- The discovery job's hot path: which tenants/platforms may run today.
CREATE INDEX IF NOT EXISTS idx_offpage_plat_settings_enabled
    ON offpage_platform_settings (site_id, platform)
    WHERE enabled AND discovery_seams <> '{}';

-- ------------------------------------------------------------
-- §4. offpage_search_log - discovery seam audit ledger
-- ------------------------------------------------------------
-- plan/33 used DataForSEO through connectors.costlog but recorded NO
-- per-search audit row: budget_config tracked the money, not the query.
-- The new web-search seam (and, going forward, every automated discovery
-- call) needs "WHAT did we search, WHO for, WHO answered, HOW FAST" to be
-- answerable, and it is the zero-fingerprint audit: nothing here ever
-- records a login, a cookie, or a platform identity - it records calls to
-- public search surfaces under the tenant's own rate ceiling.
CREATE TABLE IF NOT EXISTS offpage_search_log (
    search_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    site_id      UUID NOT NULL REFERENCES site_config(site_id),
    platform     TEXT NOT NULL,
    seam         TEXT NOT NULL
                 CHECK (seam IN ('dataforseo','web_search','operator_import')),
    query        TEXT NOT NULL,
    result_count INT,
    provider     TEXT,                  -- 'dataforseo_serp' | 'web_search_...'
    http_status  INT,
    latency_ms   INT,
    created_at   TIMESTAMPTZ DEFAULT now()
);

-- "How is this tenant's discovery spend pacing?" - the dashboard join.
CREATE INDEX IF NOT EXISTS idx_offpage_search_log_site
    ON offpage_search_log (site_id, platform, created_at DESC);

-- NOTE: no predicate here. `now()` is STABLE, not IMMUTABLE, and Postgres
-- rejects it in an index predicate; the scan cost of an unbounded recency
-- index is trivial at this table's write volume.
CREATE INDEX IF NOT EXISTS idx_offpage_search_log_recency
    ON offpage_search_log (created_at DESC);

-- Read-only spend view for the operator (mirrors v_offpage_account_mix:
-- diagnostic, never a gate).
CREATE OR REPLACE VIEW v_offpage_search_spend AS
SELECT site_id,
       platform,
       seam,
       count(*) AS calls_7d,
       sum(result_count) AS results_7d
FROM offpage_search_log
WHERE created_at > now() - INTERVAL '7 days'
GROUP BY site_id, platform, seam;

-- ------------------------------------------------------------
-- §5. offpage_search_quota - atomic rate-limit state for seams
-- ------------------------------------------------------------
-- The web-search seam's brake. One row per (site_id, platform, window);
-- the dispatcher claims a call with an atomic upsert so concurrent
-- discovery legs cannot exceed the ceiling by racing:
--
--   INSERT ... VALUES (..., date_trunc('minute', now()), 3600, 1, max)
--   ON CONFLICT (site_id, platform, window_start)
--   DO UPDATE SET calls_made = offpage_search_quota.calls_made + 1
--   WHERE offpage_search_quota.calls_made < offpage_search_quota.max_calls
--   RETURNING calls_made;
--
-- The upsert serialises on the row lock under READ COMMITTED, so the
-- WHERE re-check runs against the committed value and the ceiling holds
-- across concurrent callers. When now() crosses the minute boundary a NEW
-- window_start becomes a fresh row and the counter resets naturally.
-- window_seconds IS the window shape (3600 = hourly); window_start is a
-- truncated anchor, not "when the first call happened".
--
-- Layering with costlog: the DataForSEO leg already passes
-- connectors.costlog.check_budget(service='openseo_serp') BEFORE the call
-- (plan/23 non-negotiable #5). The web-search seam enforces the quota
-- table FIRST and then, if the seam is paid, the SAME budget gate -
-- budget and rate are two different brakes and both must trip
-- independently.
CREATE TABLE IF NOT EXISTS offpage_search_quota (
    site_id        UUID NOT NULL,
    platform       TEXT NOT NULL,
    window_start   TIMESTAMPTZ NOT NULL,
    window_seconds INT  NOT NULL,
    calls_made     INT  NOT NULL DEFAULT 1,
    max_calls      INT  NOT NULL,

    PRIMARY KEY (site_id, platform, window_start)
);

-- Old windows are read-only history; prune daily with the search log:
--   DELETE FROM offpage_search_quota
--   WHERE window_start < now() - INTERVAL '7 days';
-- (documented here; no trigger needed - this is bookkeeping, not safety.)

-- Quota lookups by window: the discovery job's first check.
CREATE INDEX IF NOT EXISTS idx_offpage_search_quota_site
    ON offpage_search_quota (site_id, platform, window_start);

-- ------------------------------------------------------------
-- §6. seeds
-- ------------------------------------------------------------
-- No platform rows are pre-created: like plan/33, discovery and account
-- seeds are deployment/operator-specific and adding disabled-by-default
-- rows here would just tell lies about what a tenant opted into. The
-- enabled=false default on offpage_platform_settings is the fail-closed
-- posture; nothing in §1-§5 changes an existing tenant's behaviour.
-- ============================================================
-- 33 - Off-page community SEO (Reddit / Quora / Medium)
-- ============================================================
-- Off-page DISCOVERY and DRAFTING are automated; PUBLISHING is strictly
-- manual and structurally outside the automated executor.
--
-- This migration creates eight off-page tables plus one column on
-- `recommendations`. It creates NO fix_policy rows and NO
-- generated_fixes rows, on purpose: off-page must never be claimable by
-- fix_executor. `_claim_queued` (jobs/fix_executor.py) selects
-- status='queued' from generated_fixes, and the only writer of 'queued'
-- in the whole tree is the human gate-2 endpoint (api/routes/fixes.py).
-- Off-page drafts live in `offpage_drafts` instead, so there is no row
-- for the executor to claim even in the presence of a bug.
--
-- TENANT SCOPE IS THE INVARIANT. Every table below carries
-- site_id NOT NULL REFERENCES site_config(site_id), and every unique
-- index leads with site_id.
--
-- A single-column site_id FK is NOT sufficient and this migration does
-- not rely on one. Independent FKs do not tie a child to its parent's
-- tenant: with plain constraints, a draft could carry tenant A's
-- site_id while referencing tenant B's recommendation, and the corpus
-- loader would then read tenant A's catalogue against tenant B's
-- recommendation - precisely the cross-brand hallucination this feature
-- must make unrepresentable. So every parent-facing FK below is a
-- COMPOSITE (site_id, <parent_pk>) FK against a unique index on the
-- parent. Because site_id is NOT NULL on every off-page table, MATCH
-- SIMPLE semantics always apply the constraint; there is no NULL escape.
--
-- recommendations.site_id is nullable (00-schema.sql:139), so a
-- tenant-less recommendation deliberately cannot be referenced by an
-- off-page draft. That refusal is correct: off-page work with no tenant
-- has no brand to ground against.
--
-- This feature promotes the CLIENT's brand, never the platform's:
-- discovery seeds come from site_config.target_keywords, and draft
-- grounding comes from that tenant's own pages.body_html (plan/22:123 -
-- the only durable per-tenant product copy in the schema).
--
-- Idempotent: every object is IF NOT EXISTS and every seed is
-- ON CONFLICT DO NOTHING, so re-running is a no-op.

-- Composite-FK targets must exist before the child tables that use them.
CREATE UNIQUE INDEX IF NOT EXISTS uq_recommendations_site_rec
    ON recommendations (site_id, recommendation_id);

-- ------------------------------------------------------------
-- 1. offpage_communities - external community + its rules
-- ------------------------------------------------------------
-- Keyed (site_id, platform, community_key) rather than globally,
-- mirroring fix_policy's (site_id, sub_type) primary key. Community
-- PERMISSION is genuinely per-tenant: one client's account can be
-- banned from r/ecommerce while another's is fine, and the strike
-- counter that disables targeting must not leak across tenants.
--
-- allows_self_promo is nullable on purpose. NULL means "we did not
-- read the rules", never "self-promo is disallowed". This mirrors the
-- structured_data.py lesson (src/audit/): a default that reads as an
-- observation manufactures false facts. The draft gate treats NULL as
-- REFUSE, because promoting into an unread-ruleset community is the
-- failure mode that gets accounts banned.
CREATE TABLE IF NOT EXISTS offpage_communities (
    community_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    site_id           UUID NOT NULL REFERENCES site_config(site_id),

    platform          TEXT NOT NULL
                      CHECK (platform IN ('reddit','quora','medium')),
    community_key     TEXT NOT NULL,          -- 'r/ecommerce', 'quora.com/Topic'

    rules_url         TEXT,                   -- operator- or import-supplied
    rules_text        TEXT,
    rules_hash        TEXT,                   -- md5(rules_text); NULL = rules never read
    rules_fetched_at  TIMESTAMPTZ,

    -- 'unknown' is a real state, not a placeholder: it means the rules
    -- were never resolved and the draft gate must refuse promotional work.
    link_policy       TEXT NOT NULL DEFAULT 'unknown'
                      CHECK (link_policy IN ('allowed','nofollow','ask_first',
                                             'forbidden','unknown')),
    allows_self_promo BOOLEAN,                -- NULL = not asserted, NOT "no"

    -- Some communities prohibit substantial AI-generated content outright;
    -- others permit it. This is a per-community fact we must READ, never
    -- assume. NULL = rules not read = refuse to generate a draft here at
    -- all, not merely refuse promotion. Reddit's 2026 enforcement posture
    -- targets "AI-enabled inauthentic activity" specifically, so guessing
    -- TRUE is the expensive direction to be wrong in.
    ai_content_allowed BOOLEAN,

    -- Self-imposed brake on promotional posts per account in this
    -- community, rolling 30 days. This is a CEILING, not a target: it
    -- cannot be optimised against, because producing more helpful content
    -- never raises it. Deliberately NOT derived from any published limit.
    promotional_cap_30d INT NOT NULL DEFAULT 2,

    daily_karma_cap   INT,

    -- Kill-switch. enabled=false blocks discovery from minting new
    -- opportunities here AND blocks the draft gate from producing a
    -- promotional draft. Set by the 3-strike rule in offpage_verify.
    enabled           BOOLEAN NOT NULL DEFAULT false,
    strike_count      INT NOT NULL DEFAULT 0,
    disabled_reason   TEXT,

    data_source       TEXT CHECK (data_source IN ('live','mock','import')),
    created_at        TIMESTAMPTZ DEFAULT now(),
    updated_at        TIMESTAMPTZ DEFAULT now(),

    UNIQUE (site_id, platform, community_key)
);

-- Composite-FK target for offpage_threads.offpage_community_id and for
-- offpage_publications.community_id.
CREATE UNIQUE INDEX IF NOT EXISTS uq_offpage_comm_tenant
    ON offpage_communities (site_id, community_id);

CREATE INDEX IF NOT EXISTS idx_offpage_comm_enabled
    ON offpage_communities (site_id, enabled);

-- ------------------------------------------------------------
-- 2. offpage_threads - discovered external threads
-- ------------------------------------------------------------
-- `snippet` is the DataForSEO SERP description field and is the ONLY
-- thread text this system ingests (v1 is SERP-ONLY by decision; see
-- context_source below). We never fetch a reddit.com URL: discovery is a
-- DataForSEO SERP read against our own existing contract, which keeps the
-- entire discovery path behind the never-raise adapter, the cost ledger,
-- and the weekly budget gate with no new external surface.
--
-- THE CONSEQUENCE, accepted deliberately: a SERP description is often a
-- truncated title rather than the question the Reddit user asked. The
-- drafter therefore runs a CONTEXT SUFFICIENCY GATE and drops the
-- opportunity when the snippet cannot support a genuinely useful
-- peer-level answer. Fewer, high-confidence drafts is the correct
-- trade: a wrong or generic reply to a real question is worse for the
-- brand than no reply, and it is the behaviour platform defences cluster
-- on. Operator-supplied thread text (context_source='operator_import')
-- is the documented escape hatch for a thin snippet.
CREATE TABLE IF NOT EXISTS offpage_threads (
    thread_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    site_id           UUID NOT NULL REFERENCES site_config(site_id),
    community_id      UUID NOT NULL,

    -- Composite tenant FK: a thread's community must belong to the SAME
    -- tenant as the thread itself. A single-column community_id FK would
    -- happily let tenant A hold a thread pointing at tenant B's community,
    -- which would hand the drafter the wrong brand's rules.
    FOREIGN KEY (site_id, community_id)
        REFERENCES offpage_communities (site_id, community_id),

    thread_url        TEXT NOT NULL,
    external_id       TEXT,
    title             TEXT,
    snippet           TEXT,                   -- SERP description; the drafting corpus
    thread_score      INT,
    comment_count     INT,

    -- WHERE THE CONTEXT CAME FROM, and how much of it there was.
    --
    -- v1 is SERP-ONLY BY DECISION, not by omission. We never fetch a
    -- reddit.com URL: a fetcher would drag in rate limits, robots.txt
    -- parsing and anti-scraping defences, none of which are worth the
    -- engineering or the legal surface in exchange for a marginally
    -- better draft. The cost is real and accepted - see the context
    -- sufficiency gate below.
    --
    -- 'serp' is the only value v1 may write. 'operator_import' exists for
    -- a human pasting thread text they read themselves, which is the
    -- escape hatch when the SERP snippet is too thin to draft from.
    context_source    TEXT NOT NULL DEFAULT 'serp'
                      CHECK (context_source IN ('serp','operator_import')),
    context_chars     INT,                   -- measured usable context length
    context_usable    BOOLEAN,               -- NULL = not yet assessed
    context_assessed_at TIMESTAMPTZ,

    -- Rules AS OF DISCOVERY. rules_hash lets the UI warn "the rules for
    -- this community changed since this draft was written - re-read
    -- before posting", which is cheap and catches the most common
    -- cause of a silent automod removal.
    rules_snapshot    JSONB,
    rules_hash        TEXT,

    -- THREAD-LEVEL promotion permission, which is a genuinely separate
    -- fact from the community-level one. A subreddit may permit
    -- self-promotion only in designated weekly threads, or require
    -- moderator permission for this particular question. Community
    -- permission therefore CANNOT answer "may this thread take a
    -- mention?", and conflating the two produces exactly the silent
    -- automod removal this system exists to prevent.
    --
    -- Same nullable-on-purpose contract as allows_self_promo: NULL means
    -- nobody determined whether this thread permits promotion. The draft
    -- gate treats NULL as REFUSE, not as "no objection".
    promotion_allowed BOOLEAN,

    first_seen_at     TIMESTAMPTZ DEFAULT now(),
    last_seen_at      TIMESTAMPTZ DEFAULT now(),
    data_source       TEXT CHECK (data_source IN ('live','mock','import')),

    UNIQUE (site_id, thread_url)
);

-- Composite-FK target for offpage_publications.thread_id and
-- recommendations.offpage_thread_id (with recommendations.site_id).
CREATE UNIQUE INDEX IF NOT EXISTS uq_offpage_threads_tenant
    ON offpage_threads (site_id, thread_id);

CREATE INDEX IF NOT EXISTS idx_offpage_threads_comm
    ON offpage_threads (site_id, community_id, last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_offpage_threads_recency
    ON offpage_threads (site_id, last_seen_at DESC);

-- ------------------------------------------------------------
-- 3. offpage_accounts - our own aged accounts on each platform
-- ------------------------------------------------------------
-- Account age, karma, and standing are OPERATOR-ASSERTED unless
-- last_verified_at is set. v1 uses no platform auth for reads, so the
-- verified fields stay NULL and the UI must render them as asserted.
-- last_verified_at IS NULL is the structural signal that we have not
-- confirmed these numbers - same principle as allows_self_promo.
CREATE TABLE IF NOT EXISTS offpage_accounts (
    account_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    site_id           UUID NOT NULL REFERENCES site_config(site_id),

    platform          TEXT NOT NULL
                      CHECK (platform IN ('reddit','quora','medium')),
    handle            TEXT NOT NULL,

    account_created_on DATE,
    karma             INT,
    status            TEXT NOT NULL DEFAULT 'aging'
                      CHECK (status IN ('aging','active','restricted',
                                        'banned','retired')),
    restricted_at     TIMESTAMPTZ,
    disabled_reason   TEXT,

    -- Self-imposed CEILING on promotional posts per account, rolling 30
    -- days. Replaces the earlier trailing-90-day helpful:promotional RATIO
    -- floor, and the distinction is load-bearing:
    --
    -- A floor is a target. "Keep helpful/promotional >= 0.80" invites
    -- tuning the denominator, and the only way to satisfy it while
    -- wanting more promotional output is to manufacture filler "helpful"
    -- posts - which is itself spam, and a more expensive kind than the
    -- promotion it was avoiding. It also encodes an anti-spam constraint
    -- as a number to optimise against, which inverts the intent.
    --
    -- A ceiling has no such failure mode. Producing more helpful content
    -- never raises it, so it cannot be gamed by volume in either
    -- direction. Over-shooting it is simply refused.
    --
    -- Deliberately NOT derived from any published platform limit. This is
    -- a conservative brake we impose on ourselves, not an estimate of
    -- what would get an account banned.
    promotional_cap_30d INT NOT NULL DEFAULT 3,

    -- The retired ratio is not resurrected as a gate. It survives as a
    -- READ-ONLY diagnostic view (v_offpage_account_mix, at the end of
    -- this file) because "is this account's output drifting promotional?"
    -- is a genuinely useful question - it just must not be a threshold
    -- that rewards manufacturing filler to satisfy.

    last_verified_at  TIMESTAMPTZ,            -- NULL = operator-asserted
    notes             TEXT,
    created_at        TIMESTAMPTZ DEFAULT now(),
    updated_at        TIMESTAMPTZ DEFAULT now(),

    UNIQUE (site_id, platform, handle)
);

-- Composite-FK target for offpage_publications.account_id.
CREATE UNIQUE INDEX IF NOT EXISTS uq_offpage_accounts_tenant
    ON offpage_accounts (site_id, account_id);

CREATE INDEX IF NOT EXISTS idx_offpage_accounts_status
    ON offpage_accounts (site_id, status);

-- Denormalized brand identity, read by the drafter so the prompt never
-- has to re-derive "what is this client's name". Sourced from
-- site_config at insert time; NULL means "not configured", and the
-- drafter refuses promotional work when brand_name is NULL rather than
-- inventing one.
ALTER TABLE offpage_accounts
    ADD COLUMN IF NOT EXISTS brand_name TEXT;

-- ------------------------------------------------------------
-- 3b. offpage_account_communities - which account may post where
-- ------------------------------------------------------------
-- "Is this account genuinely appropriate here?" is a THIRD gate, distinct
-- from "what does this community allow" (offpage_communities) and
-- "is this thread appropriate" (offpage_threads). An account aged in
-- r/homeimprovement has no standing in r/webdev regardless of what
-- either community's rules say, and posting there is the behaviour
-- platform-level defences are explicitly built to cluster.
--
-- `standing` is OPERATOR-ASSERTED from real browsing, never inferred.
-- 'none' is distinct from NULL: NULL means not yet assessed (refuse),
-- 'none' means assessed and concluded unsuitable (refuse, with a reason
-- an operator can read).
CREATE TABLE IF NOT EXISTS offpage_account_communities (
    site_id           UUID NOT NULL,
    account_id        UUID NOT NULL,
    community_id      UUID NOT NULL,

    -- Both parents composite-FK'd, so an account-community pair cannot
    -- mix tenants even by accident during operator data entry.
    FOREIGN KEY (site_id, account_id)
        REFERENCES offpage_accounts (site_id, account_id),
    FOREIGN KEY (site_id, community_id)
        REFERENCES offpage_communities (site_id, community_id),

    standing          TEXT CHECK (standing IN ('established','participating',
                                              'occasional','none')),
    assessed_at       TIMESTAMPTZ,            -- NULL = never assessed
    notes             TEXT,
    created_at        TIMESTAMPTZ DEFAULT now(),

    -- One row per pair; re-assessing updates rather than appends.
    UNIQUE (account_id, community_id)
);

-- "May I post from this account in this community?" is checked on every
-- draft, so the lookup is on the pair, ordered by recency of assessment.
CREATE INDEX IF NOT EXISTS idx_offpage_acct_comm
    ON offpage_account_communities (site_id, community_id, account_id)
    WHERE standing IS NOT NULL;

-- ------------------------------------------------------------
-- 4. offpage_drafts - AI drafts (NOT generated_fixes)
-- ------------------------------------------------------------
-- Deliberately a separate table from generated_fixes. A fix row is a
-- write instruction for a platform adapter; an off-page draft is text
-- a human pastes by hand. Conflating them would put a copy-to-clipboard
-- artifact inside the executor's claim path.
--
-- `version` + the partial unique index below give one live draft per
-- recommendation: a revision supersedes the prior version rather than
-- racing it.
CREATE TABLE IF NOT EXISTS offpage_drafts (
    draft_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    site_id           UUID NOT NULL REFERENCES site_config(site_id),
    recommendation_id UUID NOT NULL,

    -- Composite tenant FK. This is the load-bearing constraint for brand
    -- safety: it is what makes "draft tenant A's copy while grounding on
    -- tenant B's catalogue" unrepresentable rather than merely unlikely.
    -- The corpus loader reads pages WHERE site_id = offpage_drafts.site_id,
    -- so if this FK did not hold, the two would disagree about whose brand
    -- the draft is about.
    FOREIGN KEY (site_id, recommendation_id)
        REFERENCES recommendations (site_id, recommendation_id),

    version           INT NOT NULL DEFAULT 1,
    body_md           TEXT NOT NULL,

    -- Same vocabulary as generated_fixes.generation_source: 'agent'
    -- means LLM-drafted, 'deterministic' means the fallback drafter.
    generation_source TEXT NOT NULL DEFAULT 'agent'
                      CHECK (generation_source IN ('agent','deterministic')),

    -- Gate transcript: every validator that ran and every problem it
    -- raised, so an operator can see why a candidate was refused. This
    -- is the off-page equivalent of the existing
    -- payload["grounding"]["llm_candidates_rejected"].
    validation        JSONB NOT NULL DEFAULT '{}',
    grounding         JSONB NOT NULL DEFAULT '{}',

    -- FROZEN CORPUS PROVENANCE. grounding_corpus_hash is md5 of the
    -- concatenated tenant corpus the draft was allowed to draw from.
    -- Because publishing is manual and may happen days later, this is
    -- how we prove at audit time exactly which of the client's pages
    -- the model could see. grounding_page_ids lists them.
    grounding_corpus_hash TEXT,
    grounding_page_ids    UUID[],
    grounding_corpus_chars INT,

    -- 80/20 helpful-vs-promotional split, measured not asserted.
    helpfulness_score NUMERIC(3,2),
    promo_density     NUMERIC(3,2),
    brand_mention_count INT,
    link_count        INT,

    status            TEXT NOT NULL DEFAULT 'drafted'
                      CHECK (status IN ('drafted','rejected_by_validator',
                                        'approved','superseded')),
    created_at        TIMESTAMPTZ DEFAULT now(),
    approved_at       TIMESTAMPTZ,

    UNIQUE (recommendation_id, version)
);

-- Composite-FK target for offpage_publications.draft_id.
CREATE UNIQUE INDEX IF NOT EXISTS uq_offpage_drafts_tenant
    ON offpage_drafts (site_id, draft_id);

-- One non-superseded draft per recommendation: the live draft.
CREATE UNIQUE INDEX IF NOT EXISTS uq_offpage_draft_live
    ON offpage_drafts (recommendation_id)
    WHERE status IN ('drafted','approved');

CREATE INDEX IF NOT EXISTS idx_offpage_drafts_site
    ON offpage_drafts (site_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_offpage_drafts_rec
    ON offpage_drafts (recommendation_id, version DESC);

-- ------------------------------------------------------------
-- 5. offpage_publications - the manual-execution record
-- ------------------------------------------------------------
-- This table is ALSO the helpful/promotional event trail, so the ratio
-- guard is a plain aggregate and needs no denormalized counter to drift.
--
-- approved_body_md is frozen at gate 2 and is the audit source of truth:
-- it is what the operator was shown, not what they eventually pasted.
--
-- status is the safety state machine:
--   pending_publish  -> draft approved, awaiting a human to paste it
--   pending_verify   -> operator supplied live_permalink, awaiting survival
--   live             -> survived all checkpoints
--   removed          -> automod or mods removed it (3x triggers kill-switch)
--   unverifiable     -> permalink did not resolve; NOT counted as removed
CREATE TABLE IF NOT EXISTS offpage_publications (
    publication_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    site_id           UUID NOT NULL REFERENCES site_config(site_id),
    recommendation_id UUID NOT NULL,
    draft_id          UUID NOT NULL,
    account_id        UUID NOT NULL,
    community_id      UUID NOT NULL,
    thread_id         UUID NOT NULL,

    -- FIVE composite tenant FKs. A publication is the one row that must
    -- agree on a tenant across every dimension it touches - which client
    -- the recommendation belongs to, which draft was approved, which
    -- aged account posted it, which community it went to, and which
    -- thread it answered. Any mismatch (e.g. tenant A's draft posted via
    -- tenant B's aged account) would silently corrupt both the per-account
    -- ratio guard and the per-community strike counter, which are the two
    -- mechanisms protecting the account. These FKs make those mistakes
    -- unrepresentable instead of merely unlikely.
    FOREIGN KEY (site_id, recommendation_id)
        REFERENCES recommendations (site_id, recommendation_id),
    FOREIGN KEY (site_id, draft_id)
        REFERENCES offpage_drafts (site_id, draft_id),
    FOREIGN KEY (site_id, account_id)
        REFERENCES offpage_accounts (site_id, account_id),
    FOREIGN KEY (site_id, community_id)
        REFERENCES offpage_communities (site_id, community_id),
    FOREIGN KEY (site_id, thread_id)
        REFERENCES offpage_threads (site_id, thread_id),

    approved_body_md  TEXT NOT NULL,          -- frozen at gate 2
    live_permalink    TEXT,

    post_type         TEXT NOT NULL
                      CHECK (post_type IN ('helpful','promotional')),
    status            TEXT NOT NULL DEFAULT 'pending_publish'
                      CHECK (status IN ('pending_publish','pending_verify','live',
                                        'removed','deleted_by_operator','unverifiable')),

    submitted_at      TIMESTAMPTZ,
    verified_at       TIMESTAMPTZ,
    operator          TEXT,                   -- who pasted it

    -- Per-checkpoint EVIDENCE lives in offpage_verifications, not here as
    -- JSONB. The split is deliberate and it is about one specific query:
    -- the sweep must claim rows that are DUE and NOT YET CHECKED, which
    -- is a partial index over nullable work
    --     WHERE checked_at IS NULL AND checkpoint_due_at <= now()
    -- A JSONB array cannot express "checked_at is null per element", so
    -- every sweep would have to load and re-parse every pending
    -- publication's checkpoints in application code to work out which
    -- were outstanding. With a table that is one indexed scan.
    --
    -- status stays HERE and is the safety state machine, because that is
    -- what the operator UI and the 3-strike kill-switch read. The table
    -- is the evidence; this column is the state.

    created_at        TIMESTAMPTZ DEFAULT now(),
    updated_at        TIMESTAMPTZ DEFAULT now()
);

-- Composite-FK target for offpage_verifications.publication_id.
CREATE UNIQUE INDEX IF NOT EXISTS uq_offpage_pub_tenant
    ON offpage_publications (site_id, publication_id);

-- The verification sweep's hot path: pending rows past their grace period.
CREATE INDEX IF NOT EXISTS idx_offpage_pub_verify
    ON offpage_publications (site_id, submitted_at)
    WHERE status = 'pending_verify';

-- The promotional-cap gate's hot path: promotional events per account,
-- rolling 30 days. Backs v_offpage_promo_cap_usage.
CREATE INDEX IF NOT EXISTS idx_offpage_pub_promo_cap
    ON offpage_publications (account_id, submitted_at DESC)
    WHERE post_type = 'promotional' AND status IN ('live', 'removed');

CREATE INDEX IF NOT EXISTS idx_offpage_pub_community
    ON offpage_publications (community_id, status);

-- ------------------------------------------------------------
-- 5b. offpage_verifications - per-checkpoint survival evidence
-- ------------------------------------------------------------
-- Four checkpoints per publication (+15m, +2h, +24h, +72h). Rows are
-- PRE-CREATED at publish time with checked_at NULL, so the sweep never
-- has to reason about "which checkpoints should exist yet" - it only
-- finds due rows. That also makes the schedule explicit and auditable
-- rather than implied by a chain of nested IFs in a job.
--
-- The result vocabulary is deliberately four-valued, not boolean:
--   live          - permalink resolved and the comment is present
--   removed       - resolved, but our comment is gone (mods or automod)
--   unverifiable  - could NOT determine; explicitly NOT a removal
--   pending       - not yet checked (implicit: checked_at IS NULL)
--
-- 'unverifiable' is the load-bearing one. A network blip, a logged-out
-- 403, or a rate-limit must never be able to increment a strike count,
-- because the consequence of that is disabling a community on the basis
-- of our own infrastructure failing. Absence of evidence is recorded as
-- absence of evidence.
CREATE TABLE IF NOT EXISTS offpage_verifications (
    verification_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    site_id           UUID NOT NULL,
    publication_id    UUID NOT NULL,

    FOREIGN KEY (site_id, publication_id)
        REFERENCES offpage_publications (site_id, publication_id),

    checkpoint_index  INT NOT NULL
                      CHECK (checkpoint_index BETWEEN 0 AND 3),  -- 0..3
    checkpoint_due_at TIMESTAMPTZ NOT NULL,

    checked_at        TIMESTAMPTZ,
    result            TEXT CHECK (result IN ('live','removed','unverifiable')),

    -- Why we concluded that, for a human reviewing a disputed removal.
    http_status       INT,
    matched_comment_id TEXT,
    detail            TEXT,

    UNIQUE (publication_id, checkpoint_index)
);

-- The sweep's ONLY hot path. Partial, so it contains exactly the due
-- work and stays small however many publications exist.
CREATE INDEX IF NOT EXISTS idx_offpage_verif_due
    ON offpage_verifications (site_id, checkpoint_due_at)
    WHERE checked_at IS NULL;

-- "Did this publication ever get pulled?" - the 3-strike query.
CREATE INDEX IF NOT EXISTS idx_offpage_verif_removed
    ON offpage_verifications (publication_id, checkpoint_index)
    WHERE result = 'removed';

-- ------------------------------------------------------------
-- 5c. offpage_events - the eligibility-gate decision log
-- ------------------------------------------------------------
-- "why the system allowed/rejected each step" is a first-class
-- requirement, not a debug log, and it answers questions the other
-- tables cannot:
--   - why was this draft refused, given the community looks fine?
--   - did we ever consider a promotional draft where rules were unread?
--   - what did the operator actually see at gate 2?
--
-- One row per (entity, gate) evaluation, written on BOTH outcomes. A log
-- that records only refusals is not an audit trail - it cannot prove a
-- gate was ever evaluated and passed, which is the property that makes
-- the system defensible.
--
-- 'unknown' is a first-class outcome and maps to a refuse at the gate.
-- That is the operational expression of "if any answer is uncertain,
-- don't generate the promotional version": uncertainty is recorded
-- rather than silently resolved to a permissive default.
CREATE TABLE IF NOT EXISTS offpage_events (
    event_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    site_id          UUID NOT NULL REFERENCES site_config(site_id),

    entity_type      TEXT NOT NULL
                     CHECK (entity_type IN ('community','thread','account',
                                            'draft','publication',
                                            'recommendation')),
    entity_id        UUID NOT NULL,

    stage            TEXT NOT NULL
                     CHECK (stage IN ('discovery','draft','publish','verify')),
    gate             TEXT NOT NULL,   -- 'community_allows_promotion',
                                      -- 'ai_content_allowed',
                                      -- 'thread_allows_promotion',
                                      -- 'account_standing',
                                      -- 'promotional_cap', 'grounding', ...
    outcome          TEXT NOT NULL
                     CHECK (outcome IN ('pass','refuse','skipped','unknown')),
    reason           TEXT,            -- operator-readable explanation
    evidence         JSONB NOT NULL DEFAULT '{}',
    created_at       TIMESTAMPTZ DEFAULT now()
);

-- "Show me everything we decided about this draft" - the UI's
-- explanation panel, and the answer to "why was this refused?".
CREATE INDEX IF NOT EXISTS idx_offpage_events_entity
    ON offpage_events (site_id, entity_type, entity_id, created_at DESC);

-- "How often does each gate refuse?" - the metric that tells us whether
-- a community's rules are being read at all.
CREATE INDEX IF NOT EXISTS idx_offpage_events_gate
    ON offpage_events (site_id, gate, outcome, created_at DESC);

-- ------------------------------------------------------------
-- 5d. v_offpage_account_mix - retired ratio, read-only
-- ------------------------------------------------------------
-- The trailing-90-day helpful:promotional ratio survives as a DIAGNOSTIC
-- ONLY. It is not referenced by any gate, and nothing in the draft or
-- publish path reads it - a ratio that gates can be satisfied by
-- manufacturing filler, so it must not gate. Kept because "is this
-- account drifting promotional?" is worth watching on a dashboard.
-- NULLIF on the denominator keeps it at NULL rather than infinity when
-- an account has promoted nothing.
CREATE OR REPLACE VIEW v_offpage_account_mix AS
SELECT a.site_id,
       a.account_id,
       a.platform,
       a.handle,
       count(p.publication_id) FILTER (WHERE p.post_type = 'helpful')    AS helpful_90d,
       count(p.publication_id) FILTER (WHERE p.post_type = 'promotional') AS promotional_90d,
       round(
           count(p.publication_id) FILTER (WHERE p.post_type = 'helpful')::numeric
           / NULLIF(count(p.publication_id) FILTER (WHERE p.post_type = 'promotional'), 0),
           2) AS helpful_ratio
FROM offpage_accounts a
LEFT JOIN offpage_publications p
       ON p.account_id = a.account_id
      AND p.status IN ('live', 'removed')
      AND p.submitted_at > now() - INTERVAL '90 days'
GROUP BY a.site_id, a.account_id, a.platform, a.handle;

-- The GATE, which the view above deliberately is not: a hard ceiling on
-- promotional posts per account per rolling 30 days. Counted from
-- publications rather than a denormalised counter, so it cannot drift
-- out of agreement with the event trail.
CREATE OR REPLACE VIEW v_offpage_promo_cap_usage AS
SELECT a.site_id,
       a.account_id,
       a.promotional_cap_30d,
       count(p.publication_id) AS promotional_30d,
       (count(p.publication_id) >= a.promotional_cap_30d) AS cap_reached
FROM offpage_accounts a
LEFT JOIN offpage_publications p
       ON p.account_id = a.account_id
      AND p.post_type = 'promotional'
      AND p.status IN ('live', 'removed')
      AND p.submitted_at > now() - INTERVAL '30 days'
GROUP BY a.site_id, a.account_id, a.promotional_cap_30d;

-- ------------------------------------------------------------
-- 6. recommendations: link a rec to the thread it answers
-- ------------------------------------------------------------
-- Off-page recs insert as generator='offpage_community',
-- action_type='offpage_reply', status='raw', with target_url set to the
-- thread permalink. One added column carries the typed thread link; the
-- rest of the thread context (title, snippet, rules snapshot, community)
-- rides in evidence_json exactly like every other recommendation's
-- evidence does.
--
-- NOTE: action_type='offpage_reply' is deliberately NOT added to
-- measurement_window_lookup. Off-page recommendations park at status
-- 'live' and are never measured - a single community reply cannot clear
-- the min-sample-50 / paired-control / 28-day machinery in
-- src/measurement/classify.py. Adding a lookup row would imply a
-- measurement we do not perform.
ALTER TABLE recommendations
    ADD COLUMN IF NOT EXISTS offpage_thread_id UUID;

-- The thread link is composite too: an off-page recommendation must
-- point at a thread belonging to the SAME tenant.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'recommendations_offpage_thread_tenant_fkey'
    ) THEN
        ALTER TABLE recommendations
            ADD CONSTRAINT recommendations_offpage_thread_tenant_fkey
            FOREIGN KEY (site_id, offpage_thread_id)
            REFERENCES offpage_threads (site_id, thread_id);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_rec_offpage
    ON recommendations (site_id, status, created_at DESC)
    WHERE generator = 'offpage_community';
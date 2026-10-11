-- ============================================================
-- 34 - Off-page operator queue (human-in-the-loop / publication)
-- ============================================================
-- Puts the durable rows the operator UI needs in place for the off-page
-- queue tab (plan/33 tables + drafter were the generate side; this is the
-- decide-and-publish side).
--
-- 1. `advice_md` / `mention_md` on offpage_drafts.
--    The drafter has always produced the two segments separately, but only
--    `body_md` (advice + mention joined by "\n\n") was persisted. The queue
--    UI must show the operator EXACTLY the advice segment and the mention
--    segment, so the split is now stored. Existing rows are backfilled
--    deterministically: the drafter composes body_md as
--    f"{advice}\n\n{mention}".strip() when a mention survived the gates, so
--    the LAST "\n\n" boundary is the advice/mention seam for promotional
--    drafts; helpful drafts carry no mention and body_md IS the advice.
--    `grounding->>'post_type'` discriminates the two cases.
--
-- 2. `rejected_by_operator` on offpage_drafts.status.
--    "Skip / Ineligible" is a HUMAN decision, distinct from the validator's
--    `rejected_by_validator`. Recording it as its own state keeps the audit
--    trail honest: a draft an operator discarded did NOT fail the gates.
--    The status CHECK already carried the 'drafted' -> 'approved' transition
--    gate-2 path; this adds the operator-withdraw terminal state.

-- ------------------------------------------------------------
-- 1. advice_md / mention_md columns + deterministic backfill
-- ------------------------------------------------------------
ALTER TABLE offpage_drafts
    ADD COLUMN IF NOT EXISTS advice_md  TEXT;
ALTER TABLE offpage_drafts
    ADD COLUMN IF NOT EXISTS mention_md TEXT;

-- Backfill for rows created before this migration. The split rule:
--   helpful      => mention is empty, body_md IS the advice
--   promotional  => body_md is advice + "\n\n" + mention; the seam is the
--                   LAST "\n\n" because advice may itself contain
--                   paragraph breaks and the drafter joins at the end.
-- NOTE: do NOT add CHECKs like "advice_md IS NOT NULL" here — the pre-draft
-- draft row (validator-refused rows never insert, but superseded/legacy
-- rows exist) and rows written by older code paths need to stay valid.
UPDATE offpage_drafts
SET advice_md = CASE
        WHEN (grounding->>'post_type') = 'promotional'
             AND POSITION(E'\n\n' IN REVERSE(body_md)) > 0
        THEN SUBSTRING(body_md
                       FROM 1
                       FOR LENGTH(body_md)
                           - POSITION(E'\n\n' IN REVERSE(body_md)) - 1)
        ELSE body_md
    END,
    mention_md = CASE
        WHEN (grounding->>'post_type') = 'promotional'
             AND POSITION(E'\n\n' IN REVERSE(body_md)) > 0
        THEN SUBSTRING(body_md
                       FROM LENGTH(body_md)
                            - POSITION(E'\n\n' IN REVERSE(body_md)) + 2)
        ELSE ''
    END
WHERE advice_md IS NULL;

-- ------------------------------------------------------------
-- 2. rejected_by_operator draft status
-- ------------------------------------------------------------
-- The default-generated constraint name is <table>_<column>_check; make the
-- migration idempotent by guarding both the drop and the add.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'offpage_drafts_status_check'
    ) THEN
        ALTER TABLE offpage_drafts DROP CONSTRAINT offpage_drafts_status_check;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'offpage_drafts_status_check'
    ) THEN
        ALTER TABLE offpage_drafts
            ADD CONSTRAINT offpage_drafts_status_check
            CHECK (status IN ('drafted', 'rejected_by_validator',
                              'approved', 'superseded',
                              'rejected_by_operator'));
    END IF;
END $$;

-- Operator queue hot path: oldest pending draft first. The existing
-- idx_offpage_drafts_site (site_id, status, created_at DESC) already backs
-- the queue read; add nothing more.
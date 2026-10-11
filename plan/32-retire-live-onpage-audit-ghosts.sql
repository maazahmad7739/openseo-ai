-- ============================================================
-- plan/32 — Retire the `live_onpage_audit` ghost generator
-- ============================================================
-- A one-off generator once wrote "Commercial/landing page missing structured
-- data" recommendations with `evidence->>'source' = 'live_onpage_audit'`.
-- That code path no longer exists in the tree, so nothing can reproduce,
-- maintain, or act on these rows — they are inert leftovers, not findings.
--
-- On kineticgrowth.co.in they appeared as 18 identical rows for ONE
-- JSON-LD gap, all created in a single 2026-09-27 10:15 run:
--
--   * 18 separate operator decisions for one defect;
--   * every one of them WRONG — the live homepage carries Organization /
--     WebSite / ContactPoint JSON-LD. The generator asserted an absence it
--     had never measured.
--
-- plan/32 fixes the underlying causes (grouping by template in plan/03,
-- and the crawl-evidence guard that stops absence being reported on pages
-- that were never actually loaded). This migration only disposes of the
-- wreckage.
--
-- REJECT, do not DELETE. These rows have child generated_fixes, and
-- rejection_log / rejection_reason preserve why they were wrong. Deleting
-- would destroy the audit trail that proves the defect was never real —
-- exactly the evidence needed to argue this is not a live regression.
--
-- Idempotent: rows already 'rejected' are matched by the same predicate and
-- rewritten to the same values, so re-running is a no-op.
-- ============================================================

BEGIN;

-- ------------------------------------------------------------
-- 1. Log the retirement (once per row; re-running is a no-op because
--    the UPDATE in step 2 only touches rows not yet carrying this reason)
-- ------------------------------------------------------------
INSERT INTO rejection_log
    (site_id, run_id, candidate_id, generator, primary_keyword,
     target_url, reason, rejected_by)
SELECT
    r.site_id,
    'plan32-retire-live-onpage-audit',
    r.recommendation_id,
    r.generator,
    -- recommendations has no primary_keyword column (the issue type rides in
    -- evidence_json, plan/03 reuses that slot); the log column is nullable.
    NULL,
    r.target_url,
    'generator source live_onpage_audit was removed from the codebase; '
    'row asserted an unmeasured absence of structured data and is not '
    'actionable (plan/32)',
    'system:plan32'
FROM recommendations r
WHERE r.evidence_json->>'source' = 'live_onpage_audit'
  AND r.status <> 'rejected'
  AND NOT EXISTS (
      SELECT 1 FROM rejection_log l
      WHERE l.candidate_id = r.recommendation_id
        AND l.run_id = 'plan32-retire-live-onpage-audit'
  );

-- ------------------------------------------------------------
-- 2. Permanently reject
-- ------------------------------------------------------------
UPDATE recommendations
SET status           = 'rejected',
    rejected_at      = COALESCE(rejected_at, now()),
    rejection_reason = COALESCE(
        rejection_reason,
        'plan/32: retired live_onpage_audit generator — asserted an '
        'unmeasured absence of structured data; superseded by the '
        'template-grouped, crawl-guarded plan/03 rule'
    )
WHERE evidence_json->>'source' = 'live_onpage_audit'
  AND status <> 'rejected';

-- ------------------------------------------------------------
-- 3. Any fix generated from a ghost row is just as unfounded as its
--    source. Retire those too rather than leaving a queued PR that
--    injects schema the page may not need.
--
--    'expired', not 'rejected': generated_fixes_status_check allows only
--    generated/approved/queued/applied/failed/reverted/expired. 'expired'
--    is also the honest reading — the fix was never wrong at execution, it
--    was retired before it could run.
-- ------------------------------------------------------------
UPDATE generated_fixes
SET status       = 'expired',
    error_detail = COALESCE(
        error_detail,
        'plan/32: source recommendation was a retired live_onpage_audit ghost'
    )
WHERE recommendation_id IN (
    SELECT r.recommendation_id
    FROM recommendations r
    WHERE r.evidence_json->>'source' = 'live_onpage_audit'
)
  AND status IN ('generated', 'approved', 'queued');

COMMIT;

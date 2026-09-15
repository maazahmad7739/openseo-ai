-- plan/16 — raw -> proposed candidate lifecycle (Phase 6 Fix Brief P1/P2/P3)
-- Idempotent; same pattern as plan/14-15.
--
-- Goals:
--   1. Agent rejections that were written to rejection_log but never propagated
--      to the recommendation row (pre-fix persist bug) -> status='rejected'.
--   2. Candidate rows the agent never processed (status='proposed' with no
--      decision recorded in rejection_log) -> status='raw'.
--   3. Dedupe natural-key collisions (same site/generator/action_type/cluster/
--      target/proposed) keeping the most-advanced row, so the unique index in
--      step 4 can be created. rejection_log has no FK into recommendations, so
--      history for removed rows survives in the log.
--   4. Natural-key UNIQUE INDEX making re-generation idempotent per candidate:
--      orchestrator's ON CONFLICT ... DO NOTHING now actually does something.
--      Documented rule: a row the agent already promoted/rejected keeps its
--      status -- re-generation never knocks enriched work back to 'raw' and
--      never resurrects a rejected candidate.
--   5. measurement_window_lookup.metric: single source of truth for the metric
--      shown on the operator detail screen (P3.1), so the UI never renders
--      "Metric: not set" for a valid action_type.

-- 1) agent-rejected but status never written -> rejected
UPDATE recommendations r
SET status = 'rejected',
    rejection_reason = COALESCE(r.rejection_reason, sub.reason),
    rejected_at = COALESCE(r.rejected_at, sub.rejected_at)
FROM (
    SELECT DISTINCT ON (candidate_id)
           candidate_id,
           reason,
           created_at AS rejected_at
    FROM rejection_log
    WHERE rejected_by = 'agent' AND candidate_id IS NOT NULL
    ORDER BY candidate_id, created_at ASC
) sub
WHERE r.status = 'proposed'
  AND sub.candidate_id = r.recommendation_id;

-- 2) unprocessed candidates (no decision in rejection_log) -> raw
UPDATE recommendations
SET status = 'raw'
WHERE status = 'proposed'
  AND NOT EXISTS (
      SELECT 1 FROM rejection_log l
      WHERE l.candidate_id = recommendations.recommendation_id
  );

-- 3) dedupe natural-key collisions (keep most-advanced; newest on tie)
WITH ranked AS (
    SELECT recommendation_id,
           CASE status WHEN 'measured' THEN 5 WHEN 'live' THEN 4
                       WHEN 'in_progress' THEN 3 WHEN 'approved' THEN 2
                       WHEN 'rejected' THEN 1 WHEN 'proposed' THEN 0 ELSE -1
           END AS rank,
           created_at
    FROM recommendations
),
to_delete AS (
    SELECT a.recommendation_id
    FROM recommendations a
    JOIN ranked ra ON ra.recommendation_id = a.recommendation_id
    JOIN recommendations b ON b.recommendation_id <> a.recommendation_id
    JOIN ranked rb ON rb.recommendation_id = b.recommendation_id
    WHERE a.site_id = b.site_id
      AND a.generator = b.generator
      AND a.action_type = b.action_type
      AND COALESCE(a.cluster_id, '00000000-0000-0000-0000-000000000000'::uuid)
          = COALESCE(b.cluster_id, '00000000-0000-0000-0000-000000000000'::uuid)
      AND COALESCE(a.target_url, '') = COALESCE(b.target_url, '')
      AND COALESCE(a.proposed_url, '') = COALESCE(b.proposed_url, '')
    AND (ra.rank < rb.rank OR (ra.rank = rb.rank AND a.created_at < b.created_at))
)
DELETE FROM recommendations r
USING to_delete d
WHERE r.recommendation_id = d.recommendation_id;

-- 4) natural-key unique index -> idempotent re-generation
CREATE UNIQUE INDEX IF NOT EXISTS uq_recommendations_candidate
ON recommendations (
    site_id, generator, action_type,
    COALESCE(cluster_id, '00000000-0000-0000-0000-000000000000'::uuid),
    COALESCE(target_url, ''), COALESCE(proposed_url, '')
);

-- 5) metric column on the lookup (single source of truth, P3.1)
ALTER TABLE measurement_window_lookup ADD COLUMN IF NOT EXISTS metric TEXT;
UPDATE measurement_window_lookup SET metric = 'impressions'       WHERE action_type = 'create_page'    AND metric IS NULL;
UPDATE measurement_window_lookup SET metric = 'clicks'            WHERE action_type = 'improve_page'   AND metric IS NULL;
UPDATE measurement_window_lookup SET metric = 'organic_sessions'  WHERE action_type = 'consolidate'    AND metric IS NULL;
UPDATE measurement_window_lookup SET metric = 'impressions'       WHERE action_type = 'technical_fix'  AND metric IS NULL;
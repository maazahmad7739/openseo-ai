-- plan/29: webhook + pipeline hardening (Category 2, item 2.9).
--
-- 1. Webhook fan-out lookup index. The GitHub webhook receiver
--    (src/api/routes/github_sync.py) matches applied fixes by
--    snapshot_json->>'pr.url' on every merged-PR event. Before this
--    index the match was a full scan of generated_fixes; the partial
--    expression index keeps the lookup O(fixes-per-PR) and the index
--    itself tiny (only rows that carry a pr.url are indexed).
--
-- Idempotent: CREATE INDEX IF NOT EXISTS. Safe to re-run against a live
-- database (concurrent builds may use CREATE INDEX CONCURRENTLY manually
-- for very large tables; plain build is fine at current row counts).

CREATE INDEX IF NOT EXISTS idx_fixes_pr_url
    ON generated_fixes ((snapshot_json->>'pr.url'))
    WHERE snapshot_json ? 'pr.url';
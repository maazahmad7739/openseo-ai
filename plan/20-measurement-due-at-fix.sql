-- ============================================================
-- plan/20-measurement-due-at-fix.sql
-- measurement_due_at: recompute from implemented_at, not promotion time
-- ============================================================
-- Problem: seo_agent persist_agent_output() set measurement_due_at =
-- now() + window at PROPOSAL time. If the operator implemented days later,
-- the due date had already (partly) elapsed, so the pipeline kanban showed
-- a skewed countdown and any code keying off the column would measure too
-- early.
--
-- Fix (code side, this migration is the one-time repair):
--   * implement / implement-safe now recompute measurement_due_at in the
--     same UPDATE as the status transition (routes/measurements.py), from
--     measurement_window_lookup — never a Python constant.
--   * The measurement clock computes its own due condition from
--     implemented_at + window + GSC_SETTLE_DAYS and never reads this
--     column; the column is a display projection only.
--
-- This migration backfills the column for rows already past proposal so
-- live/in-progress rows stop showing a skewed countdown.
-- Idempotent: re-running recomputes the same values.
-- ============================================================

-- Only in-flight rows need repair: proposed/approved rows have no
-- implemented_at yet (they get a correct value at implement), and measured
-- rows are done. live/in_progress rows carry implemented_at.
UPDATE recommendations r
SET measurement_due_at = r.implemented_at::date
    + (SELECT mwl.measurement_window_days
       FROM measurement_window_lookup mwl
       WHERE mwl.action_type = r.action_type)
WHERE r.status IN ('in_progress', 'live')
  AND r.implemented_at IS NOT NULL;
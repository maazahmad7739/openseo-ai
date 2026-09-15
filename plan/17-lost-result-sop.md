# Lost Result Handling SOP — Draft for Discussion

**Status:** Draft — not yet approved. For Shrey/team discussion.
**Created:** Phase 4 of the correction brief

## What triggers this SOP

When a recommendation reaches `result = 'lost'` via the measurement pipeline, meaning the implemented change is associated with a statistically significant decline (two-proportion z-test, see `src/measurement/classify.py:265-278`).

The recommendation is simultaneously marked `status = 'measured'`, which removes it from the operator queue and surfaces it on the Results Dashboard.

## Questions for the team to answer

### 1. Who gets notified?
- [ ] The person who was assigned the recommendation?
- [ ] The site owner / account lead?
- [ ] Everyone in the team channel?

### 2. Is human review required before any action?
- [ ] Yes — a human must review and explicitly decide next steps
- [ ] No — auto-rollback after N days unless overridden

### 3. What does "rollback" mean?
- [ ] Literally revert the change (unpublish page, remove redirect, undo code change)
- [ ] Deprioritize the cluster/approach going forward (no new recommendations for this cluster)
- [ ] Both — revert AND mark cluster as "approach failed"

### 4. What information should the operator see?
- [ ] Before/after metrics for the target page
- [ ] Control group trend (was it site-wide decline or specific to this change?)
- [ ] Recommended next steps based on the recommendation type

### 5. Should lost results affect future recommendations?
- [ ] Never re-propose for the same cluster (permanent block)
- [ ] Re-propose after N weeks if underlying signals improved
- [ ] Re-propose immediately with a different approach

### 6. Time limit for review
- [ ] Review within N business days
- [ ] Escalate if not reviewed within N days

## Current system behavior

- `result = 'lost'` is set by `src/measurement/classify.py` when a two-proportion z-test shows significant decline
- The recommendation is marked `status = 'measured'`
- No automatic next step is triggered
- The operator must check the Results Dashboard to see lost results
- The `lost` badge is visually distinct (red) from `won` (green) and `neutral` (gray) on the dashboard
- There is no `reviewed_at` or `reviewed_by` field on the recommendations table
- There is no notification or escalation mechanism

## Next steps after this SOP is approved

1. Implement the agreed notification mechanism
2. Add `reviewed_at`/`reviewed_by` fields if required (schema migration)
3. Implement any automated escalation rules
4. Update the operator dashboard to support the review workflow

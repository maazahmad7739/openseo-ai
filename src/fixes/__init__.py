"""Stage 2 fix framework (plan/21): generator, policy, adapters, executor.

One generic framework; only decision-logic + write-adapter vary per type.
Nothing publishes without two human gates (recommendation approve + diff
approve). queued -> applied happens ONLY in the executor job, never inline
in an API request.
"""
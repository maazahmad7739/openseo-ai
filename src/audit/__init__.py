"""Audit engine package (plan/23 — on-demand brand-agnostic URL audit).

Phase A scope: page ingest + site resolution only (zero external SERP
spend). Every module is read-only with respect to the batch pipeline's
tables; the engine writes ONLY its own audit_* session tables.
"""
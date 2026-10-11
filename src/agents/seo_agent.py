"""Principal SEO agent (plan/05) — evaluates candidates via hosted Ollama Cloud.

Flow (plan/05 Agent Invocation Flow, steps 10-15; plan/16 raw→proposed lifecycle):
  1. Load raw recommendations (generator output, status='raw') for the site.
  2. Build the agent input payload: candidates + cluster + evidence +
     page_context + rejection_context + config.
  3. Build the system prompt: prompts/agent_system.md + the full skill files
     (plan/16, plan/17) for the generators present in the batch
     (SKILL_FILES_BY_GENERATOR, build_system_prompt).
  4. Invoke Ollama Cloud (OLLAMA_API_BASE + OLLAMA_API_KEY) with that prompt.
  5. Validate the model's JSON against the plan/05 output schema.
  6. Persist: accepted → enrich the raw row and promote it to 'proposed'
     (agent-validated, awaiting operator decision); rejected → set the row
     to 'rejected' WITH a reason in rejection_log. Only the agent promotes
     raw → proposed; the operator queue shows exclusively 'proposed' rows.

No offline stub: construction without credentials raises OllamaConfigError
loudly (per roadmap decision).
"""

import os
import sys
import json
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.llm_client import FallbackClient  # noqa: E402
from agents.ollama_client import OllamaApiError, OllamaConfigError  # noqa: E402
from agents.gemini_client import (  # noqa: E402
    GeminiApiError,
    GeminiConfigError,
)

import db as database  # noqa: E402
import env as env_loader  # noqa: E402

PROMPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts", "agent_system.md")
SKILLS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "plan")
AGENT_VERSION = "1.1.0"
MAX_RECOMMENDATIONS = 5
# Generator → skill file. Skill files are injected verbatim into the system
# prompt for the generators actually present in the batch (dynamic loader,
# run_agent step 3). Only skills with full rule files are wired; the
# missing_page / existing_opportunity stubs carry their rules inline in
# agent_system.md, so they need no file.
SKILL_FILES_BY_GENERATOR = {
    "technical_fix": os.path.join(SKILLS_DIR, "16-skill-technical-fix.md"),
    "cannibalization": os.path.join(SKILLS_DIR, "17-skill-consolidate-cannibalization.md"),
}
REQUIRED_REC_KEYS = {"action_type", "diagnosis", "evidence", "work_required",
                     "impact", "confidence", "effort", "owner"}


def load_measurement_windows(conn):
    """Measurement windows read from measurement_window_lookup (fix 3.3).

    The DB lookup is the single source of truth; no duplicated copy in
    Python. Falls back to an empty dict only if the table is empty — the
    agent then leaves measurement_window_days to the persistence layer,
    which resolves from the same lookup.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT action_type, measurement_window_days FROM measurement_window_lookup")
        return {r[0]: r[1] for r in cur.fetchall()}


class AgentValidationError(Exception):
    pass


def load_system_prompt(path=PROMPT_PATH):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def load_skill_file(path):
    """Read one skill file; raise loudly if missing (no silent stubs)."""
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def build_system_prompt(candidates, system_prompt=None):
    """System prompt = agent_system.md + full skill files for the generators
    present in this batch (plan/16 skill 4, plan/17 skill 5).

    The abbreviated SKILLS BY CANDIDATE TYPE summaries in agent_system.md are
    replaced per injected skill so the model executes the complete rules
    (per-issue_type checks, worked examples, final checklists) rather than
    the stubs. Deterministic: same candidates → same prompt.
    """
    system_prompt = system_prompt if system_prompt is not None else load_system_prompt()
    generators = sorted({c.get("generator") for c in candidates if c.get("generator")})
    injected = []
    for gen in generators:
        skill_path = SKILL_FILES_BY_GENERATOR.get(gen)
        if not skill_path:
            continue
        skill_text = load_skill_file(skill_path)
        injected.append(
            f"\n\n---\n\n# SKILL FILE (loaded for generator: {gen})\n\n{skill_text}"
        )
    if injected:
        injected_names = sorted({
            gen for gen in generators if SKILL_FILES_BY_GENERATOR.get(gen)
        })
        note = (
            "SKILL FILE PRECEDENCE:\n"
            f"Full skill files are APPENDED below only for: {', '.join(injected_names)}.\n"
            "For those types the appended file supersedes the inline summary.\n"
            "Candidate types WITHOUT an appended file (missing_page,\n"
            "existing_opportunity) follow the inline SKILLS BY CANDIDATE TYPE\n"
            "summaries exactly — they are complete rules, not placeholders.\n"
            "'No skill file' is NEVER a valid rejection reason; evaluate every\n"
            "candidate with the rules that apply to its type."
        )
        summary_start = system_prompt.find("SKILLS BY CANDIDATE TYPE:")
        summary_end = system_prompt.find("OUTPUT:", summary_start)
        if summary_start != -1 and summary_end != -1:
            system_prompt = (system_prompt[:summary_start] +
                             system_prompt[summary_start:summary_end].rstrip() +
                             "\n\n" + note + "\n\n" +
                             system_prompt[summary_end:])
        system_prompt = system_prompt + "".join(injected)
    return system_prompt


def build_agent_input(conn, site_id, max_candidates=25):
    """Assemble the plan/05 agent input payload from the DB."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT domain, site_name, catalogue_size_tier, in_stock_definition, "
            "agent_prefetch_limit, brand_queries FROM site_config WHERE site_id = %s",
            (site_id,),
        )
        row = cur.fetchone()
        if not row:
            raise AgentValidationError(f"unknown site_id {site_id}")
        (domain, site_name, tier, in_stock_def,
         prefetch_limit, brand_queries) = row

        # Active-window budget: the operator queue holds at most
        # MAX_ACTIVE_QUEUE proposed cards (api/routes/queue.py). Generating a
        # fresh batch of MAX_RECOMMENDATIONS on top of an undrained queue is
        # what let the backlog grow past the cap — the UI then masked it by
        # slicing locally. Top up only the free slots; leave the rest as 'raw'
        # for the next run.
        cur.execute(
            "SELECT count(*) FROM recommendations "
            "WHERE site_id = %s AND status = 'proposed'",
            (site_id,),
        )
        proposed_now = cur.fetchone()[0]
        budget = max(MAX_RECOMMENDATIONS - int(proposed_now or 0), 0)
        prefetch = min(int(prefetch_limit or 25), budget) if budget else 0

        rows = []
        if prefetch > 0:
            cur.execute(
                "SELECT recommendation_id, generator, action_type, target_url, proposed_url, "
                "       cluster_id, diagnosis, evidence_json "
                "FROM recommendations WHERE site_id = %s AND status = 'raw' "
                "ORDER BY created_at DESC LIMIT %s",
                (site_id, prefetch),
            )
            rows = cur.fetchall()

        candidate_payloads = []
        for (rec_id, generator, action_type, target_url, proposed_url,
             cluster_id, diagnosis, evidence_json) in rows:
            cluster = None
            serp_evidence = []
            if cluster_id:
                cur.execute(
                    "SELECT cluster_id, primary_keyword, keywords, intent, search_volume, "
                    "commercial_value FROM keyword_clusters "
                    "WHERE cluster_id = %s AND site_id = %s",
                    (cluster_id, site_id),
                )
                crow = cur.fetchone()
                if crow:
                    cluster = {
                        "cluster_id": str(crow[0]),
                        "primary_keyword": crow[1],
                        "keywords": crow[2] or [],
                        "intent": crow[3],
                        "search_volume": crow[4],
                        "commercial_value": crow[5],
                    }
                # Live SERP evidence (skill 1 §1 requirement): the latest
                # snapshot rows for this cluster — who outranks whom, at which
                # positions. Cap at 10 rows; empty list when none exist (the
                # agent may then reject for missing SERP confirmation, which
                # is the honest verdict, not a payload defect).
                cur.execute(
                    """
                    SELECT result_url, result_domain, position, is_self, snapshot_date
                    FROM openseo_serp_snapshots
                    WHERE site_id = %s AND cluster_id = %s
                    ORDER BY snapshot_date DESC, position ASC LIMIT 10
                    """,
                    (site_id, cluster_id),
                )
                serp_evidence = [
                    {
                        "position": r[2],
                        "url": r[1] and r[0] or r[0],
                        "domain": r[1],
                        "is_self": r[3],
                        "snapshot_date": str(r[4]),
                    }
                    for r in cur.fetchall()
                ]
            try:
                # psycopg2 already decodes JSONB to dict/list; json.loads()
                # would raise TypeError on those (silently emptied by the
                # except below, starving the agent of all generator evidence).
                if isinstance(evidence_json, (dict, list)):
                    evidence = evidence_json
                elif evidence_json:
                    evidence = json.loads(evidence_json)
                else:
                    evidence = []
            except (TypeError, ValueError):
                evidence = []
            # Page context for skill validation (plan/16 skill 4 checks
            # render_status / indexability / crawl depth; plan/06/07 need GSC
            # signals): crawl columns from pages + 28-day GSC aggregate for
            # the target URL. All optional — NULL when no page row exists.
            page_context = None
            target = target_url or proposed_url
            if target:
                full_url = target if target.startswith("http") else f"https://{domain}{target}"
                cur.execute(
                    """
                    SELECT p.indexable, p.status_code, p.canonical_url, p.crawl_depth,
                           p.internal_links_in, p.internal_links_out, p.product_count,
                           p.is_orphan, p.has_structured_data, p.render_status,
                           p.raw_html_hash, p.rendered_html_hash, p.page_type, p.title,
                           p.last_crawled_at
                    FROM pages p WHERE p.site_id = %s AND p.url = %s
                    """,
                    (site_id, full_url),
                )
                prow = cur.fetchone()
                if prow:
                    cur.execute(
                        """
                        SELECT COALESCE(SUM(s.clicks), 0), COALESCE(SUM(s.impressions), 0),
                               COALESCE(AVG(s.position), NULL), COUNT(DISTINCT s.query)
                        FROM search_performance s
                        WHERE s.site_id = %s AND s.page_url = %s
                          AND s.date >= CURRENT_DATE - 28
                        """,
                        (site_id, full_url),
                    )
                    srow = cur.fetchone()
                    page_context = {
                        "url": full_url,
                        "indexable": prow[0],
                        "status_code": prow[1],
                        "canonical_url": prow[2],
                        "crawl_depth": prow[3],
                        "internal_links_in": prow[4],
                        "internal_links_out": prow[5],
                        "product_count": prow[6],
                        "is_orphan": prow[7],
                        "has_structured_data": prow[8],
                        "render_status": prow[9],
                        "rendering_verifiable": bool(prow[10] and prow[11]),
                        "raw_html_hash_present": bool(prow[10]),
                        "rendered_html_hash_present": bool(prow[11]),
                        "page_type": prow[12],
                        "title": prow[13],
                        # Trust flag: has_structured_data is BOOLEAN DEFAULT
                        # false, so False is only evidence of absence when a
                        # crawl actually ran. Without this the agent cannot
                        # distinguish "looked, none present" from "never
                        # looked" and reports the second as the first
                        # (plan/03 structured_data_failure false positives).
                        "last_crawled_at": (
                            prow[14].isoformat() if prow[14] else None),
                        "structured_data_verifiable": bool(prow[14]),
                        "gsc_28d": {
                            "clicks": int(srow[0]),
                            "impressions": int(srow[1]),
                            "avg_position": float(srow[2]) if srow[2] is not None else None,
                            "queries": int(srow[3]),
                        },
                    }
            candidate_payloads.append({
                "candidate_id": str(rec_id),
                "generator": generator,
                "action_type": action_type,
                "target_url": target_url,
                "proposed_url": proposed_url,
                "cluster": cluster,
                "candidate_diagnosis": diagnosis,
                "evidence_summary": evidence if isinstance(evidence, (dict, list)) else [],
                "serp_evidence": serp_evidence,
                "page_context": page_context,
            })

        cur.execute(
            "SELECT generator, primary_keyword, reason FROM rejection_log "
            "WHERE site_id = %s ORDER BY created_at DESC LIMIT 5",
            (site_id,),
        )
        rejections = [
            {"generator": g, "primary_keyword": pk, "reason": reason}
            for g, pk, reason in cur.fetchall()
        ]

        payload = {
            "run_id": str(uuid.uuid4()),
            "site": {
                "domain": domain,
                "site_name": site_name,
                "catalogue_size_tier": tier,
                "in_stock_definition": in_stock_def,
            },
            "candidates": candidate_payloads,
            "rejection_context": {
                "site_id": str(site_id),
                "last_rejections": rejections,
                "bulk": "Avoid re-proposing these patterns this week.",
            },
            "config": {
                "max_recommendations": budget,
                "proposed_remaining": budget,
                "excluded_patterns": list(brand_queries or []),
            },
        }
        return payload


def validate_agent_output(output, candidate_ids, valid_action_types=None):
    """Enforce the plan/05 output schema; returns (recommendations, rejections).

    valid_action_types comes from measurement_window_lookup at runtime
    (single source of truth; fix 3.3). When not supplied, validation of the
    action_type enum is skipped (test harnesses).
    """
    if not isinstance(output, dict):
        raise AgentValidationError("agent output must be a JSON object")
    recs = output.get("recommendations")
    rejects = output.get("rejection_log")
    if not isinstance(recs, list) or not isinstance(rejects, list):
        raise AgentValidationError("recommendations and rejection_log must be arrays")
    if len(recs) > MAX_RECOMMENDATIONS:
        raise AgentValidationError(f"more than {MAX_RECOMMENDATIONS} recommendations")

    known_ids = set(candidate_ids)
    for r in recs:
        if not isinstance(r, dict):
            raise AgentValidationError("each recommendation must be an object")
        missing = REQUIRED_REC_KEYS - set(r.keys())
        if missing:
            raise AgentValidationError(f"recommendation missing keys: {sorted(missing)}")
        if valid_action_types is not None and r.get("action_type") not in valid_action_types:
            raise AgentValidationError(f"invalid action_type: {r.get('action_type')}")
        if r.get("impact") not in ("high", "medium", "low"):
            raise AgentValidationError(f"invalid impact: {r.get('impact')}")
        if r.get("confidence") not in ("high", "medium", "low"):
            raise AgentValidationError(f"invalid confidence: {r.get('confidence')}")
        # Model tolerance: '4 hours' / '1 day' normalize to the enum the
        # schema (and the DB CHECK) expects — an entire run must not die
        # because the model phrased effort as a duration with a count.
        effort = str(r.get("effort") or "").strip().lower()
        if "hour" in effort:
            r["effort"] = "hours"
        elif "day" in effort:
            r["effort"] = "days"
        if r.get("effort") not in ("hours", "days"):
            raise AgentValidationError(f"invalid effort: {r.get('effort')}")
        if r.get("owner") not in ("SEO", "content", "engineering"):
            raise AgentValidationError(f"invalid owner: {r.get('owner')}")

    for rej in rejects:
        if not isinstance(rej, dict) or not rej.get("candidate_id") or not rej.get("reason"):
            raise AgentValidationError("each rejection needs candidate_id and reason")
        if known_ids and rej.get("candidate_id") not in known_ids:
            raise AgentValidationError(f"rejection for unknown candidate_id: {rej.get('candidate_id')}")
    return recs, rejects


# Rules whose impact is decided deterministically by the generator SQL and
# must not be re-derived by the model. Keyed on the generator's issue_type,
# which the generator stamps into the evidence payload.
DETERMINISTIC_IMPACT_ISSUES = ("structured_data_failure",)


def _issue_types(evidence_json):
    """Which deterministically-scored rule produced this evidence payload.

    Two signals, because rows generated before plan/03 started stamping
    `issue_type` into the evidence object are still in the raw queue:

      * `issue_type` — explicit, preferred (plan/03 structured_data_failure).
      * an evidence object carrying `has_structured_data` — the legacy
        signature of that same CTE, which is the only generator-3 rule whose
        evidence includes that key.
    """
    if isinstance(evidence_json, str):
        try:
            evidence_json = json.loads(evidence_json)
        except (TypeError, ValueError):
            return ()
    if isinstance(evidence_json, dict):
        items = [evidence_json]
    elif isinstance(evidence_json, list):
        items = [e for e in evidence_json if isinstance(e, dict)]
    else:
        return ()
    out = set()
    for e in items:
        val = e.get("issue_type")
        if isinstance(val, str) and val in DETERMINISTIC_IMPACT_ISSUES:
            out.add(val)
        elif "has_structured_data" in e:
            out.add("structured_data_failure")
    return tuple(out)


# Evidence keys the GENERATOR authored and that downstream code depends on.
# plan/32: fixes/generator.py expands one grouped structured_data_failure
# recommendation into a fix per affected page by reading `template` and
# `page_count` from evidence. The model sees the recommendation and rewrites
# `evidence` wholesale in persist_agent_output, so without this the grouping
# metadata is silently dropped at raw→proposed and every grouped fix silently
# degrades back to a single-URL fix — or to nothing.
#
# These are facts about what was measured, not opinions, so the generator's
# value wins over anything the model returns for the same key. The model may
# still ADD its own keys; it just cannot restate the measurement.
GENERATOR_PROVENANCE_KEYS = (
    "issue_type",
    "has_structured_data",
    "scope",
    "template",
    "template_inferred",
    "page_count",
    "sample_urls",
    "representative_url",
    "page_type",
)


def _merge_generator_evidence(model_evidence, stored_evidence):
    """Re-attach generator-authored evidence keys under the model's output.

    Returns the model's payload unchanged when the row carries no generator
    provenance, so rows the generator did not shape behave exactly as before.
    A list payload collapses to a dict here, which is a shape every consumer
    of evidence already handles (_issue_types, the fix generator).
    """
    if not isinstance(stored_evidence, dict):
        if isinstance(stored_evidence, str):
            try:
                stored_evidence = json.loads(stored_evidence)
            except (TypeError, ValueError):
                return model_evidence
        if not isinstance(stored_evidence, dict):
            return model_evidence
    provenance = {k: stored_evidence[k] for k in GENERATOR_PROVENANCE_KEYS
                  if k in stored_evidence}
    if not provenance:
        return model_evidence
    if isinstance(model_evidence, dict):
        merged = dict(model_evidence)
    elif isinstance(model_evidence, list):
        merged = {}
        for item in model_evidence:
            if isinstance(item, dict):
                merged.update(item)
    elif model_evidence is None:
        merged = {}
    else:
        merged = {"detail": model_evidence}
    merged.update(provenance)
    return merged


def _stored_recommendation_state(conn, site_id, recs):
    """{recommendation_id: (impact, evidence)} for rows the generator scored.

    The impact already in the column is the generator's verdict, written at
    insert time (orchestrator._insert_recommendations) and untouched since —
    raw rows are not yet visible to the operator, so nothing else has claimed
    the value. Only rows the generator itself flagged are pinned; every other
    rule keeps the model-authored impact.
    """
    ids = [str(r.get("_recommendation_id")) for r in recs if r.get("_recommendation_id")]
    if not ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT recommendation_id, impact, evidence_json FROM recommendations "
            "WHERE site_id = %s AND recommendation_id = ANY(%s::uuid[])",
            (site_id, ids),
        )
        rows = cur.fetchall()
    return {
        str(rid): (impact, evidence)
        for rid, impact, evidence in rows
        if impact and _issue_types(evidence)
    }


def _pinned_impact_map(conn, site_id, recs):
    """{recommendation_id: stored_impact} for deterministically-scored rows."""
    return {rid: impact
            for rid, (impact, _ev) in _stored_recommendation_state(conn, site_id, recs).items()}


def persist_agent_output(conn, site_id, recs, rejects, measurement_windows=None):
    """Accepted → enrich the raw row and promote to 'proposed'; rejected → set the
    row to 'rejected' with reason + rejection_log.

    Raw→proposed is the ONLY promotion to the operator queue (plan/16): rows the
    agent accepts become operator-visible; rows it rejects are removed from the
    candidate cycle with a logged reason.

    measurement_windows: dict from measurement_window_lookup (single source);
    resolved at runtime when not supplied.
    """
    if measurement_windows is None:
        measurement_windows = load_measurement_windows(conn)
    # Deterministic-impact rows (plan/03 structured_data_failure) must keep the
    # generator's page_type verdict: the skill's generic rubric grades on GSC
    # demand, which plan/03 deliberately withheld for this rule
    # (gsc_clicks_28d is hardcoded NULL there). Letting the model apply a
    # traffic rubric here is what made identical defects score differently.
    pinned = _pinned_impact_map(conn, site_id, recs)
    stored = _stored_recommendation_state(conn, site_id, recs)
    accepted = 0
    with conn.cursor() as cur:
        for r in recs:
            window = r.get("measurement_window_days") or measurement_windows.get(r.get("action_type"))
            rec_id = r.get("_recommendation_id")
            impact = pinned.get(rec_id) or r.get("impact")
            # plan/32: keep the generator's measurement facts (template, page
            # count, sample URLs) attached — the grouped auto-fix expands off
            # them, and the model's rewritten evidence would otherwise drop them.
            _prior = stored.get(rec_id) or (None, None)
            evidence = _merge_generator_evidence(r.get("evidence"), _prior[1])
            cur.execute(
                """
                UPDATE recommendations SET
                    diagnosis = %s,
                    evidence_json = %s,
                    work_required_json = %s,
                    impact = %s,
                    confidence = %s,
                    effort = %s,
                    owner = %s,
                    measurement_metric = %s,
                    measurement_window_days = %s,
                    measurement_due_at = now() + (%s || ' days')::interval,
                    status = 'proposed'
                WHERE recommendation_id = %s AND site_id = %s
                """,
                (
                    r.get("diagnosis"),
                    json.dumps(evidence if evidence is not None else [], default=str),
                    json.dumps(r.get("work_required") or [], default=str),
                    impact, r.get("confidence"), r.get("effort"), r.get("owner"),
                    r.get("measurement_metric"),
                    window,
                    str(window),
                    rec_id, site_id,
                ),
            )
        for rej in rejects:
            candidate_uuid = None
            try:
                candidate_uuid = str(uuid.UUID(str(rej.get("candidate_id"))))
            except (ValueError, AttributeError, TypeError):
                candidate_uuid = None
            rec_id = rej.get("_recommendation_id") or candidate_uuid
            if rec_id:
                cur.execute(
                    """
                    UPDATE recommendations SET
                        status = 'rejected',
                        rejection_reason = %s,
                        rejected_at = now()
                    WHERE recommendation_id = %s AND site_id = %s
                    """,
                    (rej.get("reason"), rec_id, site_id),
                )
            cur.execute(
                """
                INSERT INTO rejection_log
                    (site_id, run_id, candidate_id, generator, primary_keyword, reason, rejected_by)
                VALUES (%s, %s, %s, %s, %s, %s, 'agent')
                """,
                (site_id, None,
                 candidate_uuid, rej.get("generator"),
                 rej.get("primary_keyword"), rej.get("reason")),
            )
    return {"approved": len(recs), "rejected": len(rejects)}


def run_agent(site_id, conn=None, client=None):
    """Full agent pass for one site; returns the run summary."""
    env_loader.load_env_file()
    own_conn = conn is None
    if own_conn:
        conn = database.get_connection()
    try:
        client = client or FallbackClient()
        payload = build_agent_input(conn, site_id)
        candidate_ids = [c["candidate_id"] for c in payload["candidates"]]
        if not candidate_ids:
            return {"status": "no_candidates", "total_evaluated": 0}

        system_prompt = build_system_prompt(payload["candidates"])
        output = client.complete_json(system_prompt, payload)
        windows = load_measurement_windows(conn)
        recs, rejects = validate_agent_output(output, candidate_ids,
                                              valid_action_types=set(windows))

        id_to_row = {c["candidate_id"]: c for c in payload["candidates"]}
        for r in recs:
            cid = str(r.get("candidate_id") or "")
            row = id_to_row.get(cid)
            if row is None:
                # Fallback matching: action_type + url overlap when the model
                # omits or mangles candidate_id.
                for c in payload["candidates"]:
                    same_action = c.get("action_type") == r.get("action_type")
                    url_match = (
                        c.get("proposed_url") and c["proposed_url"] == r.get("proposed_url")
                    ) or (c.get("target_url") and c["target_url"] == r.get("target_url"))
                    if same_action and (url_match or (not r.get("proposed_url") and not r.get("target_url"))):
                        row = c
                        break
            if row is not None:
                r["_recommendation_id"] = row["candidate_id"]

        for rej in rejects:
            row = id_to_row.get(str(rej.get("candidate_id") or ""))
            if not row:
                for c in payload["candidates"]:
                    if c.get("candidate_id") == str(rej.get("candidate_id") or ""):
                        row = c
                        break
            if row:
                rej["generator"] = row.get("generator")
                rej["primary_keyword"] = (row.get("cluster") or {}).get("primary_keyword")
                rej["_recommendation_id"] = row["candidate_id"]
        counts = persist_agent_output(conn, site_id, recs, rejects,
                                      measurement_windows=windows)
        conn.commit()
        return {
            "status": "ok",
            "run_id": payload["run_id"],
            "agent_version": AGENT_VERSION,
            "total_evaluated": len(payload["candidates"]),
            "total_rejected": len(rejects),
            "approved": counts["approved"],
            # Which provider actually served this run, and why the primary
            # was not used. Ops needs this to tell "the model answered" from
            # "we silently degraded to the fallback and nobody noticed".
            "llm_provider": getattr(client, "last_provider", None),
            "llm_model": getattr(client, "last_model", None),
            "llm_fallback_reason": getattr(client, "fallback_reason", None),
        }
    except (OllamaConfigError, OllamaApiError, GeminiConfigError,
            GeminiApiError, RuntimeError, AgentValidationError):
        # FallbackClient raises RuntimeError when no provider can serve, so
        # that must roll back too. The generic handler below does the same.
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        if own_conn:
            conn.close()


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", help="domain (plan/31: mandatory — no default site)")
    args = parser.parse_args()
    if not args.site:
        print("error: --site is required (plan/31 removed the default-site fallback)")
        sys.exit(2)
    conn = database.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT site_id FROM site_config WHERE domain = %s", (args.site,))
            row = cur.fetchone()
        if not row:
            print(f"no site configured for domain {args.site!r}")
            sys.exit(1)
        summary = run_agent(row[0], conn)
        print(json.dumps(summary, default=str))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
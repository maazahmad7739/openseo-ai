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

from agents.ollama_client import OllamaClient, OllamaConfigError, OllamaApiError  # noqa: E402

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
        summary_start = system_prompt.find("SKILLS BY CANDIDATE TYPE:")
        summary_end = system_prompt.find("OUTPUT:", summary_start)
        if summary_start != -1 and summary_end != -1:
            replacement = (
                "SKILLS BY CANDIDATE TYPE:\n"
                "The full skill files for the candidate types in this batch are\n"
                "appended below. For those types, follow the APPENDED skill files\n"
                "(complete step-by-step rules, per-issue checks, and worked\n"
                "examples); the summary above is superseded."
            )
            system_prompt = (system_prompt[:summary_start] + replacement +
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

        cur.execute(
            "SELECT recommendation_id, generator, action_type, target_url, proposed_url, "
            "       cluster_id, diagnosis, evidence_json "
            "FROM recommendations WHERE site_id = %s AND status = 'raw' "
            "ORDER BY created_at DESC LIMIT %s",
            (site_id, prefetch_limit or 25),
        )
        rows = cur.fetchall()

        candidate_payloads = []
        for (rec_id, generator, action_type, target_url, proposed_url,
             cluster_id, diagnosis, evidence_json) in rows:
            cluster = None
            if cluster_id:
                cur.execute(
                    "SELECT cluster_id, primary_keyword, keywords, intent, search_volume, "
                    "commercial_value FROM keyword_clusters WHERE cluster_id = %s",
                    (cluster_id,),
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
            try:
                evidence = json.loads(evidence_json) if evidence_json else []
            except (TypeError, ValueError):
                evidence = []
            candidate_payloads.append({
                "candidate_id": str(rec_id),
                "generator": generator,
                "action_type": action_type,
                "target_url": target_url,
                "proposed_url": proposed_url,
                "cluster": cluster,
                "candidate_diagnosis": diagnosis,
                "evidence_summary": evidence if isinstance(evidence, (dict, list)) else [],
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
                "max_recommendations": MAX_RECOMMENDATIONS,
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
    accepted = 0
    with conn.cursor() as cur:
        for r in recs:
            window = r.get("measurement_window_days") or measurement_windows.get(r.get("action_type"))
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
                    json.dumps(r.get("evidence") or [], default=str),
                    json.dumps(r.get("work_required") or [], default=str),
                    r.get("impact"), r.get("confidence"), r.get("effort"), r.get("owner"),
                    r.get("measurement_metric"),
                    window,
                    str(window),
                    r.get("_recommendation_id"), site_id,
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
        client = client or OllamaClient()
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
        }
    except (OllamaConfigError, OllamaApiError, AgentValidationError):
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
    parser.add_argument("--site", help="domain; defaults to the first site")
    args = parser.parse_args()
    conn = database.get_connection()
    try:
        with conn.cursor() as cur:
            if args.site:
                cur.execute("SELECT site_id FROM site_config WHERE domain = %s", (args.site,))
            else:
                cur.execute("SELECT site_id FROM site_config ORDER BY created_at LIMIT 1")
            row = cur.fetchone()
        if not row:
            print("no site configured; run sync first")
            sys.exit(1)
        summary = run_agent(row[0], conn)
        print(json.dumps(summary, default=str))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
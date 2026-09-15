import os
import sys
import json
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from agents.ollama_client import OllamaClient, OllamaConfigError, parse_json_content  # noqa: E402
from agents.seo_agent import (  # noqa: E402
    validate_agent_output,
    persist_agent_output,
    load_system_prompt,
    build_agent_input,
)

import db as database  # noqa: E402


def check(name, value):
    print(f"[{'PASS' if value else 'FAIL'}] {name}")
    return value


class FakeClient:
    """Records the prompt; returns a canned plan/05-shaped output (no network)."""

    def __init__(self, output):
        self._output = output
        self.last_payload = None

    def complete_json(self, system_prompt, user_payload):
        self.last_payload = {"system_len": len(system_prompt), "user": user_payload}
        return self._output


def main():
    allok = True
    saved = {}
    for name in ("OLLAMA_API_BASE", "OLLAMA_API_KEY", "OLLAMA_MODEL"):
        saved[name] = os.environ.pop(name, None)

    try:
        print("== credential loudness ==")
        refused = False
        try:
            OllamaClient(config={})
        except OllamaConfigError as exc:
            refused = "OLLAMA_API_BASE" in str(exc) and "OLLAMA_API_KEY" in str(exc)
        allok &= check("missing credentials -> loud OllamaConfigError naming env vars", refused)

        os.environ["OLLAMA_API_BASE"] = "https://ollama.example/v1"
        os.environ["OLLAMA_API_KEY"] = "test-key-never-logged"
        client = OllamaClient(config={})
        allok &= check("client constructs with env credentials", client.model is not None)

        print("\n== json content parsing ==")
        allok &= check("plain json parses", parse_json_content('{"a": 1}') == {"a": 1})
        allok &= check("fenced json parses", parse_json_content('```json\n{"a": 1}\n```') == {"a": 1})
        bad = False
        try:
            parse_json_content("not json at all")
        except Exception:
            bad = True
        allok &= check("non-json -> typed error", bad)

        print("\n== output schema validation ==")
        valid = {
            "rejection_log": [
                {"candidate_id": "c2", "reason": "volume below threshold", "skill_applied": "validate_opportunity"}
            ],
            "recommendations": [
                {"action_type": "create_page", "target_url": "", "proposed_url": "/collections/x",
                 "query_cluster": ["x"], "diagnosis": "gap", "evidence": [{"source": "GSC", "finding": "f"}],
                 "work_required": [{"owner": "SEO", "task": "t", "acceptance_criteria": "a"}],
                 "impact": "high", "confidence": "medium", "effort": "days", "owner": "SEO",
                 "measurement_metric": "sessions", "measurement_window_days": 49}
            ],
        }
        recs, rejects = validate_agent_output(valid, {"c2"})
        allok &= check("valid plan/05 output passes", len(recs) == 1 and len(rejects) == 1)

        for mutate, label in (
            (lambda o: o["recommendations"][0].pop("diagnosis"), "missing required key"),
            (lambda o: o["recommendations"][0].update(impact="huge"), "invalid impact enum"),
            (lambda o: o.update(recommendations=[o["recommendations"][0]] * 6), "more than 5 recs"),
            (lambda o: o["rejection_log"][0].update(candidate_id="unknown"), "rejection for unknown candidate"),
        ):
            broken = json.loads(json.dumps(valid))
            mutate(broken)
            bad = False
            try:
                validate_agent_output(broken, {"c2"})
            except Exception:
                bad = True
            allok &= check(f"schema rejects: {label}", bad)

        print("\n== system prompt ==")
        prompt = load_system_prompt()
        allok &= check("prompt carries plan/05 rules",
                       "Never invent traffic" in prompt and "maximum of 5" in prompt)

        print("\n== persistence (fake client, real DB) ==")
        conn = database.get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT site_id FROM site_config ORDER BY created_at LIMIT 1")
                site_row = cur.fetchone()
            if site_row:
                site_id = site_row[0]
                with conn.cursor() as cur:
                    # plan/16: build_agent_input reads status='raw' (generator-inserted
                    # candidates awaiting agent promotion). After the migration, raw rows
                    # may have been cleaned; only run if raw candidates exist.
                    cur.execute(
                        "SELECT count(*) FROM recommendations WHERE site_id = %s AND status = 'raw'",
                        (site_id,))
                    raw_before = cur.fetchone()[0]
                if raw_before:
                    payload = build_agent_input(conn, site_id)
                    allok &= check("agent input built from DB (reads raw candidates)",
                                   payload["site"]["domain"] and isinstance(payload["candidates"], list))
                    recs[0]["_recommendation_id"] = payload["candidates"][0]["candidate_id"]
                    counts = persist_agent_output(conn, site_id, recs, rejects)
                    allok &= check("approved recs returned from persist", counts["approved"] == 1)
                    # verify the row is now proposed (agent-enriched), not 'approved'
                    with conn.cursor() as cur:
                        cur.execute("SELECT status FROM recommendations WHERE recommendation_id=%s",
                                   (payload["candidates"][0]["candidate_id"],))
                        new_status = cur.fetchone()
                    allok &= check("persisted row promoted to proposed (raw->proposed)",
                                   new_status and new_status[0] == "proposed",
                                   f"new_status={new_status}")
                    conn.rollback()  # test only; don't persist
                else:
                    print("[SKIP] persistence (no raw rows; generation may be needed first)")
            else:
                print("[SKIP] persistence (no site; run sync first)")
        finally:
            conn.close()
    finally:
        for name, value in saved.items():
            if value is not None:
                os.environ[name] = value

    print("\nAGENT LAYER TEST RUN", "PASSED" if allok else "FAILED")
    sys.exit(0 if allok else 1)


if __name__ == "__main__":
    main()
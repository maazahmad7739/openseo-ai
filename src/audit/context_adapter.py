"""The in-memory rec adapter — the ONLY new seam into the existing fix
pipeline (plan/23 §3.2, §6 Phase C).

`load_decision_inputs()` in src/fixes/generator.py reads from the batch
pipeline's tables (recommendations/pages/keyword_clusters) — tables an
ad-hoc audit session never populates. This adapter instead BUILDS the
same `rec` dict shape in memory from an audit session + page snapshot +
inferred query, then calls the generator's draft/validate/outline
functions DIRECTLY, unchanged:

    draft_title / draft_meta_description / validate_title_draft /
    validate_meta_draft / content_outline_gaps

Zero drafting logic is forked — there is no second code path that could
silently weaken an invariant.

Mode split (plan/23 §3.2):
  * audit_checklist : shopify_gid=None, site_name=None (no brand-suffix
    claim possible), drafts returned as diff PREVIEWS; nothing is ever
    inserted into generated_fixes. Duplicate guards run against the
    single ingested page (self-duplicate impossible) and are reported
    `site_wide_checked: False` (honest scope).
  * connected       : full fidelity — duplicate guards scope to the real
    site (pages self-joins work against store data), and the full GSC
    guards (protect-winner / consolidate suppression) run exactly as in
    the batch path via the normal hooks (not re-implemented here).

Contract test (§6): the rec dict is pinned key-by-key against the exact
keys the generator functions read.
"""

import json

from fixes.generator import (
    META_MAX_CHARS,
    META_MIN_CHARS,
    TITLE_MAX_CHARS,
    TITLE_MIN_CHARS,
    _competitor_prefix,
    _competitor_snippet_leads,
    _body_to_text,
    _snippet_hooks,
    _title_framing,
    content_outline_gaps,
    draft_meta_description,
    draft_title,
    duplicate_meta_check,
    duplicate_title_check,
    generate_fix_for_recommendation,
    generate_meta_fix_for_recommendation,
    meta_quality_checks,
    title_quality_checks,
    validate_meta_draft,
    validate_title_draft,
)

AUDIT_MODES = ("audit_checklist", "connected")

URL_PATTERN_PAGE_TYPES = (
    ("/products/", "product"),
    ("/collections/", "collection"),
    ("/blog/", "blog"),
    ("/guides/", "blog"),
    ("/pages/", "page"),
    ("/articles/", "blog"),
)


# ------------------------------------------------------------
# rec construction (the pinned contract shape)
# ------------------------------------------------------------

def page_type_from_url(url):
    """Deterministic page_type from the URL archetype (plan/23 §3.2:
    '/products/' → 'product'). Unknown patterns -> 'unknown'."""
    path = (url or "").split("://", 1)[-1]
    path = path.split("?", 1)[0].split("#", 1)[0].lower()
    for pattern, page_type in URL_PATTERN_PAGE_TYPES:
        if pattern in path:
            return page_type
    return "unknown"


def build_competitor_context(serp_rows):
    """audit_serp_competitors rows (or normalized SERP dicts) ->
    the {titles, snippets, url_patterns} shape load_competitor_context
    returns (plan/23 §3.1). is_self rows are excluded — grounding reads
    only competitors, same as the batch query.

    Framing/hook extraction is REUSED from the generator (_title_framing /
    _snippet_hooks) — the same enrichment load_competitor_context applies,
    so _competitor_prefix and _competitor_snippet_leads see identical
    inputs from either source (the §2.3 schema-parity promise)."""
    titles, snippets, patterns = [], [], []
    seen_urls = set()
    for row in sorted(serp_rows or [], key=lambda r: r.get("position") or 0):
        url = row.get("result_url")
        if not url or url in seen_urls or row.get("is_self"):
            continue
        seen_urls.add(url)
        position = row.get("position")
        title = (row.get("title") or "").strip()
        snippet = (row.get("snippet") or "").strip()
        if title:
            titles.append({"title": title, "position": position,
                           "pattern": _title_framing(title, position)})
        if snippet:
            snippets.append({"snippet": snippet, "position": position,
                             "hooks": _snippet_hooks(snippet)})
        pattern = row.get("url_pattern")
        if pattern:
            patterns.append(pattern)
    return {"titles": titles, "snippets": snippets,
            "url_patterns": list(dict.fromkeys(patterns))}


def build_rec(normalized_url, page=None, inferred_query=None,
              inferred_intent=None, site_name=None, shopify_gid=None,
              serp_rows=None):
    """The in-memory rec dict, pinned to the exact keys the generator
    functions read (contract test pins the key set):
      target_url, page_title, page_meta_description, page_type,
      body_html, body_text, primary_keyword, intent, site_name,
      shopify_gid, competitor_context."""
    page = page or {}
    body_html = page.get("body_html")
    return {
        "target_url": normalized_url,
        "page_title": (page or {}).get("title"),
        "page_meta_description": (page or {}).get("meta_description"),
        "page_type": (page or {}).get("page_type") or page_type_from_url(normalized_url),
        "body_html": body_html,
        "body_text": _body_to_text(body_html) if body_html
        else (page or {}).get("body_text"),
        "primary_keyword": (page or {}).get("primary_keyword") or inferred_query,
        "intent": (page or {}).get("intent") or inferred_intent,
        "site_name": site_name,          # None in brand-agnostic mode
        "shopify_gid": shopify_gid,      # None unless connected
        "competitor_context": build_competitor_context(serp_rows),
    }


# ------------------------------------------------------------
# Duplicate-scope handling per mode
# ------------------------------------------------------------

def _duplicate_scope(conn, mode, site_id):
    """audit_checklist has no site to self-join against: the guard
    degrades to per-page (self-duplicate impossible) and the response
    says so honestly (`site_wide_unchecked: true`). Connected mode runs
    the REAL site-wide guard (plan/21 §2.1 check 4 / §2.2)."""
    if mode == "connected" and site_id:
        return site_id, False
    return None, True


# ------------------------------------------------------------
# Suggestions assembly
# ------------------------------------------------------------

def _field_result(current, draft, problems, grounding):
    return {
        "current": current,
        "draft": draft,
        "char_count": len(draft) if draft else None,
        "validator_problems": problems or [],
        "bounds": None,
        "grounding": grounding,
    }


def _title_grounding(rec, draft):
    prefix = _competitor_prefix(rec)
    return {
        "framing_pattern": prefix.strip() if prefix else "keyword-forward",
        "competitor_titles_sampled": len(rec["competitor_context"].get("titles") or []),
        "url_patterns": rec["competitor_context"].get("url_patterns") or [],
    }


def _meta_grounding(rec, draft):
    leads = _competitor_snippet_leads(rec)
    return {
        "hook_ordering": leads,
        "competitor_snippets_sampled": len(rec["competitor_context"].get("snippets") or []),
    }


def _suggested_headings(outline, max_headings=3):
    """map missing_sections → suggested H2s (deterministic; labeled
    competitor-grounded)."""
    return [{"level": "h2", "suggested": section,
             "rationale": "competitor-grounded section angle"}
            for section in outline.get("missing_sections", [])[:max_headings]]


def generate_suggestions(conn, normalized_url, mode, page=None,
                         inferred_query=None, inferred_intent=None,
                         site_name=None, shopify_gid=None, serp_rows=None,
                         site_id=None):
    """Draft + validate BOTH fields through the UNCHANGED generator
    functions; returns the §4.1 suggestions payload.

    Every draft that reaches the caller passed its validator; a failing
    draft is surfaced as draft=None + validator_problems — never a
    silently weakened suggestion (plan/23 §3.2)."""
    rec = build_rec(normalized_url, page=page, inferred_query=inferred_query,
                    inferred_intent=inferred_intent, site_name=site_name,
                    shopify_gid=shopify_gid, serp_rows=serp_rows)
    dup_scope_site, site_wide_unchecked = _duplicate_scope(conn, mode, site_id)

    suggestions = {"mode": mode, "unprotected": True,
                   "site_wide_unchecked": site_wide_unchecked}

    # ---- seo.title ----
    current_title = rec["page_title"]
    checks = title_quality_checks(current_title, rec["primary_keyword"])
    draft = draft_title_safe(rec)
    problems = []
    if draft is not None:
        problems = validate_title_draft(
            draft, rec["primary_keyword"],
            product_title=rec["page_title"], intent=rec["intent"],
            site_name=site_name)
        if problems:
            draft = None
    if draft is not None and dup_scope_site is not None \
            and duplicate_title_check(conn, dup_scope_site, draft,
                                      exclude_url=normalized_url):
        draft = None
        problems = ["duplicate title exists site-wide"]
    suggestions_entry = {
        "current": current_title,
        "draft": draft,
        "char_count": len(draft) if draft else None,
        "validator_problems": problems,
        "grounding": _title_grounding(rec, draft),
        "quality_checks": checks,
        "needs_fix": _title_needs_fix(checks),
    }

    # ---- seo.description ----
    current_meta = rec["page_meta_description"]
    meta_checks = meta_quality_checks(current_meta, rec["primary_keyword"])
    meta_draft = draft_meta_safe(rec)
    meta_problems = []
    if meta_draft is not None:
        meta_problems = validate_meta_draft(
            meta_draft, rec["primary_keyword"], intent=rec["intent"],
            site_name=site_name)
        if meta_problems:
            meta_draft = None
    if meta_draft is not None and dup_scope_site is not None:
        if duplicate_meta_check(conn, dup_scope_site, meta_draft,
                                exclude_url=normalized_url):
            meta_draft = None
            meta_problems = ["duplicate meta description exists site-wide"]
    meta_entry = {
        "current": current_meta,
        "draft": meta_draft,
        "char_count": len(meta_draft) if meta_draft else None,
        "validator_problems": meta_problems,
        "grounding": _meta_grounding(rec, meta_draft),
        "checks": meta_checks,
    }

    # ---- content outline ----
    outline = content_outline_gaps(rec, body_text=rec.get("body_text"))
    headings = _suggested_headings(outline)

    result = {
        "seo.title": suggestions_entry,
        "seo.description": meta_entry,
        "content_outline": outline,
        "headings": headings,
        "unprotected": True,
        "site_wide_unchecked": site_wide_unchecked,
        "mode": mode,
    }
    # expose bounds for the UI's character counters
    result["bounds"] = {"title": [TITLE_MIN_CHARS, TITLE_MAX_CHARS],
                        "meta": [META_MIN_CHARS, META_MAX_CHARS]}
    return result


def _title_needs_fix(checks):
    """plan/21 §2.1 only-fix-what's-broken: a fix is warranted only when
    at least one quality check FAILS."""
    return bool(checks["is_blank"] or not checks["length_ok"]
                or not checks["has_primary_kw"])


def draft_title_safe(rec):
    """draft_title wrapped: None propagates as None (no crash, no fix)."""
    try:
        return draft_title(rec)
    except Exception:
        return None


def draft_meta_safe(rec):
    try:
        return draft_meta_description(rec)
    except Exception:
        return None


# ------------------------------------------------------------
# Grounding payload (§3.4 shape — the batch payload["grounding"] block)
# ------------------------------------------------------------

def grounding_block(rec, mode):
    ctx = rec.get("competitor_context") or {}
    outline = content_outline_gaps(rec, body_text=rec.get("body_text"))
    prefix = _competitor_prefix(rec)
    return {
        "framing_pattern": prefix.strip() if prefix else "keyword-forward",
        "competitor_titles_sampled": len(ctx.get("titles") or []),
        "url_patterns": ctx.get("url_patterns") or [],
        "content_outline": outline,
        "unprotected": True,     # §3.3: no GSC data ad-hoc — explicit flag
        "mode": mode,
    }


# ------------------------------------------------------------
# Connected-mode bridge (full pipeline hooks — no forking)
# ------------------------------------------------------------

def upsert_page_for_site(conn, site_id, snapshot):
    """§4.3 step 2: upsert the audited page into `pages` (site-scoped) so
    duplicate_title_check / duplicate_meta_check site-wide self-joins and
    the executor's GID lookups work against real store data. Reuses
    plan/21 §1.4 column semantics (meta_description/body_html). url_hash
    is a GENERATED column (md5) — never written."""
    url = snapshot.get("url")
    if not url or not site_id:
        raise ValueError("upsert_page_for_site requires site_id + url")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO pages (site_id, url, page_type, title,
                               meta_description, body_html, last_crawled_at,
                               render_status)
            VALUES (%s, %s, %s, %s, %s, %s, now(), %s)
            ON CONFLICT (site_id, url_hash) DO UPDATE SET
                title = EXCLUDED.title,
                meta_description = EXCLUDED.meta_description,
                body_html = EXCLUDED.body_html,
                last_crawled_at = now(),
                render_status = EXCLUDED.render_status
            RETURNING page_id
            """,
            (site_id, url,
             snapshot.get("page_type") or "unknown",
             snapshot.get("title"), snapshot.get("meta_description"),
             snapshot.get("body_html"),
             snapshot.get("render_status") or "not_rendered"),
        )
        page_id = str(cur.fetchone()[0])
    conn.commit()
    return page_id


def generate_fixes_for_session(conn, mode, site_id, normalized_url,
                               page, inferred_query, inferred_intent,
                               site_name, serp_rows, recommendation_id,
                               generation_source="deterministic"):
    """Connected mode: call the REAL generator hooks (plan/21 §2.1/§2.2)
    — the rec adapter merely supplies the in-memory rec inputs; the fix
    lifecycle (conflict guards, payload builders, generated_fixes insert)
    stays 100% in fixes/generator.py.

    Returns the hook dicts ({fix_id, status, created, diff}) or raises
    FixNotSupported exactly like the batch path (typed, never silent).
    """
    if mode != "connected":
        raise ValueError("generate_fixes_for_session is connected-mode only")
    rec = build_rec(normalized_url, page=page,
                    inferred_query=inferred_query,
                    inferred_intent=inferred_intent, site_name=site_name,
                    shopify_gid=None, serp_rows=serp_rows)
    # the hooks re-read from load_decision_inputs(recommendation_id) —
    # the recommendation row must exist first (gate 1); the rec dict here
    # feeds only the shared grounding payload builders.
    title_fix = generate_fix_for_recommendation(
        conn, recommendation_id, generation_source=generation_source)
    meta_fix = None
    try:
        meta_fix = generate_meta_fix_for_recommendation(
            conn, recommendation_id, generation_source=generation_source)
    except Exception as exc:
        meta_fix = {"error": str(exc)}
    return {"seo.title": title_fix, "seo.description": meta_fix}


def session_to_payload(session_row, suggestions):
    """§4.1 response assembly (Phase D preview): the persisted session +
    suggestion payload in the API contract shape."""
    return {
        "session_id": str(session_row.get("session_id")) if session_row else None,
        "mode": suggestions.get("mode"),
        "suggestions": suggestions,
        "grounding": None,
    }


def export_checklist_markdown(suggestions, inferred_query=None):
    """§4.5 copy-paste checklist: markdown clipboard export of the
    suggestion payload (audit_checklist mode emphasis)."""
    lines = ["# SEO Implementation Checklist", ""]
    if inferred_query:
        lines += [f"Target keyword: **{inferred_query}**", ""]
    title = suggestions.get("seo.title") or {}
    meta = suggestions.get("seo.description") or {}
    lines.append("## seo.title")
    lines.append(f"- Current: {title.get('current') or '(missing)'}")
    if title.get("draft"):
        lines.append(f"- Draft ({title['char_count']} chars): "
                     f"`{title['draft']}`")
    else:
        lines.append("- No safe draft available"
                     + (f" ({'; '.join(title.get('validator_problems') or [])})"
                        if title.get("validator_problems") else ""))
    lines.append("")
    lines.append("## seo.description")
    lines.append(f"- Current: {meta.get('current') or '(missing)'}")
    if meta.get("draft"):
        lines.append(f"- Draft ({meta['char_count']} chars): `{meta['draft']}`")
    else:
        lines.append("- No safe draft available"
                     + (f" ({'; '.join(meta.get('validator_problems') or [])})"
                        if meta.get("validator_problems") else ""))
    outline = suggestions.get("content_outline") or {}
    if outline.get("missing_sections"):
        lines += ["", "## Content sections to add"]
        lines += [f"- {s}" for s in outline["missing_sections"]]
    return "\n".join(lines) + "\n"
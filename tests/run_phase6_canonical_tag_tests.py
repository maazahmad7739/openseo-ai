# -*- coding: utf-8 -*-
"""Tests for plan/28 canonical-tag auto-fix (offline transforms + adapters).
python tests/run_phase6_canonical_tag_tests.py"""
import os
import sys
import json

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

FAILURES = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}"
          + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)
    return bool(cond)


HTML = (
    "<!DOCTYPE html>\n<html>\n<head>\n"
    "    <title>Page</title>\n"
    '    <meta name="description" content="Desc">\n'
    "</head>\n<body>hi</body>\n</html>"
)
HTML_WITH_CANONICAL = (
    "<!DOCTYPE html>\n<html>\n<head>\n"
    "    <title>Page</title>\n"
    '    <link rel="canonical" href="https://x.example.com/">\n'
    "</head>\n<body>hi</body>\n</html>"
)


def main():
    from fixes.adapters import apply_netlify_html_edit, get_adapter, ADAPTERS

    print("== HTML transforms ==")
    # inject when missing
    new, summary = apply_netlify_html_edit(
        HTML, "canonical_tag",
        {"canonical_url": "https://www.example.com/contact.html"})
    check("canonical injected",
          new is not None
          and '<link rel="canonical" href="https://www.example.com/contact.html">' in new
          and new.index("canonical") < new.index("</head>"),
          str(summary))
    check("summary reports canonical_link",
          isinstance(summary, dict) and "canonical_link" in (summary.get("fields") or []),
          str(summary))
    # no-op loop guard: page with an existing canonical refuses
    dup, sdup = apply_netlify_html_edit(
        HTML_WITH_CANONICAL, "canonical_tag",
        {"canonical_url": "https://x.example.com/other.html"})
    check("existing canonical -> already_has_canonical refusal",
          dup is None and sdup == "already_has_canonical", str(sdup))
    # non-https href refuses
    bad, sbad = apply_netlify_html_edit(
        HTML, "canonical_tag", {"canonical_url": "http://x.example.com/"})
    check("non-https href refused",
          bad is None and sbad == "canonical_url_not_https", str(sbad))
    # self-referencing (same URL) — the normal case
    same, ssame = apply_netlify_html_edit(
        HTML, "canonical_tag", {"canonical_url": "https://www.example.com/"})
    check("self-referencing canonical injected",
          same is not None and 'href="https://www.example.com/"' in same,
          str(ssame))

    print("\n== adapter registry + payload validation ==")
    adapter = get_adapter("netlify.canonical_tag")
    check("canonical_tag adapter registered",
          bool(adapter) and callable(adapter.get("execute")), "")
    # payload validation
    from fixes.adapters import _netlify_payload_for
    try:
        _netlify_payload_for(
            {"mutation": "netlify.gitPr",
             "variables": {"page_url": "https://x.example.com/",
                           "file_path": "index.html",
                           "action": "canonical_tag",
                           "canonical_url": "ftp://nope"},
             "surface": "netlify"},
            "netlify.canonical_tag")
        check("non-https canonical_url -> payload_invalid", False)
    except Exception as exc:
        check("non-https canonical_url -> payload_invalid",
              "https" in str(exc) or "canonical_url" in str(exc), str(exc))
    ok_vars = _netlify_payload_for(
        {"mutation": "netlify.gitPr",
         "variables": {"page_url": "https://x.example.com/",
                       "file_path": "index.html",
                       "action": "canonical_tag",
                       "canonical_url": "https://x.example.com/"},
         "surface": "netlify"},
        "netlify.canonical_tag")
    check("valid canonical payload passes",
          ok_vars.get("canonical_url") == "https://x.example.com/", "")

    print("\n== classifier ==")
    from api.routes.queue import (_sub_type_for_task,
                                  _classify_execution_type)
    task = ("Add a self-referencing canonical tag "
            "(<link rel=\"canonical\" href=…>) in <head>")
    check("canonical task routes to netlify.canonical_tag",
          _sub_type_for_task(task.lower(), "netlify") == "netlify.canonical_tag",
          _sub_type_for_task(task.lower(), "netlify"))
    check("canonical task classified automated",
          _classify_execution_type(task, "netlify") == "automated", "")
    # structured still wins the specific-match race
    check("structured task still routes to structured_data",
          _sub_type_for_task("add structured data json-ld", "netlify")
          == "netlify.structured_data", "")
    check("title task still routes to title_meta_rewrite",
          _sub_type_for_task("rewrite the page <title>", "netlify")
          == "netlify.title_meta_rewrite", "")

    print()
    if FAILURES:
        print(f"FAILED: {FAILURES}")
        sys.exit(1)
    print("ALL PHASE 6 CANONICAL-TAG TESTS PASSED")


if __name__ == "__main__":
    main()
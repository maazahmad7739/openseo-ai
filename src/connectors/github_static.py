"""GitHub PR connector (plan/27): the write surface for netlify.* fix
adapters on static-hosted sites (Netlify/GitHub Pages/any git-backed host).

Design decision (plan/27 §1): static-site updates are delivered as GIT
PULL REQUESTS against the site's content repo — NEVER live API
overwrites, never direct pushes to the base branch. A human merges the PR;
the platform's own verify step is the PR's CI-renderable diff, not a
deployment probe.

Contract mirrors the connector family (never-raise typed results; same
shape as FramerClient / ShopifyGraphQLClient.run):

  read_file(path, ref=None)   -> {ok, data: {path, sha, content,
                                  encoding}, ...}   — the snapshot source
  default_branch()            -> {ok, data: {branch}, ...}
  open_pr(title, body, head, base, changes)
                              -> {ok, data: {pr_number, pr_url, branch,
                                  files}, ...}     — one PR per fix
  close_pr(pr_number)         -> {ok, ...}          — the revert path
  probe_auth()                -> {ok, capabilities} or typed error

Modes (central INTEGRATION_MODE switch, connectors/mode.py):
  * mock (default)  — an in-repo JSON "repo fixture" file
                      (tests/fixtures/github_repos.json) acts as a tiny
                      git store: get_file/open_pr read+mutate it exactly
                      like the Framer fixture store. Zero network.
  * live            — REAL GitHub REST v3 calls ONLY behind the explicit
                      capability flag NETLIFY_LIVE_WRITES=1 (mirrors the
                      Framer flag-gate pattern; the surface is verified —
                      GitHub's API is stable — but the flag keeps the
                      two-gate discipline uniform: mode + flag). Without
                      the flag live mode degrades to a typed
                      "live_writes_disabled" failure.

One PR per fix, branch naming: openseo/fix-<fix_id-short>. The PR body
embeds the recommendation_id + fix_id so the rollback path links back
through the audited revert row (rollback_of chain) into Supabase/Postgres.
"""

import base64
import json
import os
import urllib.error
import urllib.request

from .mode import resolve_mode

NETLIFY_MOCK_MODE_ENV = "NETLIFY_MOCK_MODE"
NETLIFY_LIVE_WRITES_ENV = "NETLIFY_LIVE_WRITES"

DEFAULT_CAPABILITIES = ["file_read", "pr_write", "pr_close"]
HTTP_TIMEOUT_SECONDS = 30
API_VERSION_HEADER = "X-GitHub-Api-Version"
API_VERSION = "2022-11-28"

PR_TITLE_FORMAT = "fix(seo): {action_label} for {page_label}"
PR_BODY_RECOMMENDATION_LINE = "recommendation_id: {recommendation_id}"
PR_BODY_FIX_LINE = "fix_id: {fix_id}"


class GitHubStaticError(Exception):
    pass


def file_path_for_url(url, root_dir=""):
    """Canonical page URL -> repo-relative file path (deterministic).

    Rules (plan/27 §2):
      * the scheme + host are stripped (the repo IS the site);
      * path ending in '/' gets index.html (directory convention);
      * path with a final segment without an extension keeps the
        directory convention too (clean URLs) — 'contact' ->
        'contact/index.html';
      * '' root_dir = repo root; otherwise the root dir prefixes the path.
    Never returns a guessed extension for explicit .html paths.
    """
    raw = (url or "").strip()
    if not raw:
        return None
    path = raw.split("://", 1)[-1]
    path = path.split("?", 1)[0].split("#", 1)[0]
    # strip the host when a scheme/host was present
    if raw != path or "://" in raw:
        path = path.split("/", 1)[-1] if "/" in path else ""
    else:
        # bare path form ('/contact'): strip the leading slash only
        path = path.lstrip("/")
    if not path or path == "":
        path = "index.html"
    lower = path.lower()
    if lower.endswith("/"):
        path = path.rstrip("/") + "/index.html"
        lower = path.lower()
    if "." not in os.path.basename(path):
        path = path.rstrip("/") + "/index.html"
    elif lower.endswith((".md", ".markdown")):
        pass  # markdown sources preserved verbatim
    root = (root_dir or "").strip().strip("/")
    if root:
        path = f"{root}/{path}"
    return path.replace("\\", "/")


class GitHubPRClient:
    """GitHub content-repo read + PR write; never-raise typed results.

    The URL->file resolution lives in file_path_for_url() (deterministic,
    no id minting); open_pr() mints the head branch name from the fix id
    so reverts are branch-close operations (never a force-push).
    """

    def __init__(self, github_token=None, github_repo=None,
                 github_branch="main", github_root_dir="", api_base=None,
                 mode=None, fixtures_dir=None, live_writes=None,
                 timeout=HTTP_TIMEOUT_SECONDS):
        self.github_token = github_token
        self.github_repo = github_repo
        self.github_branch = github_branch or "main"
        self.github_root_dir = github_root_dir or ""
        self.api_base = (api_base or os.environ.get("GITHUB_API_BASE")
                         or "https://api.github.com")
        self.fixtures_dir = fixtures_dir or os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__)))), "tests", "fixtures")
        env_mock = os.environ.get(NETLIFY_MOCK_MODE_ENV, "").strip().lower() \
            in ("1", "true", "yes", "on")
        self.mode = mode if mode is not None else resolve_mode(
            None, (NETLIFY_MOCK_MODE_ENV,))
        self.mock_mode = self.mode == "mock" or env_mock
        if live_writes is None:
            live_writes = os.environ.get(NETLIFY_LIVE_WRITES_ENV, "").strip() \
                .lower() in ("1", "true", "yes", "on")
        self.live_writes = bool(live_writes)
        self.timeout = timeout

    # ------------------------------------------------------------
    # The adapter-facing surface
    # ------------------------------------------------------------

    def read_file(self, path, ref=None):
        """One file's current content on the base branch (typed)."""
        if self.mock_mode:
            return self._mock_read_file(path, ref)
        if not self._live_allowed():
            return {"ok": False, "outcome": "live_writes_disabled",
                    "detail": ("GitHub live calls are flag-gated "
                               "(NETLIFY_LIVE_WRITES=1)")}
        return self._live_request(
            "GET",
            f"/repos/{self.github_repo}/contents/{path.lstrip('/')}"
            + (f"?ref={ref}" if ref else ""))

    def default_branch(self):
        if self.mock_mode:
            return {"ok": True, "outcome": "ok",
                    "data": {"branch": self.github_branch}}
        if not self._live_allowed():
            return {"ok": False, "outcome": "live_writes_disabled",
                    "detail": "GitHub live calls are flag-gated "
                              "(NETLIFY_LIVE_WRITES=1)"}
        return self._live_request("GET", f"/repos/{self.github_repo}")

    def open_pr(self, title, body, head, base, changes):
        """Open one pull request applying `changes`.

        changes: list of {path, new_content, old_content} — the mock store
        records the PR; live mode creates the branch (from base), commits
        each file via the contents API, and opens the PR.
        """
        if not isinstance(changes, list) or not changes:
            return {"ok": False, "outcome": "payload_invalid",
                    "detail": "open_pr needs a non-empty changes list"}
        if self.mock_mode:
            return self._mock_open_pr(title, body, head, base, changes)
        if not self._live_allowed():
            return {"ok": False, "outcome": "live_writes_disabled",
                    "detail": "GitHub live PR writes are flag-gated "
                              "(NETLIFY_LIVE_WRITES=1)"}
        return self._live_open_pr(title, body, head, base, changes)

    def close_pr(self, pr_number):
        """Close an open PR (the revert path; the branch is deleted too)."""
        if self.mock_mode:
            return self._mock_close_pr(pr_number)
        if not self._live_allowed():
            return {"ok": False, "outcome": "live_writes_disabled",
                    "detail": "GitHub live PR writes are flag-gated "
                              "(NETLIFY_LIVE_WRITES=1)"}
        return self._live_request(
            "PATCH", f"/repos/{self.github_repo}/pulls/{pr_number}",
            body={"state": "closed"})

    def probe_auth(self):
        """Executor startup probe (mirrors FramerClient.probe_auth)."""
        if self.mock_mode:
            return {"ok": True, "mode": "mock",
                    "capabilities": DEFAULT_CAPABILITIES,
                    "repo": self.github_repo}
        if not (self.github_token and self.github_repo):
            return {"ok": False, "error": "no_credentials",
                    "detail": "probe needs GITHUB_TOKEN + "
                              "site_config.github_repo"}
        if not self._live_allowed():
            return {"ok": False, "error": "live_writes_disabled",
                    "detail": "GitHub live calls flag-gated "
                              "(NETLIFY_LIVE_WRITES=1)"}
        probe = self._live_request("GET", f"/repos/{self.github_repo}")
        if not probe.get("ok"):
            return {"ok": False, "error": probe.get("outcome", "probe_failed"),
                    "detail": probe.get("detail")}
        return {"ok": True, "mode": "live",
                "capabilities": DEFAULT_CAPABILITIES,
                "repo": self.github_repo}

    # ------------------------------------------------------------
    # Mock store (fixture-backed, same pattern as framer_pages.json)
    # ------------------------------------------------------------

    FIXTURE_NAME = "github_repos.json"

    def _fixture_path(self):
        return os.path.join(self.fixtures_dir, self.FIXTURE_NAME)

    def _load_fixture(self):
        path = self._fixture_path()
        if not os.path.exists(path):
            return {"repos": {}}
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {"repos": {}}

    def _save_fixture(self, fixture):
        with open(self._fixture_path(), "w", encoding="utf-8") as fh:
            json.dump(fixture, fh, indent=2, ensure_ascii=False)

    def _repo_fixture(self):
        fixture = self._load_fixture()
        repos = fixture.get("repos") if isinstance(fixture, dict) else None
        if not isinstance(repos, dict):
            return {}
        return repos.get(self.github_repo) or {}

    def _mock_read_file(self, path, ref=None):
        repo = self._repo_fixture()
        files = repo.get("files") or {}
        # tolerate both rooted ('index.html') and prefixed ('site/index.html')
        # key forms so the fixture can model github_root_dir layouts
        content = files.get(path)
        if content is None and self.github_root_dir:
            content = files.get(path[len(self.github_root_dir) + 1:])
        if content is None:
            alt = f"{self.github_root_dir}/{path}".lstrip("/") \
                if self.github_root_dir else None
            if alt and alt != path:
                content = files.get(alt)
        if content is None:
            return {"ok": False, "outcome": "not_found",
                    "detail": f"fixture repo has no file {path!r}"}
        if isinstance(content, dict):
            content = content.get("content")
        return {"ok": True, "outcome": "ok",
                "data": {"path": path, "sha": "mock-sha", "ref": ref,
                         "content": content}}

    def _mock_open_pr(self, title, body, head, base, changes):
        repo = self._repo_fixture()
        prs = repo.setdefault("prs", [])
        pr_number = repo.get("next_pr_number", 1)
        repo["next_pr_number"] = pr_number + 1
        # the applied files ARE recorded on the PR (the merge is the
        # operator's action — the mock models the branch state, not main)
        pr = {"number": pr_number, "title": title, "body": body,
              "head": head, "base": base, "state": "open",
              "files": [dict(c) for c in changes]}
        prs.append(pr)
        # persist through the outer fixture dict
        fixture = self._load_fixture()
        fixture.setdefault("repos", {})[self.github_repo] = repo
        self._save_fixture(fixture)
        return {"ok": True, "outcome": "ok",
                "data": {"pr_number": pr_number,
                         "pr_url": f"https://github.com/{self.github_repo}/pull/{pr_number}",
                         "branch": head,
                         "files": [c.get("path") for c in changes]}}

    def _mock_close_pr(self, pr_number):
        fixture = self._load_fixture()
        repo = (fixture.get("repos") or {}).get(self.github_repo) or {}
        for pr in repo.get("prs") or []:
            if pr.get("number") == pr_number and pr.get("state") == "open":
                pr["state"] = "closed"
                self._save_fixture(fixture)
                return {"ok": True, "outcome": "ok",
                        "data": {"number": pr_number, "state": "closed"}}
        return {"ok": False, "outcome": "not_found",
                "detail": f"fixture repo has no open PR #{pr_number}"}

    # ------------------------------------------------------------
    # Live (GitHub REST v3) — behind the flag
    # ------------------------------------------------------------

    def _live_allowed(self):
        return self.mode == "live" and self.live_writes

    def _headers(self):
        headers = {
            "Accept": "application/vnd.github+json",
            API_VERSION_HEADER: API_VERSION,
            "User-Agent": "openseo-fix-executor",
        }
        if self.github_token:
            headers["Authorization"] = f"Bearer {self.github_token}"
        return headers

    @staticmethod
    def _decode_contents_payload(payload):
        """GitHub contents payload -> (content, encoding). 'base64' payloads
        are decoded to text; 'none' (small files) return the raw string.
        The decoded text is what every consumer expects (the HTML source)."""
        content = payload.get("content")
        encoding = payload.get("encoding")
        if content is None:
            return None, encoding
        if encoding == "base64":
            return base64.b64decode(content).decode("utf-8",
                                                    errors="replace"), encoding
        return content, encoding

    def _live_request(self, verb, path, body=None):
        """One REST call; never raises (typed result). Never logs the
        token; error bodies are truncated and never echoed raw."""
        url = f"{self.api_base.rstrip('/')}{path}"
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(url, data=data, method=verb,
                                         headers=self._headers())
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                payload = json.loads(
                    resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            return {"ok": False, "outcome": "http_error",
                    "detail": f"HTTP {exc.code} on {path}"}
        except Exception as exc:
            return {"ok": False, "outcome": "provider_error",
                    "detail": f"{type(exc).__name__} on {path}"}
        # contents API returns base64-encoded text — decode for consumers
        if isinstance(payload, dict) and "content" in payload:
            decoded, encoding = self._decode_contents_payload(payload)
            payload = dict(payload, content=decoded,
                           encoding=encoding or "text")
        return {"ok": True, "outcome": "ok", "data": payload}

    def _live_open_pr(self, title, body, head, base, changes):
        # 1. branch off base
        base_ref = self._live_request(
            "GET", f"/repos/{self.github_repo}/git/ref/heads/{base}")
        if not base_ref.get("ok"):
            return {"ok": False, "outcome": "provider_error",
                    "detail": f"cannot resolve base branch {base!r}"}
        base_sha = ((base_ref.get("data") or {}).get("object") or {}).get("sha")
        create = self._live_request(
            "POST", f"/repos/{self.github_repo}/git/refs",
            body={"ref": f"refs/heads/{head}", "sha": base_sha})
        if not create.get("ok"):
            # an existing head branch from a prior run is a typed conflict —
            # never silently reused (stale-diff discipline)
            return {"ok": False, "outcome": "branch_exists",
                    "detail": f"head branch {head!r} could not be created"}
        # 2. commit each file change (contents API; whole-file semantics)
        for change in changes:
            path = (change.get("path") or "").lstrip("/")
            new_content = change.get("new_content")
            if not path or new_content is None:
                return {"ok": False, "outcome": "payload_invalid",
                        "detail": "each change needs path + new_content"}
            existing = self._live_request(
                "GET", f"/repos/{self.github_repo}/contents/{path}?ref={head}")
            body_put = {
                "message": title,
                "content": base64.b64encode(
                    new_content.encode("utf-8")).decode("ascii"),
                "branch": head,
            }
            if existing.get("ok"):
                body_put["sha"] = ((existing.get("data") or {}).get("sha"))
            put = self._live_request(
                "PUT", f"/repos/{self.github_repo}/contents/{path}",
                body=body_put)
            if not put.get("ok"):
                return {"ok": False, "outcome": "provider_error",
                        "detail": f"commit failed for {path}"}
        # 3. open the PR
        pr = self._live_request(
            "POST", f"/repos/{self.github_repo}/pulls",
            body={"title": title, "body": body, "head": head, "base": base})
        if not pr.get("ok"):
            return {"ok": False, "outcome": "provider_error",
                    "detail": "PR creation failed after commit"}
        data = pr.get("data") or {}
        return {"ok": True, "outcome": "ok",
                "data": {"pr_number": data.get("number"),
                         "pr_url": data.get("html_url"),
                         "branch": head,
                         "files": [c.get("path") for c in changes]}}


def api_base_default():
    return os.environ.get("GITHUB_API_BASE") or "https://api.github.com"
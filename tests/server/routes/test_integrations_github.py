"""Tests for the GitHub App integration routes.

Builds a minimal FastAPI app with the integration router, a header-based
auth provider, and a fake GitHub client so the connect → callback →
status → disconnect flow is exercised end-to-end without the network.
"""

from __future__ import annotations

import jwt
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse

from omnigent.errors import OmnigentError
from omnigent.server.github_app import GitHubAppConfig, GitHubTokenSet
from omnigent.server.github_store import GithubConnectionStore
from omnigent.server.routes.integrations_github import (
    _body_links_session,
    _sanitize_return_to,
    create_integrations_github_router,
)
class SecretBox:  # test double for the KMS SecretCipher: key- and context-bound
    def __init__(self, key: str) -> None:
        self._key = key

    def encrypt(self, plaintext: str, *, context) -> str:
        import base64, json
        return base64.b64encode(
            json.dumps({"k": self._key, "c": dict(context), "p": plaintext}).encode()
        ).decode("ascii")

    def decrypt(self, ciphertext: str, *, context):
        import base64, json
        try:
            d = json.loads(base64.b64decode(ciphertext.encode("ascii")))
        except ValueError:
            return None
        return d["p"] if d["k"] == self._key and d["c"] == dict(context) else None


class _DenyAllPermissions:
    """Permission store that grants nobody access (get_permission_level → None).

    Enough for :func:`check_session_access` to deny: not admin, no grant.
    """

    def is_admin(self, user_id: str) -> bool:
        return False

    def check_access(self, user_id: str, conversation_id: str, required_level: int) -> bool:
        return False


class _HeaderAuth:
    """Auth provider reading the user id from ``X-Test-User``."""

    def get_user_id(self, request: object) -> str | None:
        return getattr(request, "headers", {}).get("x-test-user")


class _FakeClient:
    """Stand-in for :class:`GitHubAppClient`."""

    def __init__(self) -> None:
        self.exchanged: list[str] = []

    async def exchange_code(self, code: str) -> GitHubTokenSet:
        self.exchanged.append(code)
        return GitHubTokenSet(
            access_token="ghu_new",
            refresh_token="ghr_new",
            expires_at=None,
            refresh_token_expires_at=None,
            scopes="repo",
        )

    async def fetch_login(self, access_token: str) -> tuple[str, int]:
        return "octocat", 42

    async def list_repos(self, access_token: str) -> tuple[list[dict[str, object]], bool]:
        return (
            [
                {
                    "full_name": "caffeinelabs/app",
                    "clone_url": "https://github.com/caffeinelabs/app.git",
                    "default_branch": "main",
                    "private": True,
                    "pushed_at": "2026-07-28T00:00:00Z",
                }
            ],
            False,
        )

    async def list_branches(self, access_token: str, full_name: str) -> list[str]:
        self.branch_calls: list[str] = getattr(self, "branch_calls", [])
        self.branch_calls.append(full_name)
        return ["main", "dev"]

    async def list_pulls(self, access_token: str, full_name: str) -> list[dict[str, object]]:
        self.pull_calls: list[str] = getattr(self, "pull_calls", [])
        self.pull_calls.append(full_name)
        # PRs 1 and 4 carry this session's Open-in-Omnigent link (stamped by the
        # MCP proxy) in their body → they belong to conv_1.
        this_session_link = f"[Open in Omnigent]({_SESSION_LINK})"
        return [
            # Caller's open PR, opened after session start, carries the link → kept.
            {
                "number": 1,
                "title": "feat",
                "html_url": f"https://github.com/{full_name}/pull/1",
                "head_ref": "feat",
                "draft": False,
                "state": "open",
                "merged": False,
                "author_login": "octocat",
                "created_at": "2026-07-29T01:00:00Z",
                "body": f"does the thing\n\n{this_session_link}",
            },
            # Caller's MERGED PR from this session → still kept (any state).
            {
                "number": 4,
                "title": "merged one",
                "html_url": f"https://github.com/{full_name}/pull/4",
                "head_ref": "merged-one",
                "draft": False,
                "state": "closed",
                "merged": True,
                "author_login": "octocat",
                "created_at": "2026-07-29T01:30:00Z",
                "body": this_session_link,
            },
            # Caller's PR, but opened BEFORE the session started → filtered out.
            {
                "number": 2,
                "title": "old",
                "html_url": f"https://github.com/{full_name}/pull/2",
                "head_ref": "old",
                "draft": False,
                "state": "closed",
                "merged": False,
                "author_login": "octocat",
                "created_at": "2026-07-28T00:00:00Z",
                "body": this_session_link,
            },
            # Someone else's PR → filtered out (author).
            {
                "number": 3,
                "title": "theirs",
                "html_url": f"https://github.com/{full_name}/pull/3",
                "head_ref": "theirs",
                "draft": False,
                "state": "open",
                "merged": False,
                "author_login": "someone-else",
                "created_at": "2026-07-29T02:00:00Z",
                "body": this_session_link,
            },
            # Caller's OWN recent PR in this repo but from ANOTHER session: its
            # body carries a DIFFERENT session's link → must be filtered out.
            # This is the leak the link check fixes.
            {
                "number": 5,
                "title": "unrelated other-session PR",
                "html_url": f"https://github.com/{full_name}/pull/5",
                "head_ref": "other",
                "draft": False,
                "state": "open",
                "merged": False,
                "author_login": "octocat",
                "created_at": "2026-07-29T01:45:00Z",
                "body": "[Open in Omnigent](https://omni.example/c/other_session)",
            },
        ]

    async def search_pulls(self, access_token: str, query: str) -> list[dict[str, object]]:
        self.search_calls: list[str] = getattr(self, "search_calls", [])
        self.search_calls.append(query)
        # Search finds PRs across ALL repos by the session link. head_ref is None
        # (search results carry no head ref). Includes a cross-repo PR the session
        # never cloned, a dup of the cloned-repo #1 (to exercise dedup), and an
        # unrelated other-session PR (to exercise the body-link filter).
        link = f"[Open in Omnigent]({_SESSION_LINK})"
        return [
            {
                "number": 9,
                "title": "opened via MCP in an un-cloned repo",
                "html_url": "https://github.com/caffeinelabs/other/pull/9",
                "head_ref": None,
                "draft": False,
                "state": "open",
                "merged": False,
                "author_login": "octocat",
                "created_at": "2026-07-29T03:00:00Z",
                "body": link,
                "repo": "caffeinelabs/other",
            },
            {
                "number": 1,  # same PR search ALSO returns; dedup vs list_pulls.
                "title": "feat",
                "html_url": "https://github.com/caffeinelabs/app/pull/1",
                "head_ref": None,
                "draft": False,
                "state": "open",
                "merged": False,
                "author_login": "octocat",
                "created_at": "2026-07-29T01:00:00Z",
                "body": link,
                "repo": "caffeinelabs/app",
            },
            {
                "number": 8,
                "title": "another session's PR",
                "html_url": "https://github.com/caffeinelabs/other/pull/8",
                "head_ref": None,
                "draft": False,
                "state": "open",
                "merged": False,
                "author_login": "octocat",
                "created_at": "2026-07-29T03:30:00Z",
                "body": "[Open in Omnigent](https://omni.example/c/other_session)",
                "repo": "caffeinelabs/other",
            },
        ]


class _FakeConv:
    """Minimal conversation stub exposing the fields the PR route reads."""

    def __init__(self, labels: dict[str, str], created_at: int) -> None:
        self.labels = labels
        self.created_at = created_at
        # Read by check_session_access (sub-agent parent delegation) when a
        # permission store is wired.
        self.parent_conversation_id: str | None = None


class _FakeConvStore:
    """Conversation store stub returning a single canned session."""

    def __init__(self, convs: dict[str, _FakeConv]) -> None:
        self._convs = convs

    def get_conversation(self, session_id: str) -> _FakeConv | None:
        return self._convs.get(session_id)


def _config() -> GitHubAppConfig:
    return GitHubAppConfig(
        app_id=None,
        client_id="Iv1abc",
        client_secret="shh",
        private_key=None,
        redirect_uri="https://x/v1/integrations/github/callback",
        slug="omni-app",
    )


def _app(db_uri: str) -> tuple[TestClient, GithubConnectionStore, GitHubAppConfig, _FakeClient]:
    config = _config()
    # The store's cipher key is the credential store's own (OMNIGENT_CREDENTIAL_ENC_KEY),
    # independent of the GitHub App config.
    store = GithubConnectionStore(db_uri, SecretBox("store-enc-key"))
    client = _FakeClient()
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _handle(request: Request, exc: OmnigentError) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    app.include_router(
        create_integrations_github_router(
            config, store, auth_provider=_HeaderAuth(), client=client
        ),
        prefix="/v1",
    )
    # TestClient must not chase the external GitHub redirect.
    return TestClient(app, follow_redirects=False), store, config, client


_USER = {"X-Test-User": "alice@example.com"}


def test_status_unconnected(db_uri: str) -> None:
    tc, _store, _config, _client = _app(db_uri)
    resp = tc.get("/v1/integrations/github/status", headers=_USER)
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True
    assert body["connected"] is False
    assert body["login"] is None
    assert body["install_url"] == "https://github.com/apps/omni-app/installations/new"


def test_status_requires_auth(db_uri: str) -> None:
    tc, _store, _config, _client = _app(db_uri)
    # No X-Test-User header → require_user raises 401.
    resp = tc.get("/v1/integrations/github/status")
    assert resp.status_code == 401


def test_connect_redirects_to_github_with_signed_state(db_uri: str) -> None:
    tc, _store, config, _client = _app(db_uri)
    resp = tc.get(
        "/v1/integrations/github/connect", params={"return_to": "/settings"}, headers=_USER
    )
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith("https://github.com/login/oauth/authorize?")
    # Pull the state back out and verify it is signed + bound to the user.
    state = location.split("state=", 1)[1].split("&", 1)[0]
    claims = jwt.decode(state, config.client_secret, algorithms=["HS256"])
    assert claims["sub"] == "alice@example.com"
    assert claims["return_to"] == "/settings"


def test_callback_stores_connection_and_redirects(db_uri: str) -> None:
    tc, store, config, client = _app(db_uri)
    state = jwt.encode(
        {"sub": "alice@example.com", "return_to": "/settings", "nonce": "n", "exp": 9999999999},
        config.client_secret,
        algorithm="HS256",
    )
    resp = tc.get(
        "/v1/integrations/github/callback",
        params={"code": "abc", "state": state},
        headers=_USER,
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/settings?github=connected"
    assert client.exchanged == ["abc"]
    conn = store.get("alice@example.com", with_tokens=True)
    assert conn is not None
    assert conn.github_login == "octocat"
    assert conn.access_token == "ghu_new"


def test_callback_rejects_state_user_mismatch(db_uri: str) -> None:
    tc, store, config, _client = _app(db_uri)
    # State was signed for someone else — must not bind to alice.
    state = jwt.encode(
        {"sub": "mallory@example.com", "return_to": "/settings", "nonce": "n", "exp": 9999999999},
        config.client_secret,
        algorithm="HS256",
    )
    resp = tc.get(
        "/v1/integrations/github/callback",
        params={"code": "abc", "state": state},
        headers=_USER,
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/settings?github=error"
    assert store.get("alice@example.com") is None


def test_callback_rejects_bad_state(db_uri: str) -> None:
    tc, _store, _config, _client = _app(db_uri)
    resp = tc.get(
        "/v1/integrations/github/callback",
        params={"code": "abc", "state": "garbage"},
        headers=_USER,
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/settings?github=error"


def test_disconnect(db_uri: str) -> None:
    tc, store, _config, _client = _app(db_uri)
    store.upsert(
        "alice@example.com",
        github_login="octocat",
        github_user_id=42,
        tokens=GitHubTokenSet("ghu_a", "ghr_a", None, None, "repo"),
    )
    resp = tc.post("/v1/integrations/github/disconnect", headers=_USER)
    assert resp.status_code == 200
    assert resp.json() == {"disconnected": True}
    assert store.get("alice@example.com") is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("/settings", "/settings"),
        ("/settings?tab=integrations", "/settings?tab=integrations"),
        (None, "/settings"),
        ("", "/settings"),
        ("https://evil.com", "/settings"),
        ("//evil.com", "/settings"),
        ("/\\evil.com", "/settings"),  # backslash → browser reads protocol-relative
        ("/\t//evil.com", "/settings"),  # decoded control char
        ("/\x7fevil", "/settings"),
    ],
)
def test_sanitize_return_to_blocks_off_origin(raw: str | None, expected: str) -> None:
    assert _sanitize_return_to(raw) == expected


def test_repos_unconnected_returns_false(db_uri: str) -> None:
    tc, _store, _config, _client = _app(db_uri)
    resp = tc.get("/v1/integrations/github/repos", headers=_USER)
    assert resp.status_code == 200
    assert resp.json() == {"connected": False, "repos": [], "truncated": False}


def test_repos_lists_when_connected(db_uri: str) -> None:
    tc, store, _config, _client = _app(db_uri)
    store.upsert(
        "alice@example.com",
        github_login="octocat",
        github_user_id=42,
        tokens=GitHubTokenSet("ghu_a", "ghr_a", None, None, "repo"),
    )
    resp = tc.get("/v1/integrations/github/repos", headers=_USER)
    assert resp.status_code == 200
    body = resp.json()
    assert body["connected"] is True
    assert [r["full_name"] for r in body["repos"]] == ["caffeinelabs/app"]
    assert body["repos"][0]["default_branch"] == "main"


def test_repo_branches_unconnected_returns_false(db_uri: str) -> None:
    tc, _store, _config, _client = _app(db_uri)
    resp = tc.get("/v1/integrations/github/repos/caffeinelabs/app/branches", headers=_USER)
    assert resp.status_code == 200
    assert resp.json() == {"connected": False, "branches": []}


def test_repo_branches_lists_when_connected(db_uri: str) -> None:
    tc, store, _config, client = _app(db_uri)
    store.upsert(
        "alice@example.com",
        github_login="octocat",
        github_user_id=42,
        tokens=GitHubTokenSet("ghu_a", "ghr_a", None, None, "repo"),
    )
    resp = tc.get("/v1/integrations/github/repos/caffeinelabs/app/branches", headers=_USER)
    assert resp.status_code == 200
    assert resp.json() == {"connected": True, "branches": ["main", "dev"]}
    assert client.branch_calls == ["caffeinelabs/app"]


def test_repo_branches_rejects_bad_name(db_uri: str) -> None:
    tc, store, _config, _client = _app(db_uri)
    store.upsert(
        "alice@example.com",
        github_login="octocat",
        github_user_id=42,
        tokens=GitHubTokenSet("ghu_a", "ghr_a", None, None, "repo"),
    )
    # A path-traversal owner must be rejected before any GitHub call.
    resp = tc.get("/v1/integrations/github/repos/..%2Fx/app/branches", headers=_USER)
    assert resp.status_code in (400, 404)
    # A dot-run in a charset-valid segment is also rejected (no traversal).
    resp = tc.get("/v1/integrations/github/repos/o..o/app/branches", headers=_USER)
    assert resp.status_code == 400


# ── session pull-requests ───────────────────────────────────────────

_REPO_LABEL_KEY = "omnigent.sandbox.repo"
# 2026-07-29T00:00:00Z as epoch seconds — the fake session's start time.
_SESSION_START = 1785283200
# This instance's public base URL and the resulting Open-in-Omnigent link the
# MCP proxy stamps into PR bodies for session "conv_1".
_PUBLIC_BASE = "https://omni.example"
_SESSION_LINK = f"{_PUBLIC_BASE}/c/conv_1"


def _app_with_convs(
    db_uri: str, convs: dict[str, _FakeConv]
) -> tuple[TestClient, GithubConnectionStore, _FakeClient]:
    config = _config()
    store = GithubConnectionStore(db_uri, SecretBox("store-enc-key"))
    client = _FakeClient()
    app = FastAPI()
    app.include_router(
        create_integrations_github_router(
            config,
            store,
            auth_provider=_HeaderAuth(),
            client=client,
            conversation_store=_FakeConvStore(convs),
        ),
        prefix="/v1",
    )
    return TestClient(app, follow_redirects=False), store, client


def test_session_pulls_scopes_to_author_and_session_start(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIGENT_ACCOUNTS_BASE_URL", _PUBLIC_BASE)
    convs = {
        "conv_1": _FakeConv(
            labels={_REPO_LABEL_KEY: "https://github.com/caffeinelabs/app#main"},
            created_at=_SESSION_START,
        )
    }
    tc, store, client = _app_with_convs(db_uri, convs)
    store.upsert(
        "alice@example.com",
        github_login="octocat",
        github_user_id=42,
        tokens=GitHubTokenSet("ghu_a", "ghr_a", None, None, "repo"),
    )
    resp = tc.get("/v1/integrations/github/sessions/conv_1/pull-requests", headers=_USER)
    assert resp.status_code == 200
    body = resp.json()
    assert body["connected"] is True
    # Union of the cloned-repo listing (#1 open, #4 merged) and the cross-repo
    # search (#9, in a repo the session never cloned). #2 (pre-session), #3 (other
    # author), #5/#8 (another session's link) are all filtered out. Newest first,
    # and the search's duplicate #1 is de-duped against the cloned-repo #1.
    assert [p["number"] for p in body["pulls"]] == [9, 4, 1]
    by_num = {p["number"]: p for p in body["pulls"]}
    assert by_num[9]["repo"] == "caffeinelabs/other"  # cross-repo, MCP-opened
    assert by_num[4]["merged"] is True
    assert by_num[1]["repo"] == "caffeinelabs/app"
    assert by_num[1]["head_ref"] == "feat"  # richer cloned-repo record won dedup
    # The raw body is not leaked to the panel — the link was only a match key.
    assert "body" not in by_num[1]
    assert client.pull_calls == ["caffeinelabs/app"]
    assert len(getattr(client, "search_calls", [])) == 1  # one search request


def test_session_pulls_found_across_repos_without_a_clone(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No repo was selected/cloned; the agent opened PRs via the GitHub MCP. They
    # are still found by searching for the session link — the cloned-repo listing
    # is skipped (no repo), so detection is search-only.
    monkeypatch.setenv("OMNIGENT_ACCOUNTS_BASE_URL", _PUBLIC_BASE)
    convs = {"conv_1": _FakeConv(labels={}, created_at=_SESSION_START)}
    tc, store, client = _app_with_convs(db_uri, convs)
    store.upsert(
        "alice@example.com",
        github_login="octocat",
        github_user_id=42,
        tokens=GitHubTokenSet("ghu_a", "ghr_a", None, None, "repo"),
    )
    resp = tc.get("/v1/integrations/github/sessions/conv_1/pull-requests", headers=_USER)
    assert resp.status_code == 200
    body = resp.json()
    assert body["connected"] is True
    assert [p["number"] for p in body["pulls"]] == [9, 1]  # both from search
    assert not getattr(client, "pull_calls", [])  # no cloned repo → no listing


def test_session_pulls_empty_when_no_matching_link(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # conv_2's own link (…/c/conv_2) matches none of the candidate PR bodies (all
    # carry …/c/conv_1), so nothing is attributed to it.
    monkeypatch.setenv("OMNIGENT_ACCOUNTS_BASE_URL", _PUBLIC_BASE)
    convs = {"conv_2": _FakeConv(labels={}, created_at=_SESSION_START)}
    tc, store, _client = _app_with_convs(db_uri, convs)
    store.upsert(
        "alice@example.com",
        github_login="octocat",
        github_user_id=42,
        tokens=GitHubTokenSet("ghu_a", "ghr_a", None, None, "repo"),
    )
    resp = tc.get("/v1/integrations/github/sessions/conv_2/pull-requests", headers=_USER)
    assert resp.status_code == 200
    assert resp.json() == {"connected": True, "pulls": []}


def test_session_pulls_unconnected_returns_false(db_uri: str) -> None:
    convs = {
        "conv_3": _FakeConv(
            labels={_REPO_LABEL_KEY: "https://github.com/caffeinelabs/app"},
            created_at=_SESSION_START,
        )
    }
    tc, _store, _client = _app_with_convs(db_uri, convs)
    resp = tc.get("/v1/integrations/github/sessions/conv_3/pull-requests", headers=_USER)
    assert resp.status_code == 200
    assert resp.json() == {"connected": False, "pulls": []}


def test_session_pulls_missing_session_404(db_uri: str) -> None:
    tc, store, _client = _app_with_convs(db_uri, {})
    store.upsert(
        "alice@example.com",
        github_login="octocat",
        github_user_id=42,
        tokens=GitHubTokenSet("ghu_a", "ghr_a", None, None, "repo"),
    )
    resp = tc.get("/v1/integrations/github/sessions/nope/pull-requests", headers=_USER)
    assert resp.status_code == 404


def test_session_pulls_denies_non_owner_with_permission_store(db_uri: str) -> None:
    # The IDOR gate, actually exercised: with a permission store wired, a user
    # who has no grant on an EXISTING session gets 404 (existence-oracle
    # preserving), not that session's PRs. (Sibling tests build the router with
    # no permission store, so require_access is a no-op there.)
    store = GithubConnectionStore(db_uri, SecretBox("store-enc-key"))
    store.upsert(
        "mallory@example.com",
        github_login="mallory",
        github_user_id=7,
        tokens=GitHubTokenSet("ghu_m", "ghr_m", None, None, "repo"),
    )
    convs = {"conv_1": _FakeConv(labels={}, created_at=_SESSION_START)}
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _handle(request: Request, exc: OmnigentError) -> JSONResponse:
        del request
        return JSONResponse(status_code=exc.http_status, content={"error": {"code": exc.code}})

    app.include_router(
        create_integrations_github_router(
            _config(),
            store,
            auth_provider=_HeaderAuth(),
            client=_FakeClient(),
            conversation_store=_FakeConvStore(convs),
            permission_store=_DenyAllPermissions(),  # no grant → 404
        ),
        prefix="/v1",
    )
    tc = TestClient(app, follow_redirects=False)
    resp = tc.get(
        "/v1/integrations/github/sessions/conv_1/pull-requests",
        headers={"X-Test-User": "mallory@example.com"},
    )
    assert resp.status_code == 404


def test_body_links_session_matches_by_suffix_across_base_divergence() -> None:
    # The link stamped by the proxy (base A) still associates when this server
    # derives the URL from a different base (trailing slash, Databricks mount,
    # ?o=org) — the match is on the stable /c/<id> suffix.
    assert _body_links_session("[x](https://a.example/c/conv_1)", "conv_1")
    assert _body_links_session("[x](https://b.example/omnigent/c/conv_1)", "conv_1")
    assert _body_links_session('see <a href="https://a.example/c/conv_1?o=org">', "conv_1")
    # A longer id sharing this one as a prefix must NOT match.
    assert not _body_links_session("[x](https://a.example/c/conv_10)", "conv_1")
    # A different session's link must NOT match.
    assert not _body_links_session("[x](https://a.example/c/conv_2)", "conv_1")

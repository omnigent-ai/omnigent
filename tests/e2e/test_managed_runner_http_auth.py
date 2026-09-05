"""End-to-end proof of the managed-sandbox runner HTTP-auth fix (#357 HTTP half).

A server-managed sandbox runner has no user credential of its own — only its
tunnel binding token. Under accounts/OIDC auth every runner->server HTTP
callback gates on ``require_user``, so before this fix those callbacks went out
bare and 401'd (the runner connected its tunnel but could never fetch its own
agent spec). The Option-C fix has the runner mint a short-lived owner JWT from
``POST /v1/runners/{id}/token`` (authenticated by its binding token) and present
it as ``Authorization: Bearer`` on every callback.

This test proves that fix works against a **real** ``omnigent server``
subprocess with accounts auth enabled, driving the runner's **real** outbound
code over a **real** TCP socket — no transports are stubbed:

* ``_make_auth_token_factory`` -> ``_make_managed_mint_factory`` ->
  ``_mint_managed_owner_token`` (a real ``httpx`` POST to the mint endpoint), and
* ``_RunnerDatabricksAuth`` on a real ``httpx.AsyncClient`` GET.

The only thing simulated is the managed-sandbox *condition* — no ``omnigent
login`` token and no Databricks config on disk — which is exactly what makes the
fix necessary (and what a fresh sandbox actually looks like).

The differential is asserted in one test, so the mint is provably the cause of
the flip:

* WITHOUT a minted token (``_RunnerDatabricksAuth(None)`` — precisely what
  ``_make_auth_token_factory`` returns for a managed sandbox on ``main``):
  ``GET /v1/sessions/{id}/agent/contents`` -> **401**.
* WITH the fix: the same GET -> **200**, returning the agent bundle.

The second test replays the OMNI-2529 deadlock over the same live stack: a
managed runner whose re-mint gets 403 from the (simulated) Apps edge after its
owner JWT fully expires must re-resolve a local credential instead of raising
``httpx.RequestError`` on every callback forever.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
import pytest

from omnigent.runner._entry import (
    _InitialAuthTokenFactory,
    _make_auth_token_factory,
    _ManagedMintTokenFactory,
    _RunnerDatabricksAuth,
)
from omnigent.runner.identity import (
    OMNIGENT_INTERNAL_WS_ORIGIN,
    RUNNER_DELEGATED_AUTH_ENV_VAR,
    RUNNER_INITIAL_AUTH_TOKEN_ENV_VAR,
    RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR,
    token_bound_runner_id,
)
from omnigent.server.oidc import mint_session_cookie
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from tests._helpers.compat import apply_server_env, compat_server_cwd, server_executable
from tests._helpers.live_server import find_free_port
from tests.server.helpers import build_agent_bundle

# Repo root — this file lives at tests/e2e/<name>.py.
_REPO_ROOT = Path(__file__).resolve().parents[2]

# 32-byte cookie secret (64 hex chars), shared between this test process and the
# server subprocess so (a) the accounts cookie we mint for the owner validates
# server-side and (b) the JWT the mint endpoint signs validates the same way.
_COOKIE_SECRET_HEX = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2"
_OWNER = "alice@example.com"
_BINDING_TOKEN = "e2e-managed-sandbox-binding-token"
_SERVER_HEALTH_TIMEOUT_S = 40.0


def _await_health(base_url: str, log_path: Path) -> None:
    """Poll ``/health`` until the server answers 200, or fail with the log tail.

    :param base_url: Server base URL, e.g. ``"http://localhost:58123"``.
    :param log_path: Server stdout/stderr log, tailed into the failure message.
    :returns: None.
    :raises RuntimeError: If the server doesn't answer within the deadline.
    """
    deadline = time.monotonic() + _SERVER_HEALTH_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base_url}/health", timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            # Expected while the server is still booting (connection refused /
            # reset); keep polling until the deadline.
            pass
        time.sleep(0.5)
    tail = log_path.read_text()[-3000:] if log_path.exists() else "(no log)"
    raise RuntimeError(f"accounts server did not become healthy. Log:\n{tail}")


@pytest.fixture()
def accounts_server(tmp_path: Path) -> Iterator[tuple[str, str]]:
    """Run a real ``omnigent server`` subprocess with accounts auth enabled.

    Accounts mode is selected by ``OMNIGENT_AUTH_PROVIDER=accounts`` plus a
    shared cookie secret; the subprocess handles the full runtime lifecycle
    (migrations, DBOS, auth provider, permission store) exactly as a deployed
    server does. The server boots without prompting; first-admin setup is
    via the web form or ``--admin-password``.

    Deliberately independent of the session-scoped ``live_server`` fixture
    (which spawns a server + runner pair for the harness matrix): this test
    needs an accounts-auth server and no LLM, so it owns its own subprocess.

    :param tmp_path: Per-test temp dir for the DB, artifacts, and server log.
    :returns: ``(base_url, db_uri)`` — the running server's URL and the SQLite
        URI the test opens directly to bind the managed runner id.
    """
    port = find_free_port()
    db_path = tmp_path / "e2e.db"
    db_uri = f"sqlite:///{db_path}"
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    log_path = tmp_path / "server.log"
    base_url = f"http://localhost:{port}"

    env = {**os.environ}
    env["OMNIGENT_AUTH_PROVIDER"] = "accounts"
    env["OMNIGENT_ACCOUNTS_COOKIE_SECRET"] = _COOKIE_SECRET_HEX
    env["OMNIGENT_ACCOUNTS_BASE_URL"] = base_url
    # Force the accounts branch of the auth-source switch (an ambient OIDC
    # issuer in the environment would otherwise select oidc mode).
    env.pop("OMNIGENT_OIDC_ISSUER", None)
    # Import the server package from this worktree, not an installed copy.
    apply_server_env(env, _REPO_ROOT)

    log_handle = open(log_path, "w")  # noqa: SIM115 — handle lives for the subprocess
    proc = subprocess.Popen(
        [
            server_executable(),
            "-m",
            "omnigent.cli",
            "server",
            "--port",
            str(port),
            "--database-uri",
            db_uri,
            "--artifact-location",
            str(artifact_dir),
        ],
        env=env,
        cwd=compat_server_cwd(),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    try:
        _await_health(base_url, log_path)
        yield base_url, db_uri
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        log_handle.close()


def _seed_owned_session_with_managed_runner(base_url: str, db_uri: str) -> str:
    """Create Alice's session and bind the managed runner id to it.

    Alice's identity comes from a directly-minted accounts cookie signed with
    the server's shared secret — the same JWT the password login flow issues;
    only the password dance is skipped. The session-create, agent registration,
    and owner grant are all real. The runner-id bind is what the managed-launch
    path does at spawn time via ``replace_runner_id``; WAL journaling + a 20s
    busy_timeout make this cross-process write safe against the running server,
    which then resolves runner_id -> owner from this row.

    :param base_url: Live server base URL.
    :param db_uri: SQLite URI of the server's database.
    :returns: The created session id.
    """
    owner_cookie = mint_session_cookie(_OWNER, bytes.fromhex(_COOKIE_SECRET_HEX), 8, "accounts")
    bundle = build_agent_bundle(name="e2e-managed-runner-agent")
    with httpx.Client(base_url=base_url, timeout=30.0) as http:
        create = http.post(
            "/v1/sessions",
            headers={
                "Authorization": f"Bearer {owner_cookie}",
                "Origin": OMNIGENT_INTERNAL_WS_ORIGIN,
            },
            data={"metadata": "{}"},
            files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        )
    assert create.status_code in (200, 201), (create.status_code, create.text)
    session_id = create.json()["session_id"]

    runner_id = token_bound_runner_id(_BINDING_TOKEN)
    SqlAlchemyConversationStore(db_uri).replace_runner_id(session_id, runner_id)
    return session_id


def _start_apps_edge_proxy(
    upstream: str,
) -> tuple[str, threading.Event, Callable[[], None]]:
    """Run a reverse proxy that can start 403ing the managed-mint endpoint.

    Stands in for the Databricks Apps edge: every request is forwarded to
    *upstream* over a real socket until ``forbid_mint`` is set, after which
    ``POST /v1/runners/{id}/token`` is answered ``403 Invalid Token`` without
    reaching the server — exactly what the edge does once the bearer presented
    on the mint request has expired.

    :param upstream: Base URL of the real server to forward to.
    :returns: ``(proxy_base_url, forbid_mint, shutdown)``.
    """
    forbid_mint = threading.Event()
    forward = httpx.Client(base_url=upstream, timeout=30.0)

    class _EdgeHandler(BaseHTTPRequestHandler):
        """Forwarding handler with a switchable mint-endpoint 403."""

        def log_message(self, format: str, *args: object) -> None:
            """Suppress default request logging."""
            del format, args

        def do_GET(self) -> None:
            self._relay()

        def do_POST(self) -> None:
            self._relay()

        def _relay(self) -> None:
            """Forward one request upstream, or 403 a forbidden mint."""
            # Constrain the request target to the API paths this test drives:
            # an absolute-form target (scheme/netloc) would override the
            # forward client's fixed base_url, so only origin-form ``/v1/...``
            # targets are relayed; everything else is refused.
            parts = urlsplit(self.path)
            if parts.scheme or parts.netloc or not parts.path.startswith("/v1/"):
                self.send_error(404)
                return
            target = urlunsplit(("", "", parts.path, parts.query, ""))
            if (
                forbid_mint.is_set()
                and self.command == "POST"
                and parts.path.startswith("/v1/runners/")
                and parts.path.endswith("/token")
            ):
                body = b"Invalid Token"
                self.send_response(403)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            length = int(self.headers.get("Content-Length") or 0)
            content = self.rfile.read(length) if length else b""
            headers = {
                name: value
                for name, value in self.headers.items()
                if name.lower() not in ("host", "content-length", "connection", "accept-encoding")
            }
            upstream_resp = forward.request(self.command, target, content=content, headers=headers)
            self.send_response(upstream_resp.status_code)
            for name in ("Content-Type", "X-Agent-Name", "Location"):
                value = upstream_resp.headers.get(name)
                if value:
                    self.send_header(name, value)
            self.send_header("Content-Length", str(len(upstream_resp.content)))
            self.end_headers()
            self.wfile.write(upstream_resp.content)

    server = ThreadingHTTPServer(("127.0.0.1", 0), _EdgeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def _shutdown() -> None:
        server.shutdown()
        server.server_close()
        forward.close()

    return f"http://127.0.0.1:{server.server_port}", forbid_mint, _shutdown


async def _get_agent_contents(
    base_url: str,
    path: str,
    auth: _RunnerDatabricksAuth,
) -> httpx.Response:
    """Drive the runner's real callback client for one ``GET``.

    Builds the same ``httpx.AsyncClient`` the runner uses for its server
    callbacks (``auth=_RunnerDatabricksAuth(...)``, sentinel ``Origin``,
    redirects off) and issues a single request over a real socket.

    :param base_url: Live server base URL.
    :param path: Request path, e.g. ``"/v1/sessions/<id>/agent/contents"``.
    :param auth: The runner's httpx auth — ``_RunnerDatabricksAuth(None)`` for
        the bare (pre-fix) leg, or one wired to the managed-mint factory.
    :returns: The HTTP response.
    """
    async with httpx.AsyncClient(
        base_url=base_url,
        auth=auth,
        headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
        follow_redirects=False,
        timeout=30.0,
    ) as client:
        return await client.get(path)


def test_managed_runner_callback_authenticates_end_to_end(
    accounts_server: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A managed runner's HTTP callback 401s bare and 200s with a minted token.

    End-to-end against a live accounts-auth server, driving the runner's real
    outbound auth code over a real socket. Reverting the runner-side managed
    mint tier (or the server-side mint endpoint) turns the 200 assertion back
    into a 401 — that is the exact gap this change closes.

    :param accounts_server: ``(base_url, db_uri)`` from the live-server fixture.
    :param monkeypatch: Puts this process into the managed-sandbox posture
        (binding token + server URL present; no user credential resolvable).
    :returns: None.
    """
    base_url, db_uri = accounts_server

    # 1-2. Alice owns a real session with a managed runner id bound to it.
    session_id = _seed_owned_session_with_managed_runner(base_url, db_uri)

    # 3. Put this process in a managed-sandbox posture: the runner holds ONLY
    #    its binding token and the server URL — no omnigent-login token, no
    #    Databricks config. Forcing both credential sources to miss is what a
    #    fresh sandbox actually is, and it routes _make_auth_token_factory to
    #    the managed-mint tier under test.
    from omnigent.inner.databricks_executor import DatabricksAuthError

    def _no_databricks_creds(*args: object, **kwargs: object) -> tuple[object, str]:
        """Stand in for _resolve_databricks_auth in a credential-less sandbox."""
        raise DatabricksAuthError("managed sandbox has no Databricks config")

    monkeypatch.setenv("RUNNER_SERVER_URL", base_url)
    monkeypatch.setenv(RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR, _BINDING_TOKEN)
    monkeypatch.setattr("omnigent.cli_auth.load_token", lambda _url, **_kw: None)
    monkeypatch.setattr(
        "omnigent.inner.databricks_executor._resolve_databricks_auth",
        _no_databricks_creds,
    )

    contents_path = f"/v1/sessions/{session_id}/agent/contents"

    # 4a. Pre-fix behavior: a managed sandbox on main resolves no credential,
    #     so _make_auth_token_factory returns None and the callback goes out
    #     with no bearer. require_user rejects it.
    bare = asyncio.run(_get_agent_contents(base_url, contents_path, _RunnerDatabricksAuth(None)))
    assert bare.status_code == 401, (bare.status_code, bare.text)

    # 4b. With the fix: the factory installs the managed-mint tier, mints a
    #     short-lived owner JWT from the binding token (a real POST to the mint
    #     endpoint at construction), and presents it on the callback.
    factory = _make_auth_token_factory()
    assert factory is not None, (
        "managed-mint factory should install for a managed sandbox "
        "(binding token + server URL present, no user credential)"
    )
    authed = asyncio.run(
        _get_agent_contents(base_url, contents_path, _RunnerDatabricksAuth(factory))
    )
    assert authed.status_code == 200, (authed.status_code, authed.text)
    # The minted owner token resolved to Alice, who owns the session, so the
    # real agent bundle comes back.
    assert authed.headers.get("X-Agent-Name")
    assert authed.content[:2] == b"\x1f\x8b", "expected a gzip agent bundle body"


def test_managed_runner_survives_mint_403_after_token_expiry(
    accounts_server: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mint 403 after the owner JWT expires must not brick the runner (OMNI-2529).

    Replays the production deadlock end-to-end over real sockets: a
    host-launched runner bootstraps on the injected bearer, falls back to
    managed mint when that bearer is rejected (a real mint through the Apps
    edge stand-in), then the session outlives the minted JWT while idle. The
    re-mint presents the expired JWT as its own proxy bearer, so the edge
    answers 403 without reaching the server. Before the fix neither
    ``declined`` nor ``proxy_auth_failed`` latched in that state, and every
    subsequent callback raised ``httpx.RequestError("Databricks token refresh
    returned no token")`` for the remaining life of the process. With the fix
    the chain re-resolves the machine's stored login credential in the same
    call, so the callback that hits the 403 still authenticates.

    :param accounts_server: ``(base_url, db_uri)`` from the live-server fixture.
    :param monkeypatch: Puts this process into the host-launched-runner posture
        (injected initial bearer, delegated-mint marker, binding token) with a
        stored login credential available for recovery.
    :returns: None.
    """
    base_url, db_uri = accounts_server
    session_id = _seed_owned_session_with_managed_runner(base_url, db_uri)
    proxy_url, forbid_mint, shutdown_proxy = _start_apps_edge_proxy(base_url)
    try:
        # Host-launched posture: the runner starts with an injected bearer and
        # the delegated-mint marker, and everything it sends goes through the
        # Apps edge. The machine also holds a stored `omnigent login` token —
        # the recovery credential the pre-fix code never consulted.
        owner_cookie = mint_session_cookie(
            _OWNER, bytes.fromhex(_COOKIE_SECRET_HEX), 8, "accounts"
        )
        monkeypatch.setenv("RUNNER_SERVER_URL", proxy_url)
        monkeypatch.setenv(RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR, _BINDING_TOKEN)
        monkeypatch.setenv(RUNNER_INITIAL_AUTH_TOKEN_ENV_VAR, owner_cookie)
        monkeypatch.setenv(RUNNER_DELEGATED_AUTH_ENV_VAR, "1")
        monkeypatch.setattr("omnigent.cli_auth.load_token", lambda _url, **_kw: owner_cookie)

        factory = _make_auth_token_factory()
        assert isinstance(factory, _InitialAuthTokenFactory)
        contents_path = f"/v1/sessions/{session_id}/agent/contents"

        # The injected host bearer authenticates the first callback.
        first = asyncio.run(
            _get_agent_contents(proxy_url, contents_path, _RunnerDatabricksAuth(factory))
        )
        assert first.status_code == 200, (first.status_code, first.text)

        # The host bearer is rejected -> managed mint takes over: a real mint
        # POST through the edge to the live server, then the callback runs on
        # the minted owner JWT.
        factory.invalidate()
        minted = asyncio.run(
            _get_agent_contents(proxy_url, contents_path, _RunnerDatabricksAuth(factory))
        )
        assert minted.status_code == 200, (minted.status_code, minted.text)
        mint_factory = factory._fallback_factory
        assert isinstance(mint_factory, _ManagedMintTokenFactory)
        assert mint_factory._cached_token, "a real owner JWT should have been minted"

        # The session outlives the JWT: the cache is fully expired and the
        # edge now 403s every re-mint (the presented bearer is dead).
        mint_factory._cached_expires_at = time.time() - 1.0
        forbid_mint.set()

        # Pre-fix this raised httpx.RequestError — and would forever. The
        # chain must instead latch proxy_auth_failed and hand this same
        # callback the stored login credential.
        recovered = asyncio.run(
            _get_agent_contents(proxy_url, contents_path, _RunnerDatabricksAuth(factory))
        )
        assert recovered.status_code == 200, (recovered.status_code, recovered.text)
        assert mint_factory.proxy_auth_failed is True
    finally:
        shutdown_proxy()

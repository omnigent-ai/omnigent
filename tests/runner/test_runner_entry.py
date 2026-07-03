"""Tests for the runner subprocess entry-point wiring."""

from __future__ import annotations

import asyncio
import importlib
import io
import logging
import os
import tarfile
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.runner._entry import (
    _DEFAULT_RUNNER_IDLE_TIMEOUT_S,
    _agent_cache_dest,
    _load_runner_idle_timeout_s_from_config,
    _make_auth_token_factory,
    _parent_is_orphaned,
    _parent_process_is_alive,
    _resolve_agent_spec_from_server,
    _run_inactivity_monitor,
    _run_parent_death_killer,
    _runner_parent_pid_from_env,
    _runner_tunnel_binding_token_from_env,
    _runner_workspace_from_env,
    _RunnerDatabricksAuth,
    _server_url_from_env,
    main,
)
from omnigent.runner.transports.ws_tunnel.serve import RUNNER_TUNNEL_REJECTION_PREFIX

# Force-load the MCP streamable-http client before any test monkeypatches
# httpx.AsyncClient: the MCP SDK evaluates `httpx.AsyncClient | None` eagerly at
# import, which TypeErrors if AsyncClient has been swapped for a stub. Loading it
# here (via import_module, so there is no bound-but-unused import) resolves and
# caches it with the real type.
importlib.import_module("mcp.client.streamable_http")


class _TrackingTerminalRegistry:
    """TerminalRegistry stand-in that records shutdown calls."""

    def __init__(self, *, conversation_link_base_url: str | None = None) -> None:
        """
        Initialize the terminal registry test double.

        :param conversation_link_base_url: Omnigent server base URL passed
            through by the runner entry point, e.g.
            ``"http://runner.test"``.
        :returns: None.
        """
        self.conversation_link_base_url = conversation_link_base_url
        self.shutdown_called = False

    async def shutdown(self) -> None:
        self.shutdown_called = True


class _TrackingMcpManager:
    """RunnerMcpManager stand-in that records shutdown calls."""

    def __init__(self, stdio_cwd: Path | None = None) -> None:
        self.stdio_cwd = stdio_cwd
        self.shutdown_called = False

    async def shutdown(self) -> None:
        self.shutdown_called = True


class _TrackingAsyncClient:
    """httpx.AsyncClient stand-in that records close calls."""

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class _TrackingSyncClient:
    """httpx.Client stand-in that records close calls."""

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_server_url_from_env_requires_explicit_runner_server_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing ``RUNNER_SERVER_URL`` fails loud instead of defaulting.

    :param monkeypatch: Pytest environment patch fixture.
    :returns: None.
    """
    monkeypatch.delenv("RUNNER_SERVER_URL", raising=False)

    with pytest.raises(RuntimeError, match="RUNNER_SERVER_URL is required"):
        _server_url_from_env()


def test_server_url_from_env_strips_configured_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Configured ``RUNNER_SERVER_URL`` is returned without whitespace.

    :param monkeypatch: Pytest environment patch fixture.
    :returns: None.
    """
    monkeypatch.setenv("RUNNER_SERVER_URL", " http://127.0.0.1:8123 ")

    assert _server_url_from_env() == "http://127.0.0.1:8123"


def test_make_auth_token_factory_returns_factory_when_databricks_creds_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The factory is created when Databricks SDK credentials exist.

    The runner uses this factory to refresh tokens on each WebSocket
    reconnect and each httpx callback. Without it, tokens expire
    after 1 hour and long-running sessions break.

    :param monkeypatch: Pytest environment patch fixture.
    :returns: None.
    """
    from omnigent.inner.databricks_executor import _DatabricksBearerAuth

    class _Cfg:
        """Config double whose authenticate() yields a Bearer header."""

        def authenticate(self) -> dict[str, str]:
            return {"Authorization": "Bearer fresh-token"}

    # The factory resolves SDK auth via _resolve_databricks_auth (reused
    # once) and reads tokens through _DatabricksBearerAuth.current_token().
    monkeypatch.delenv("RUNNER_SERVER_URL", raising=False)  # skip OIDC branch
    monkeypatch.setattr(
        "omnigent.inner.databricks_executor._resolve_databricks_auth",
        lambda profile=None: (_DatabricksBearerAuth(_Cfg(), profile_name=None), "https://ex.test"),
    )

    factory = _make_auth_token_factory()

    # Factory must be created so the runner can refresh tokens.
    # If None, the runner has no way to mint fresh tokens on reconnect.
    assert factory is not None, (
        "_make_auth_token_factory returned None despite Databricks credentials being available."
    )
    assert factory() == "fresh-token"


def test_make_auth_token_factory_returns_none_without_databricks_creds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without Databricks credentials the factory is ``None``.

    Local unauthenticated servers don't need auth — the runner
    connects over loopback without a bearer token.

    :param monkeypatch: Pytest environment patch fixture.
    :returns: None.
    """
    from omnigent.inner.databricks_executor import DatabricksAuthError

    def _no_creds(profile: str | None = None) -> tuple[Any, str]:
        """Stand in for _resolve_databricks_auth with no credentials."""
        raise DatabricksAuthError("no Databricks credentials configured")

    # No stored OIDC token and SDK resolution fails → factory is None, so the
    # runner connects to a local unauthenticated server without a bearer.
    monkeypatch.delenv("RUNNER_SERVER_URL", raising=False)  # skip OIDC branch
    monkeypatch.setattr(
        "omnigent.inner.databricks_executor._resolve_databricks_auth",
        _no_creds,
    )

    assert _make_auth_token_factory() is None


def test_runner_databricks_auth_injects_fresh_token_per_request() -> None:
    """``_RunnerDatabricksAuth`` calls the factory on every request.

    This is the mechanism that keeps the runner's httpx client
    authenticated after the initial OAuth token expires. If the
    factory is called only once (cached), HTTP callbacks to the
    Omnigent server break after 1 hour.

    :returns: None.
    """
    call_count = 0

    def _counting_factory() -> str:
        """Return incrementing tokens.

        :returns: Token string with call sequence number.
        """
        nonlocal call_count
        call_count += 1
        return f"tok-{call_count}"

    auth = _RunnerDatabricksAuth(_counting_factory)
    request = httpx.Request("GET", "http://server/v1/agents")

    # First request gets tok-1.
    gen = auth.auth_flow(request)
    sent = next(gen)
    assert sent.headers["Authorization"] == "Bearer tok-1"

    # Second request gets tok-2 — factory is called again, not cached.
    request2 = httpx.Request("GET", "http://server/v1/agents")
    gen2 = auth.auth_flow(request2)
    sent2 = next(gen2)
    assert sent2.headers["Authorization"] == "Bearer tok-2"

    # Factory was called twice — once per request.
    # If call_count == 1, the token was cached and HTTP callbacks
    # would break after expiry.
    assert call_count == 2, (
        f"Factory was called {call_count} time(s), expected 2. "
        f"If 1, the auth is caching the token instead of refreshing."
    )


def test_runner_databricks_auth_noop_without_factory() -> None:
    """No factory means no auth header — local unauthenticated servers.

    :returns: None.
    """
    auth = _RunnerDatabricksAuth(None)
    request = httpx.Request("GET", "http://localhost:8000/health")
    gen = auth.auth_flow(request)
    sent = next(gen)
    assert "Authorization" not in sent.headers


def _drive_auth_flow(
    auth: _RunnerDatabricksAuth,
    request: httpx.Request,
    response: httpx.Response,
) -> list[str]:
    """Drive ``auth_flow`` through one request → response → maybe-retry cycle.

    Returns the ``Authorization`` header captured *at yield time* for
    each yield, so the caller can distinguish initial vs retry tokens.
    Capturing at yield time matters: ``auth_flow`` mutates the same
    ``Request`` object's headers in place on retry, so reading the
    request after the retry would show only the latest token.

    :param auth: The auth object under test.
    :param request: The outgoing request.
    :param response: The response to feed back into the auth flow on
        the second yield, e.g. a 302 to ``/oidc/...``.
    :returns: List of bearer header values, length 1 (no retry) or 2
        (retry).
    """
    gen = auth.auth_flow(request)
    first_request = next(gen)
    captured: list[str] = [first_request.headers.get("Authorization", "")]
    try:
        retry_request = gen.send(response)
    except StopIteration:
        return captured
    captured.append(retry_request.headers.get("Authorization", ""))
    # Drain — auth_flow yields at most once after the response, then
    # exits without inspecting the second response.
    with pytest.raises(StopIteration):
        gen.send(httpx.Response(200))
    return captured


@pytest.mark.parametrize(
    "location",
    [
        # Real-world shape captured from the Omnigent HTTP path: the Apps
        # front door redirects directly to ``/oidc/...authorize``
        # with a ``redirect_uri`` of ``.../.auth/callback``.
        (
            "https://example.cloud.databricks.com/oidc/oauth2/v2.0/"
            "authorize?redirect_uri="
            "https%3A%2F%2Fapp.databricksapps.com%2F.auth%2Fcallback"
        ),
        # Path-only form (some Apps deployments).
        "/oidc/oauth2/v2.0/authorize",
        # Apps callback path, on the off chance it surfaces directly.
        "/.auth/callback?code=abc",
    ],
)
def test_runner_databricks_auth_remints_on_login_redirect(location: str) -> None:
    """A 302→login redirect re-mints the bearer and retries.

    This is the ``ness-tool-spin`` regression: the Databricks Apps
    front door bounces an expired bearer with a 302 to
    ``/oidc/...`` (or to a login HTML page whose ``next_url`` is
    the OIDC authorize endpoint) instead of returning 401, so a
    handler that only re-mints on 401 silently fails. Without the
    retry, ``ProxyMcpManager.call_tool`` raises and tool spinners
    in the UI never resolve.

    :param location: Redirect ``Location`` header value to test.
    :returns: None.
    """
    minted_tokens: list[str] = []

    def _factory() -> str:
        """Mint sequential tokens so the test can tell first vs retry apart.

        :returns: A unique token string per call.
        """
        token = f"tok-{len(minted_tokens) + 1}"
        minted_tokens.append(token)
        return token

    auth = _RunnerDatabricksAuth(_factory)
    request = httpx.Request("POST", "http://server/v1/sessions/conv_x/mcp")
    redirect = httpx.Response(302, headers={"Location": location})

    captured = _drive_auth_flow(auth, request, redirect)

    # First yield carries the original (now-stale) token; second yield
    # carries the freshly-minted token. Two yields means the retry
    # happened — without the fix this collapses to one yield and the
    # caller surfaces the 302 as a hard error.
    assert len(captured) == 2, (
        f"Expected one retry on login-redirect, got {len(captured)} yield(s). "
        f"If 1, the auth flow stopped after the redirect instead of "
        f"re-minting; this is the bug ProxyMcpManager hits as "
        f"\"MCP proxy call failed ... Redirect response '302 Found'\"."
    )
    assert captured[0] == "Bearer tok-1"
    assert captured[1] == "Bearer tok-2"
    # Two factory calls means a fresh token was minted for the retry,
    # not the cached stale one. If 1, the retry replayed the same
    # bearer and the Apps proxy would 302 again in production.
    assert minted_tokens == ["tok-1", "tok-2"]


@pytest.mark.parametrize(
    "location",
    [
        # Unrelated app-level redirect — must NOT trigger a re-mint.
        "/v1/agents/agent_abc",
        "https://other.example.com/some/path",
        # Empty Location (malformed redirect) — also not a login bounce.
        "",
    ],
)
def test_runner_databricks_auth_does_not_remint_on_unrelated_redirect(
    location: str,
) -> None:
    """A 3xx that isn't an Apps login bounce must NOT re-mint.

    Re-minting on every redirect would hide real application bugs and
    waste an OAuth token round-trip per follow. Only ``/oidc/`` and
    ``/.auth/`` redirects are treated as auth signals.

    :param location: Non-login redirect ``Location`` value.
    :returns: None.
    """
    factory_calls = 0

    def _factory() -> str:
        """Track how many tokens the auth flow minted.

        :returns: A constant token; the test asserts on call count.
        """
        nonlocal factory_calls
        factory_calls += 1
        return "tok"

    auth = _RunnerDatabricksAuth(_factory)
    request = httpx.Request("GET", "http://server/v1/agents/agent_abc")
    redirect = httpx.Response(302, headers={"Location": location} if location else {})

    captured = _drive_auth_flow(auth, request, redirect)

    # Only the initial request was sent — no retry. If 2, the auth
    # flow is treating non-login redirects as re-auth signals,
    # which would cause spurious token churn.
    assert len(captured) == 1
    # Factory was invoked exactly once for the initial request.
    # If 2, a retry was attempted despite the redirect not being
    # an Apps login bounce.
    assert factory_calls == 1


def test_runner_databricks_auth_remints_on_401() -> None:
    """The classic 401 path still re-mints (regression guard).

    The login-redirect fix added a parallel branch alongside the 401
    check; this test pins that the 401 path didn't regress.

    :returns: None.
    """
    minted_tokens: list[str] = []

    def _factory() -> str:
        """Return sequential tokens so first vs retry are distinguishable.

        :returns: Token string with a 1-indexed sequence number.
        """
        token = f"tok-{len(minted_tokens) + 1}"
        minted_tokens.append(token)
        return token

    auth = _RunnerDatabricksAuth(_factory)
    request = httpx.Request("GET", "http://server/v1/agents")
    unauthorized = httpx.Response(401)

    captured = _drive_auth_flow(auth, request, unauthorized)

    assert len(captured) == 2
    assert captured[1] == "Bearer tok-2"


@pytest.mark.asyncio
async def test_runner_databricks_auth_end_to_end_through_mock_transport() -> None:
    """End-to-end: a 302→/oidc/ becomes a 200 after the bearer refresh.

    This is the integration-shaped variant of the unit tests above.
    It exercises the actual ``httpx.AsyncClient`` send pipeline (auth
    flow → transport → auth flow → transport) so a regression in how
    the Auth class plugs into httpx is caught here, not just in
    isolation. Mirrors the production flow:

    1. Runner posts to ``/v1/sessions/{id}/mcp`` with stale bearer.
    2. Omnigent front door bounces with ``302 → /oidc/...authorize``.
    3. Runner re-mints, retries with fresh bearer, server returns 200.

    Without the login-redirect branch in ``auth_flow``, step 3 never
    happens and the call surfaces as a hard 302 to ``ProxyMcpManager``.

    :returns: None.
    """
    minted_tokens: list[str] = []
    seen_authz: list[str] = []

    def _factory() -> str:
        """Mint sequential tokens so the server can tell stale from fresh.

        :returns: Token string ``tok-1``, ``tok-2``, ...
        """
        token = f"tok-{len(minted_tokens) + 1}"
        minted_tokens.append(token)
        return token

    def _handler(request: httpx.Request) -> httpx.Response:
        """Reject the first bearer with a 302→OIDC; accept the second.

        :param request: Incoming httpx request from the client.
        :returns: 302 on the first call, 200 on the second.
        """
        seen_authz.append(request.headers.get("authorization", ""))
        if len(seen_authz) == 1:
            return httpx.Response(
                302,
                headers={
                    "Location": (
                        "https://workspace.example.com/oidc/oauth2/v2.0/authorize?state=abc"
                    ),
                },
            )
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})

    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(
        base_url="http://ap.example.com",
        auth=_RunnerDatabricksAuth(_factory),
        transport=transport,
    ) as client:
        resp = await client.post(
            "/v1/sessions/conv_abc/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )

    # 200 means the retry happened, the fresh bearer was accepted, and
    # the caller (ProxyMcpManager in production) sees a normal success.
    # If this assertion fails with status_code == 302, the auth flow
    # is not re-minting on the login-redirect path — the original bug.
    assert resp.status_code == 200
    # The server saw two distinct bearers: the stale one, then the
    # fresh one. Equal tokens here would mean the retry replayed the
    # cached value instead of asking the factory for a new one.
    assert seen_authz == ["Bearer tok-1", "Bearer tok-2"]
    assert minted_tokens == ["tok-1", "tok-2"]


def test_runner_tunnel_binding_token_from_env_returns_none_without_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unauthenticated local servers do not get a tunnel binding token.

    :param monkeypatch: Pytest environment patch fixture.
    :returns: None.
    """
    monkeypatch.delenv("OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN", raising=False)

    assert _runner_tunnel_binding_token_from_env() is None


def test_runner_tunnel_binding_token_from_env_rejects_empty_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Configured empty tunnel binding tokens fail loud.

    :param monkeypatch: Pytest environment patch fixture.
    :returns: None.
    """
    monkeypatch.setenv("OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN", "  ")

    with pytest.raises(RuntimeError, match="must not be empty"):
        _runner_tunnel_binding_token_from_env()


def test_runner_tunnel_binding_token_from_env_strips_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Authenticated remote runners forward the binding token.

    :param monkeypatch: Pytest environment patch fixture.
    :returns: None.
    """
    monkeypatch.setenv("OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN", " bind-token ")

    assert _runner_tunnel_binding_token_from_env() == "bind-token"


def test_runner_parent_pid_from_env_returns_none_without_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Manual runners can omit parent-pid watchdog wiring.

    :param monkeypatch: Pytest environment patch fixture.
    :returns: None.
    """
    monkeypatch.delenv("OMNIGENT_RUNNER_PARENT_PID", raising=False)

    assert _runner_parent_pid_from_env() is None


@pytest.mark.parametrize("value", ["  ", "not-a-pid", "0", "-1"])
def test_runner_parent_pid_from_env_rejects_invalid_pid(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    """Invalid parent-pid values fail before the runner starts.

    :param monkeypatch: Pytest environment patch fixture.
    :param value: Invalid environment value under test.
    :returns: None.
    """
    monkeypatch.setenv("OMNIGENT_RUNNER_PARENT_PID", value)

    with pytest.raises(RuntimeError, match="OMNIGENT_RUNNER_PARENT_PID"):
        _runner_parent_pid_from_env()


def test_runner_parent_pid_from_env_strips_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Configured parent pids are parsed from the environment.

    :param monkeypatch: Pytest environment patch fixture.
    :returns: None.
    """
    monkeypatch.setenv("OMNIGENT_RUNNER_PARENT_PID", " 12345 ")

    assert _runner_parent_pid_from_env() == 12345


def test_load_runner_idle_timeout_defaults_when_config_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Missing runner config uses the default one-hour idle timeout.

    :param monkeypatch: Pytest environment patch fixture.
    :param tmp_path: Isolated config home.
    :returns: None.
    """
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))

    assert _load_runner_idle_timeout_s_from_config() == float(_DEFAULT_RUNNER_IDLE_TIMEOUT_S)


def test_load_runner_idle_timeout_reads_nested_runner_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``runner.idle_timeout_s`` configures the runner idle watchdog.

    :param monkeypatch: Pytest environment patch fixture.
    :param tmp_path: Isolated config home.
    :returns: None.
    """
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "runner:\n  idle_timeout_s: 12.5\n",
        encoding="utf-8",
    )

    assert _load_runner_idle_timeout_s_from_config() == 12.5


def test_load_runner_idle_timeout_zero_disables_watchdog(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``runner.idle_timeout_s: 0`` disables self-shutdown.

    :param monkeypatch: Pytest environment patch fixture.
    :param tmp_path: Isolated config home.
    :returns: None.
    """
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "runner:\n  idle_timeout_s: 0\n",
        encoding="utf-8",
    )

    assert _load_runner_idle_timeout_s_from_config() == 0.0


@pytest.mark.parametrize(
    "config_text",
    [
        pytest.param("runner: disabled\n", id="runner-not-mapping"),
        pytest.param("runner:\n  idle_timeout_s: -1\n", id="negative"),
        pytest.param("runner:\n  idle_timeout_s: true\n", id="boolean"),
        pytest.param("runner:\n  idle_timeout_s: soon\n", id="string"),
    ],
)
def test_load_runner_idle_timeout_rejects_invalid_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    config_text: str,
) -> None:
    """Invalid idle-timeout config fails loud during runner startup.

    :param monkeypatch: Pytest environment patch fixture.
    :param tmp_path: Isolated config home.
    :param config_text: Invalid config body under test.
    :returns: None.
    """
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(config_text, encoding="utf-8")

    with pytest.raises(RuntimeError, match="runner"):
        _load_runner_idle_timeout_s_from_config()


@pytest.mark.asyncio
async def test_inactivity_monitor_requests_shutdown_when_idle() -> None:
    """Expired idle timeout requests graceful runner shutdown.

    :returns: None.
    """
    loop = asyncio.get_running_loop()
    shutdowns: list[str] = []

    await asyncio.wait_for(
        _run_inactivity_monitor(
            idle_timeout_s=0.01,
            get_last_activity=lambda: loop.time() - 1.0,
            has_active_work=lambda: False,
            request_shutdown=lambda: shutdowns.append("shutdown"),
            poll_interval_s=0.001,
        ),
        timeout=0.1,
    )

    assert shutdowns == ["shutdown"]


@pytest.mark.asyncio
async def test_inactivity_monitor_waits_for_active_work_to_finish() -> None:
    """Expired idle timeout does not stop a running agent turn.

    :returns: None.
    """
    loop = asyncio.get_running_loop()
    active = True
    shutdowns: list[str] = []

    task = asyncio.create_task(
        _run_inactivity_monitor(
            idle_timeout_s=0.01,
            get_last_activity=lambda: loop.time() - 1.0,
            has_active_work=lambda: active,
            request_shutdown=lambda: shutdowns.append("shutdown"),
            poll_interval_s=0.005,
        )
    )
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(task), timeout=0.03)
    assert shutdowns == []
    assert not task.done()

    active = False
    await asyncio.wait_for(task, timeout=0.1)
    assert shutdowns == ["shutdown"]


@pytest.mark.asyncio
async def test_inactivity_monitor_honors_activity_reset() -> None:
    """Recent activity delays shutdown until the new idle window expires.

    :returns: None.
    """
    loop = asyncio.get_running_loop()
    last_activity = loop.time()
    shutdowns: list[str] = []

    task = asyncio.create_task(
        _run_inactivity_monitor(
            idle_timeout_s=0.06,
            get_last_activity=lambda: last_activity,
            has_active_work=lambda: False,
            request_shutdown=lambda: shutdowns.append("shutdown"),
            poll_interval_s=0.005,
        )
    )
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(task), timeout=0.03)
    last_activity = loop.time()
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(task), timeout=0.04)
    assert shutdowns == []
    assert not task.done()

    await asyncio.wait_for(task, timeout=0.1)
    assert shutdowns == ["shutdown"]


def test_parent_process_is_alive_detects_current_process() -> None:
    """The liveness helper recognizes the current process id.

    :returns: None.
    """
    assert _parent_process_is_alive(os.getpid())


def test_parent_is_orphaned_false_for_live_parent() -> None:
    """Not orphaned while our real parent is alive and unchanged.

    :returns: None.
    """
    assert _parent_is_orphaned(os.getppid()) is False


def test_parent_is_orphaned_true_when_reparented() -> None:
    """Reparenting reads as orphaned even when the old pid is reused.

    ``os.kill(old_pid, 0)`` can succeed against an unrelated process that
    recycled the dead parent's pid, so the liveness probe alone would
    never trip. A ``getppid()`` that no longer matches the launcher is the
    reliable, PID-reuse-immune orphan signal.

    :returns: None.
    """
    # A pid that is definitely not our real parent (simulates having been
    # reparented away from the launcher after it died).
    assert _parent_is_orphaned(os.getppid() + 100000) is True


def test_run_parent_death_killer_requests_shutdown_then_hard_exits() -> None:
    """On parent death the killer asks for graceful shutdown, then hard-exits.

    Runs with an already-orphaned parent pid, an injected exit function,
    and a tiny grace so the real ``os._exit`` never fires and the test
    stays fast. Order matters: graceful shutdown is requested before the
    hard-exit backstop so a healthy event loop can win the race.

    :returns: None.
    """
    events: list[str] = []
    exit_calls: list[int] = []

    _run_parent_death_killer(
        os.getppid() + 100000,  # already "orphaned"
        lambda: events.append("shutdown"),
        poll_interval_s=0.01,
        grace_s=0.01,
        exit_fn=lambda code: (events.append("exit"), exit_calls.append(code)),
    )

    assert events == ["shutdown", "exit"]
    assert exit_calls == [0]


def test_run_parent_death_killer_stands_down_when_adopted() -> None:
    """An adopted runner survives the launcher's exit instead of dying.

    Even with an already-orphaned parent pid (the launcher has gone),
    a set ``adopted`` event makes the killer return WITHOUT requesting
    shutdown or hard-exiting. This is the runner side of the adopt flow: on
    a tmux detach the CLI adopts the runner so it keeps serving the web
    UI rather than being torn down with the local terminal.

    :returns: None.
    """
    import threading

    events: list[str] = []
    adopted = threading.Event()
    adopted.set()

    _run_parent_death_killer(
        os.getppid() + 100000,  # already "orphaned": launcher gone
        lambda: events.append("shutdown"),
        adopted=adopted,
        poll_interval_s=0.01,
        grace_s=0.01,
        exit_fn=lambda code: events.append("exit"),
    )

    assert events == [], (
        f"an adopted runner must not tear down, but observed {events}; the "
        "web UI would lose a still-live agent on a clean detach"
    )


@pytest.mark.asyncio
async def test_runner_shutdown_closes_terminal_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The --server local runner shuts down terminal-owned resources.

    ``examples/databricks_coding_agent.yaml`` exposes terminal tools,
    and in ``omnigent run --server`` mode those terminals are owned
    by the local tunnel runner. This test drives the runner app
    startup/shutdown hooks directly and verifies shutdown includes the
    TerminalRegistry, not just harness subprocesses and MCPs.
    """
    import omnigent.runner._entry as entry_mod

    process_managers: list[_FakeProcessManager] = []
    terminal_registries: list[_TrackingTerminalRegistry] = []
    mcp_managers: list[_TrackingMcpManager] = []
    async_clients: list[_TrackingAsyncClient] = []
    sync_clients: list[_TrackingSyncClient] = []

    class _FakeProcessManager:
        def __init__(self) -> None:
            self.started = False
            self.shutdown_called = False
            process_managers.append(self)

        async def start(self) -> None:
            self.started = True

        async def shutdown(self) -> None:
            self.shutdown_called = True

    def _terminal_registry_factory(
        *,
        conversation_link_base_url: str | None = None,
    ) -> _TrackingTerminalRegistry:
        registry = _TrackingTerminalRegistry(
            conversation_link_base_url=conversation_link_base_url,
        )
        terminal_registries.append(registry)
        return registry

    def _mcp_manager_factory(stdio_cwd: Path | None = None) -> _TrackingMcpManager:
        manager = _TrackingMcpManager(stdio_cwd=stdio_cwd)
        mcp_managers.append(manager)
        return manager

    def _async_client_factory(*args: Any, **kwargs: Any) -> _TrackingAsyncClient:
        del args, kwargs
        client = _TrackingAsyncClient()
        async_clients.append(client)
        return client

    def _sync_client_factory(*args: Any, **kwargs: Any) -> _TrackingSyncClient:
        del args, kwargs
        client = _TrackingSyncClient()
        sync_clients.append(client)
        return client

    monkeypatch.setenv("RUNNER_SERVER_URL", "http://runner.test")
    monkeypatch.setattr(
        "omnigent.runtime.harnesses.process_manager.HarnessProcessManager",
        _FakeProcessManager,
    )
    monkeypatch.setattr(
        "omnigent.terminals.TerminalRegistry",
        _terminal_registry_factory,
    )
    monkeypatch.setattr(entry_mod.httpx, "AsyncClient", _async_client_factory)
    monkeypatch.setattr(entry_mod.httpx, "Client", _sync_client_factory)
    monkeypatch.setattr(entry_mod, "_make_auth_token_factory", lambda: None)
    monkeypatch.setattr(
        "omnigent.runner.identity.get_stable_runner_id",
        lambda: "runner-test-id",
    )

    app = entry_mod.create_app()
    # starlette 1.x removed Router.startup/shutdown; drive the lifespan instead.
    async with app.router.lifespan_context(app):
        pass

    assert process_managers and process_managers[0].shutdown_called
    assert terminal_registries and terminal_registries[0].shutdown_called
    assert terminal_registries[0].conversation_link_base_url == "http://runner.test"
    # In Omnigent mode (P1) the entry point passes mcp_manager=None; MCP calls are
    # routed per-session through ProxyMcpManager (runner/proxy_mcp_manager.py)
    # instead of a shared RunnerMcpManager. No RunnerMcpManager is created on
    # startup, so mcp_managers is empty — that is the correct post-P1 behavior.
    assert not mcp_managers, (
        "RunnerMcpManager should not be created by create_app() in Omnigent mode; "
        "MCP calls are proxied per-session through ProxyMcpManager"
    )
    assert async_clients and async_clients[0].closed
    # No sync httpx.Client is wired into the runner after the DBOS
    # removal — the legacy idle-sync client used by background
    # polling was deleted with that path. If a future change
    # reintroduces a sync client, the factory above will catch it
    # and the equivalent assertion can return.


def test_runner_workspace_from_env_returns_none_without_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runner workspace is optional process wiring.

    :param monkeypatch: Pytest environment patch fixture.
    :returns: None.
    """
    monkeypatch.delenv("OMNIGENT_RUNNER_WORKSPACE", raising=False)

    assert _runner_workspace_from_env() is None


def test_runner_workspace_from_env_rejects_empty_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Configured empty runner workspaces fail loud.

    :param monkeypatch: Pytest environment patch fixture.
    :returns: None.
    """
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", "  ")

    with pytest.raises(RuntimeError, match="must not be empty"):
        _runner_workspace_from_env()


def test_runner_workspace_from_env_resolves_value(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Configured runner workspace is normalized to an absolute path.

    :param monkeypatch: Pytest environment patch fixture.
    :param tmp_path: Temporary workspace path.
    :returns: None.
    """
    workspace = tmp_path / "project"
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", f" {workspace} ")

    assert _runner_workspace_from_env() == workspace.resolve()


@pytest.mark.asyncio
async def test_resolve_agent_spec_from_server_returns_none_for_404(
    tmp_path: Path,
) -> None:
    """A missing agent is the only non-200 status mapped to ``None``.

    :param tmp_path: Temporary spec cache root.
    :returns: None.
    """
    requested_paths: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        """
        Return a 404 response and record the requested path.

        :param request: Incoming mocked HTTP request.
        :returns: A 404 response.
        """
        requested_paths.append(request.url.path)
        return httpx.Response(404)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_handler),
        base_url="http://server.test",
    ) as client:
        spec = await _resolve_agent_spec_from_server(
            client, tmp_path, "ag_missing", session_id="conv_test"
        )

    assert spec is None
    assert requested_paths == ["/v1/sessions/conv_test/agent/contents"]
    # 404 should not create a cache directory because no bundle
    # was present to extract.
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_resolve_agent_spec_from_server_caches_success_by_agent_version(
    tmp_path: Path,
) -> None:
    """A successful bundle fetch is cached under agent id and version.

    :param tmp_path: Temporary spec cache root.
    :returns: None.
    """
    config_bytes = (
        b"spec_version: 1\nname: cached-agent\nexecutor:\n  config:\n    harness: claude-sdk\n"
    )
    bundle_buf = io.BytesIO()
    with tarfile.open(fileobj=bundle_buf, mode="w:gz") as tf:
        info = tarfile.TarInfo(name="config.yaml")
        info.size = len(config_bytes)
        tf.addfile(info, io.BytesIO(config_bytes))

    requested_paths: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        """
        Return a valid bundle once, then invalid bytes for the cached version.

        :param request: Incoming mocked HTTP request.
        :returns: A mocked successful bundle response.
        """
        requested_paths.append(request.url.path)
        if len(requested_paths) == 1:
            content = bundle_buf.getvalue()
        else:
            content = b"not a tarball"
        return httpx.Response(
            200,
            content=content,
            headers={"X-Agent-Version": "7"},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_handler),
        base_url="http://server.test",
    ) as client:
        first = await _resolve_agent_spec_from_server(
            client, tmp_path, "ag_cached", session_id="conv_test"
        )
        second = await _resolve_agent_spec_from_server(
            client, tmp_path, "ag_cached", session_id="conv_test"
        )

    assert first is not None
    assert second is not None
    assert first.name == "cached-agent"
    assert second.name == "cached-agent"
    assert requested_paths == [
        "/v1/sessions/conv_test/agent/contents",
        "/v1/sessions/conv_test/agent/contents",
    ]
    cache_dir = tmp_path / "ag_cached-v7"
    assert cache_dir.is_dir()
    assert (cache_dir / "config.yaml").read_text() == config_bytes.decode()
    assert [path.name for path in tmp_path.iterdir()] == ["ag_cached-v7"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 403, 500, 502])
async def test_resolve_agent_spec_from_server_raises_for_non_404_errors(
    tmp_path: Path,
    status_code: int,
) -> None:
    """Auth and server failures are not reported as missing agents.

    :param tmp_path: Temporary spec cache root.
    :param status_code: Non-404 HTTP status returned by the AP
        server.
    :returns: None.
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        """
        Return the parametrized server failure.

        :param request: Incoming mocked HTTP request.
        :returns: A response with ``status_code``.
        """
        assert request.url.path == "/v1/sessions/conv_test/agent/contents"
        return httpx.Response(status_code)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_handler),
        base_url="http://server.test",
    ) as client:
        with pytest.raises(RuntimeError) as exc_info:
            await _resolve_agent_spec_from_server(
                client, tmp_path, "ag_test", session_id="conv_test"
            )

    message = str(exc_info.value)
    assert f"HTTP {status_code}" in message
    assert "/v1/sessions/conv_test/agent/contents" in message


def test_main_reports_tunnel_rejection_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Fatal tunnel rejections are rendered as concise CLI errors.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param capsys: Pytest stdout/stderr capture fixture.
    :returns: None.
    """

    async def _raise_tunnel_rejection() -> None:
        """Raise the RuntimeError shape emitted by ``serve_tunnel``.

        :returns: None.
        :raises RuntimeError: Always, matching fatal server rejection.
        """
        raise RuntimeError(
            f"{RUNNER_TUNNEL_REJECTION_PREFIX}(HTTP 401); check remote server authentication"
        )

    monkeypatch.setattr(
        "omnigent.runner._entry._run_tunnel_from_env",
        _raise_tunnel_rejection,
    )

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 1
    stderr = capsys.readouterr().err
    # The CLI should expose the actionable rejection message without
    # dumping the asyncio/serve_tunnel traceback onto stderr.
    assert stderr == (
        f"error: {RUNNER_TUNNEL_REJECTION_PREFIX}(HTTP 401); check remote server authentication\n"
    )
    assert "Traceback" not in stderr


def test_main_installs_timestamped_runner_log_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Runner process logs include timestamps at the formatter boundary.

    Host-spawned runners redirect stderr to
    ``~/.omnigent/logs/host-runner/runner-*.log``. The entrypoint's
    logging formatter therefore has to include ``asctime`` globally; adding
    timestamps to individual messages would miss library and framework logs.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    captured: dict[str, Any] = {}

    def _capture_basic_config(**kwargs: Any) -> None:
        """
        Record the logging configuration requested by ``main``.

        :param kwargs: Keyword arguments passed to ``logging.basicConfig``.
        :returns: None.
        """
        captured.update(kwargs)

    async def _stop_immediately() -> None:
        """
        Let ``main`` return after installing logging.

        :returns: None.
        """

    monkeypatch.setattr(
        "omnigent.runner._entry.logging.basicConfig",
        _capture_basic_config,
    )
    monkeypatch.setattr(
        "omnigent.runner._entry._run_tunnel_from_env",
        _stop_immediately,
    )

    main()

    assert captured["level"] == logging.INFO
    assert captured["format"].startswith("%(asctime)s ")
    assert "%(levelname)s:%(name)s:%(message)s" in captured["format"]
    assert captured["datefmt"] == "%Y-%m-%dT%H:%M:%S%z"


def test_main_preserves_unexpected_runtime_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Unexpected runtime failures still propagate for traceback visibility.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param capsys: Pytest stdout/stderr capture fixture.
    :returns: None.
    """

    async def _raise_unexpected_runtime_error() -> None:
        """Raise a runtime error unrelated to tunnel rejection.

        :returns: None.
        :raises RuntimeError: Always, with an unexpected message.
        """
        raise RuntimeError("programming bug")

    monkeypatch.setattr(
        "omnigent.runner._entry._run_tunnel_from_env",
        _raise_unexpected_runtime_error,
    )

    with pytest.raises(RuntimeError, match="programming bug"):
        main()

    assert capsys.readouterr().err == ""


def test_make_auth_token_factory_resolves_sdk_auth_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The token factory resolves Databricks SDK auth ONCE and reuses it across
    every token fetch, instead of rebuilding ``Config`` per call.

    This is the regression guard for the per-request Databricks CLI auth tax
    (~0.5s/call) that dominated runner establish and per-turn latency on App
    backends: each token fetch used to build a fresh ``Config`` and shell out
    to ``databricks auth token``. If the factory regresses to per-call
    resolution, ``resolve_calls`` jumps from 1 to the number of invocations
    and this test fails.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    import omnigent.inner.databricks_executor as dbx

    class _CountingConfig:
        """Config double whose authenticate() counts calls."""

        def __init__(self) -> None:
            self.authenticate_calls = 0

        def authenticate(self) -> dict[str, str]:
            self.authenticate_calls += 1
            return {"Authorization": "Bearer tok-abc"}

    cfg = _CountingConfig()
    resolve_calls = {"n": 0}

    def _fake_resolve(profile: str | None = None) -> tuple[Any, str]:
        """Stand in for _resolve_databricks_auth; counts resolutions."""
        resolve_calls["n"] += 1
        return (
            dbx._DatabricksBearerAuth(cfg, profile_name="oss"),
            "https://ex.databricks.com",
        )

    monkeypatch.setattr(dbx, "_resolve_databricks_auth", _fake_resolve)
    # No stored OIDC token → the factory falls through to the SDK path.
    monkeypatch.setattr("omnigent.cli_auth.load_token", lambda _url: None)

    factory = _make_auth_token_factory(server_url="https://ex.databricks.com")
    assert factory is not None

    tokens = [factory() for _ in range(5)]

    # Bare bearer returned on every call (current_token strips the prefix).
    # A failure means the SDK path didn't supply the token through reuse.
    assert tokens == ["tok-abc"] * 5
    # THE FIX: SDK auth resolved exactly once for the probe + 5 calls. A
    # value > 1 means per-call resolution (the old _read_databrickscfg
    # behavior, i.e. the per-request CLI auth tax) has regressed.
    assert resolve_calls["n"] == 1, (
        f"_resolve_databricks_auth called {resolve_calls['n']}x; expected 1 "
        f"(resolve-once-and-cache). >1 means the per-request auth tax "
        f"regressed."
    )
    # authenticate() runs once per token fetch: the factory's own probe (1)
    # plus the 5 explicit calls = 6. These are cheap in-memory SDK cache
    # hits, NOT CLI shell-outs — that's the behavior the fix preserves.
    assert cfg.authenticate_calls == 6, (
        f"Expected 6 authenticate() calls (probe + 5), got {cfg.authenticate_calls}."
    )


@pytest.mark.parametrize(
    "agent_id,version",
    [
        ("../../etc/cron.d/evil", "0"),  # parent traversal via separators
        ("/etc/passwd", "0"),  # absolute-looking id
        ("..", "0"),  # bare dot-dot
        ("a/../../b", "0"),  # mixed traversal
        ("back\\..\\..\\slash", "0"),  # backslash separators
        ("normal_id", "../../etc"),  # traversal via the X-Agent-Version header
        ("normal_id", "../sibling"),  # version escapes one level up
    ],
)
def test_agent_cache_dest_contains_traversal(tmp_path: Path, agent_id: str, version: str) -> None:
    """A crafted agent_id or version cannot place the cache dir outside the root.

    The runner builds the per-agent cache directory from the (server-provided
    but untrusted) agent_id and ``X-Agent-Version`` header. If separators/`..`
    in *either* field are not neutralized,
    ``spec_cache_root / f"{agent_id}-v{version}"`` could escape the cache root
    and let a bundle write be redirected onto an arbitrary filesystem location.
    _agent_cache_dest must keep the result inside spec_cache_root regardless of
    which field carries the traversal.

    :param tmp_path: Cache root fixture.
    :param agent_id: An agent id under test (traversal-laden or normal).
    :param version: A version string under test (traversal-laden or normal).
    """
    cache_root = (tmp_path / "specs").resolve()
    cache_root.mkdir()

    dest = _agent_cache_dest(cache_root, agent_id, version)

    # Containment is the whole point: a failure here means the separator
    # stripping + is_relative_to guard regressed and a crafted id escaped
    # the cache root (the path-injection the fix closes).
    assert dest.is_relative_to(cache_root), f"{dest} escaped {cache_root}"
    # Separators are neutralized, so the cache dir is a single child of the
    # root (no nested/parent components survive).
    assert dest.parent == cache_root


def test_agent_cache_dest_normal_id_round_trips(tmp_path: Path) -> None:
    """A normal agent id/version maps to the expected child directory.

    Proves the sanitization doesn't mangle well-formed ids — only the
    f-string shape changes for traversal inputs.

    :param tmp_path: Cache root fixture.
    """
    cache_root = (tmp_path / "specs").resolve()
    cache_root.mkdir()

    dest = _agent_cache_dest(cache_root, "ag_abc123", "3")

    assert dest == cache_root / "ag_abc123-v3"

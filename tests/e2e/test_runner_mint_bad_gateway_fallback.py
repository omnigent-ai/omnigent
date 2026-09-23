"""End-to-end regression test: a 502 on the managed-mint endpoint.

Scenario: ``POST /v1/runners/{id}/token`` (the managed-mint endpoint) is
answered with **502 Bad Gateway** by an intermediary (e.g. Databricks Apps
relay), which would permanently brick the runner.  The 502 falls through every branch in
``_ManagedMintTokenFactory.__call__`` and returns ``None`` without latching
``declined``, so ``_RunnerDatabricksAuth.auth_flow`` raises
``httpx.RequestError("Databricks token refresh returned no token")`` on every
subsequent callback — bricking the session at spec resolve.

Reproduction:

1. Start a fake mint endpoint that always returns 502 (standing in for the
   intermediary that never lets the request reach uvicorn).
2. Construct ``_ManagedMintTokenFactory`` pointed at it (no previously-cached
   token, mimicking a cold-start runner).
3. Call the factory — must not permanently raise / must not leave the runner
   unable to proceed.  Specifically, either:
   a. ``declined`` is latched → ``auth_flow`` sends bare requests (non-blocking),
      OR
   b. the factory propagates the transient failure in a way that does not raise
      from ``auth_flow`` (e.g. ``declined`` is treated as True on a 5xx so the
      runner falls through to bare requests).
4. Drive ``_RunnerDatabricksAuth.auth_flow`` — the bug is the raise; the fix
   must make it yield the request (bare or with token) instead.

The fake 502 handler mimics what the bug report's evidence trail shows: the
intermediary answers with an HTML ``502 Bad Gateway`` page that never reaches
the uvicorn process.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from omnigent.runner._entry import _ManagedMintTokenFactory, _RunnerDatabricksAuth

# ---------------------------------------------------------------------------
# Fake 502 server (stands in for the relay / intermediary)
# ---------------------------------------------------------------------------


class _502Handler(BaseHTTPRequestHandler):
    """Return 502 Bad Gateway for every request, like the Apps relay would."""

    def log_message(self, format: str, *args: object) -> None:
        """Suppress default request logging."""
        del format, args

    def do_POST(self) -> None:
        """Respond with 502 to any POST."""
        body = b"<html><body>Bad Gateway</body></html>"
        self.send_response(502)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def bad_gateway_server() -> object:
    """Spin up a ThreadingHTTPServer that always responds 502.

    Yields the base URL of the server.  Shuts down after the test.
    """
    server = ThreadingHTTPServer(("127.0.0.1", 0), _502Handler)
    port = server.server_port
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_mint_502_does_not_brick_runner(bad_gateway_server: str) -> None:
    """A 502 from an intermediary must not brick the runner.

    The factory must either latch ``declined`` (so ``auth_flow`` sends bare
    requests) or otherwise avoid raising ``httpx.RequestError`` from
    ``auth_flow``.  The pre-fix behaviour is that ``declined`` is never set,
    ``__call__`` returns ``None``, and ``auth_flow`` raises immediately.

    :param bad_gateway_server: Base URL of the fake 502 server fixture.
    :returns: None.
    """
    fake_runner_id = "runner_token_deadbeef0000000000000000"
    mint_url = f"{bad_gateway_server}/v1/runners/{fake_runner_id}/token"

    factory = _ManagedMintTokenFactory(
        mint_url=mint_url,
        server_url=bad_gateway_server,
        binding_token="test-binding-token",
    )

    # Drive the factory: the mint endpoint returns 502 (no cached token).
    result = factory()

    # The factory must have latched declined (or proxy_auth_failed) so that
    # auth_flow can decide what to do rather than raising blindly.
    #
    # Pre-fix: both are False, result is None, and auth_flow raises.
    # Post-fix: declined is True (or an equivalent non-raise path is taken).
    assert factory.declined or factory.proxy_auth_failed, (
        f"_ManagedMintTokenFactory did not latch declined or proxy_auth_failed "
        f"after a 502 from the mint endpoint. "
        f"declined={factory.declined}, proxy_auth_failed={factory.proxy_auth_failed}, "
        f"result={result!r}. "
        "This means auth_flow will raise httpx.RequestError on every callback, "
        "bricking the runner at spec resolve."
    )


def test_auth_flow_does_not_raise_after_mint_502(bad_gateway_server: str) -> None:
    """auth_flow must not raise RequestError when the mint endpoint returns 502.

    This is the exact failure path from the bug report:
      runner calls auth_flow → factory() returns None (502 not latched) →
      auth_flow raises httpx.RequestError("Databricks token refresh returned
      no token") → spec_resolver fails → session is bricked.

    :param bad_gateway_server: Base URL of the fake 502 server fixture.
    :returns: None.
    """
    fake_runner_id = "runner_token_deadbeef0000000000000000"
    mint_url = f"{bad_gateway_server}/v1/runners/{fake_runner_id}/token"

    factory = _ManagedMintTokenFactory(
        mint_url=mint_url,
        server_url=bad_gateway_server,
        binding_token="test-binding-token",
    )

    # Prime the factory (calls the 502 endpoint).
    factory()

    auth = _RunnerDatabricksAuth(factory)
    req = httpx.Request(
        "GET",
        f"{bad_gateway_server}/v1/sessions/test_session_id/agent/contents",
    )
    gen = auth.auth_flow(req)

    # Pre-fix: raises httpx.RequestError immediately.
    # Post-fix: yields the request (bare, since declined is latched).
    try:
        out_req = next(gen)
    except httpx.RequestError as exc:
        pytest.fail(
            f"auth_flow raised httpx.RequestError after a 502 mint response: {exc}. "
            "The runner is bricked and cannot resolve its spec. "
            "Expected auth_flow to yield a bare request instead (declined latched)."
        )

    # After the fix the request must be yielded bare (no Authorization header),
    # because declined is set and the runner correctly falls back to bare requests
    # rather than failing closed.
    assert out_req is req, "auth_flow must yield back the original request"
    assert "Authorization" not in out_req.headers, (
        "auth_flow must send a bare request when mint is definitively declined "
        "(no-auth/header-mode local server), not inject a stale or empty bearer"
    )

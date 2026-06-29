"""S1: the harness ``/v1`` control channel is gated by a per-spawn bearer token.

On Windows the harness IPC is a loopback-TCP listener reachable by any local
process (POSIX uses a uid-isolated Unix socket), so ``process_manager`` mints a
per-spawn token, ships it to the harness via its private env, and presents it on
every request. The scaffold rejects ``/v1`` requests whose bearer token does not
match. The gate is keyed on ``app.state.harness_auth_token`` so it is inert when
no token is configured (POSIX, or an app built directly in a test) — see
``omnigent/runtime/harnesses/_scaffold.py``.

This test builds the scaffold app directly (no ``/tmp`` socket manager), so it
runs on every platform.
"""

from __future__ import annotations

from starlette.testclient import TestClient

from tests.runtime.harnesses._test_scaffold_harnesses import _EchoHarness

_URL = "/v1/sessions/conv_x/events"
_BODY = {"type": "interrupt"}  # simplest inbound event; needs no in-flight turn


def _build_app() -> object:
    app = _EchoHarness().build()
    app.state.conversation_id = "conv_x"
    return app


def test_v1_is_inert_without_a_configured_token() -> None:
    """No token on app.state (POSIX / direct embedder) -> /v1 is not gated."""
    app = _build_app()
    with TestClient(app) as client:
        # Reaches the handler (404: interrupt with no in-flight turn), not 401.
        assert client.post(_URL, json=_BODY).status_code != 401


def test_v1_requires_the_bearer_token_when_configured() -> None:
    """Token on app.state (Windows) -> /v1 demands a matching bearer token."""
    app = _build_app()
    app.state.harness_auth_token = "s3cret-token"
    with TestClient(app) as client:
        # Missing and wrong tokens are rejected before any turn logic runs.
        assert client.post(_URL, json=_BODY).status_code == 401
        assert (
            client.post(_URL, json=_BODY, headers={"Authorization": "Bearer nope"}).status_code
            == 401
        )
        # A bad scheme is rejected too.
        assert (
            client.post(_URL, json=_BODY, headers={"Authorization": "s3cret-token"}).status_code
            == 401
        )
        # The correct token passes the gate (404 from the handler, not 401).
        ok = client.post(_URL, json=_BODY, headers={"Authorization": "Bearer s3cret-token"})
        assert ok.status_code != 401


def test_health_probe_is_never_gated() -> None:
    """``GET /health`` stays open for liveness even when a token is configured."""
    app = _build_app()
    app.state.harness_auth_token = "s3cret-token"
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200

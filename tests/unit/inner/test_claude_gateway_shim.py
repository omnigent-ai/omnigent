"""Tests for the Claude gateway shim (thinking.display restoration).

The shim exists because Claude CLIs ≥ 2.1.168 strip ``thinking.display``
from ``/v1/messages`` bodies when experimental betas are disabled — the
required configuration on the Databricks gateway — which silences all
Opus thinking output (Opus 4.7+ defaults ``display`` to ``"omitted"``).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
import uvicorn

from omnigent.inner.claude_gateway_shim import (
    ClaudeGatewayShim,
    restore_thinking_display,
)

# ── restore_thinking_display unit tests ───────────────────


def _body(model: str, thinking: dict[str, Any] | None) -> bytes:  # type: ignore[explicit-any]  # thinking mirrors the API's open dict
    """
    Build a minimal Messages API request body.

    :param model: Model id, e.g. ``"databricks-claude-opus-4-8"``.
    :param thinking: Thinking config dict, e.g. ``{"type": "adaptive"}``,
        or ``None`` to omit the key entirely.
    :returns: Encoded JSON body.
    """
    payload: dict[str, Any] = {  # type: ignore[explicit-any]  # request body is a heterogeneous JSON object
        "model": model,
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "hi"}],
    }
    if thinking is not None:
        payload["thinking"] = thinking
    return json.dumps(payload).encode()


@pytest.mark.parametrize(
    "model,thinking",
    [
        # Opus + adaptive missing display — the live bug shape.
        ("databricks-claude-opus-4-8", {"type": "adaptive"}),
        # Opus + enabled missing display — any non-disabled type qualifies.
        ("databricks-claude-opus-4-7", {"type": "enabled", "budget_tokens": 1024}),
        # Fable shares Opus 4.7+'s display="omitted" default.
        ("databricks-claude-fable-5", {"type": "adaptive"}),
    ],
)
def test_restore_injects_display_for_adaptive_tiers(model: str, thinking: dict[str, Any]) -> None:  # type: ignore[explicit-any]  # parametrized API dict
    """Qualifying opus/fable bodies gain ``display="summarized"``; the rest
    of the payload is preserved byte-for-byte after re-encoding."""
    result = json.loads(restore_thinking_display(_body(model, thinking)))
    # display injected — without it the model defaults to "omitted" and
    # streams no thinking text (the user-visible bug).
    assert result["thinking"]["display"] == "summarized"
    # Original thinking fields survive alongside the injected display.
    assert result["thinking"]["type"] == thinking["type"]
    # Unrelated request fields are untouched by the rewrite.
    assert result["model"] == model
    assert result["messages"] == [{"role": "user", "content": "hi"}]


@pytest.mark.parametrize(
    "model,thinking",
    [
        # Existing display must be respected (a fixed future CLI wins).
        ("databricks-claude-opus-4-8", {"type": "adaptive", "display": "omitted"}),
        # Disabled thinking has nothing to display.
        ("databricks-claude-opus-4-8", {"type": "disabled"}),
        # Sonnet streams visible thinking by default — never touched.
        ("databricks-claude-sonnet-4-6", {"type": "adaptive"}),
        # First-party ids (no databricks- prefix) are out of scope.
        ("claude-opus-4-8", {"type": "adaptive"}),
        ("claude-fable-5", {"type": "adaptive"}),
        # No thinking key at all — nothing to patch.
        ("databricks-claude-opus-4-8", None),
    ],
)
def test_restore_leaves_non_qualifying_bodies_unchanged(
    model: str,
    thinking: dict[str, Any] | None,  # type: ignore[explicit-any]  # parametrized API dict
) -> None:
    """Bodies outside the opus-missing-display shape pass through unchanged."""
    body = _body(model, thinking)
    assert restore_thinking_display(body) == body


@pytest.mark.parametrize(
    "body",
    [
        b"not json at all",
        b'"a json string"',
        b'["a", "json", "array"]',
        json.dumps({"model": "databricks-claude-opus-4-8", "thinking": "adaptive"}).encode(),
    ],
)
def test_restore_passes_through_unparseable_bodies(body: bytes) -> None:
    """Non-object JSON, invalid JSON, and non-dict thinking are forwarded
    verbatim rather than raising — the upstream owns request validation."""
    assert restore_thinking_display(body) == body


# ── shim integration tests (real local upstream) ──────────


@dataclass
class _CapturedRequest:
    """
    One request observed by the recording upstream.

    :param method: HTTP method, e.g. ``"POST"``.
    :param path: Request path, e.g. ``"/v1/messages"``.
    :param headers: Lower-cased header name → value.
    :param body: Raw request body bytes.
    """

    method: str
    path: str
    headers: dict[str, str]
    body: bytes


@dataclass
class _RecordingUpstream:
    """
    Minimal real ASGI server standing in for the Anthropic gateway.

    Records every request and answers ``POST /v1/messages`` with a
    small SSE stream (mirroring the real streaming endpoint); other
    paths get a JSON body.
    """

    requests: list[_CapturedRequest] = field(default_factory=list)
    _server: uvicorn.Server | None = None
    _task: asyncio.Task[None] | None = None
    port: int | None = None

    async def _app(self, scope, receive, send) -> None:  # type: ignore[no-untyped-def]  # ASGI protocol is untyped dicts
        """
        Record the request and reply.

        :param scope: ASGI HTTP scope.
        :param receive: ASGI receive callable.
        :param send: ASGI send callable.
        """
        if scope["type"] != "http":
            return
        body = bytearray()
        while True:
            message = await receive()
            body.extend(message.get("body", b""))
            if not message.get("more_body", False):
                break
        self.requests.append(
            _CapturedRequest(
                method=scope["method"],
                path=scope["path"],
                headers={
                    k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]
                },
                body=bytes(body),
            )
        )
        if scope["method"] == "POST" and scope["path"] == "/v1/messages":
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"text/event-stream")],
                }
            )
            for event in (b"event: ping\ndata: {}\n\n", b"data: [DONE]\n\n"):
                await send({"type": "http.response.body", "body": event, "more_body": True})
            await send({"type": "http.response.body", "body": b""})
        else:
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send({"type": "http.response.body", "body": b'{"upstream": "ok"}'})

    async def start(self) -> str:
        """
        Serve on an ephemeral loopback port.

        :returns: The upstream base URL, e.g. ``"http://127.0.0.1:49153"``.
        """
        config = uvicorn.Config(
            self._app,
            host="127.0.0.1",
            port=0,
            log_level="warning",
            lifespan="off",
            interface="asgi3",  # bound methods defeat uvicorn's auto-detection
        )
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve())
        while not self._server.started:
            if self._task.done():
                self._task.result()
                raise OSError("recording upstream exited before startup")
            await asyncio.sleep(0.01)
        self.port = self._server.servers[0].sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{self.port}"

    async def stop(self) -> None:
        """Shut the server down and reap its serve task."""
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            await asyncio.wait_for(self._task, timeout=5.0)


@pytest.fixture
async def upstream() -> Any:  # type: ignore[explicit-any]  # async generator fixture; pytest infers the yield type
    """Run a recording upstream for the duration of one test."""
    server = _RecordingUpstream()
    await server.start()
    yield server
    await server.stop()


@pytest.mark.asyncio
async def test_shim_patches_opus_messages_body_and_streams_response(upstream) -> None:  # type: ignore[no-untyped-def]  # fixture type owned by pytest
    """An opus ``/v1/messages`` POST through the shim reaches the
    upstream with ``display`` injected, and the SSE response streams
    back byte-identical."""
    shim = ClaudeGatewayShim(upstream_base_url=f"http://127.0.0.1:{upstream.port}")
    await shim.start()
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{shim.base_url}/v1/messages",
                content=_body("databricks-claude-opus-4-8", {"type": "adaptive"}),
                headers={"authorization": "Bearer test-token"},
            )
        # SSE body round-trips through the shim unmodified — a buffering
        # or truncation bug in the relay would corrupt this.
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "text/event-stream"
        assert resp.text == "event: ping\ndata: {}\n\ndata: [DONE]\n\n"

        assert len(upstream.requests) == 1
        seen = upstream.requests[0]
        # The patch is the whole point of the shim: without it the
        # upstream body has no display and opus streams no thinking.
        upstream_body = json.loads(seen.body)
        assert upstream_body["thinking"] == {
            "type": "adaptive",
            "display": "summarized",
        }
        # Auth must pass through verbatim or every request 401s.
        assert seen.headers["authorization"] == "Bearer test-token"
    finally:
        await shim.aclose()


@pytest.mark.asyncio
async def test_shim_forwards_non_opus_and_non_messages_traffic_verbatim(upstream) -> None:  # type: ignore[no-untyped-def]  # fixture type owned by pytest
    """Sonnet bodies and non-/v1/messages requests are proxied untouched."""
    shim = ClaudeGatewayShim(upstream_base_url=f"http://127.0.0.1:{upstream.port}")
    await shim.start()
    try:
        sonnet_body = _body("databricks-claude-sonnet-4-6", {"type": "adaptive"})
        async with httpx.AsyncClient() as client:
            await client.post(f"{shim.base_url}/v1/messages", content=sonnet_body)
            other = await client.get(f"{shim.base_url}/v1/models?limit=5")

        # Sonnet body forwarded byte-for-byte — an over-broad patch that
        # touched sonnet would change its (working) thinking behavior.
        assert upstream.requests[0].body == sonnet_body
        # Non-messages path + query string forwarded as-is.
        assert upstream.requests[1].method == "GET"
        assert upstream.requests[1].path == "/v1/models"
        assert other.json() == {"upstream": "ok"}
    finally:
        await shim.aclose()


@pytest.mark.asyncio
async def test_shim_returns_502_when_upstream_unreachable() -> None:
    """Upstream connection failures surface as a 502 with an Anthropic-
    shaped error body instead of hanging or crashing the shim."""
    # Port 9 (discard) on loopback is closed — connect fails immediately.
    shim = ClaudeGatewayShim(upstream_base_url="http://127.0.0.1:9")
    await shim.start()
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{shim.base_url}/v1/messages",
                content=_body("databricks-claude-opus-4-8", {"type": "adaptive"}),
            )
        assert resp.status_code == 502
        # Anthropic error shape lets the CLI render a real API error.
        assert resp.json()["type"] == "error"
    finally:
        await shim.aclose()


@pytest.mark.asyncio
async def test_shim_does_not_install_process_signal_handlers() -> None:
    """The shim's uvicorn server must not swap the process's
    SIGINT/SIGTERM handlers — the harness subprocess's own uvicorn
    server owns those for graceful shutdown (_runner.py). A stock
    ``uvicorn.Server`` here replaces the handlers for the shim's whole
    lifetime, silently breaking harness SIGTERM shutdown."""
    import signal

    before = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    shim = ClaudeGatewayShim(upstream_base_url="http://127.0.0.1:9")
    await shim.start()
    try:
        after = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        # Identical handler objects — the shim never touched them.
        assert after == before
    finally:
        await shim.aclose()


# ── executor wiring test ──────────────────────────────────


@pytest.mark.asyncio
async def test_gateway_executor_routes_new_client_through_shim(monkeypatch) -> None:  # type: ignore[no-untyped-def]  # pytest fixture
    """On the gateway path, a new SDK client's env must point at the
    local shim, and the shim must target the original gateway URL.

    Follows this suite's established stub-SDK pattern for driving
    ``_get_or_create_client`` (the seam where ``options.env`` is
    consumed); spawning the real CLI is infeasible in unit tests.
    """
    from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor
    from omnigent.inner.databricks_executor import DatabricksCredentials

    monkeypatch.setattr(
        "omnigent.inner.databricks_executor._read_databrickscfg",
        lambda profile=None: DatabricksCredentials(
            host="https://example.databricks.com", token="dapi_test_token"
        ),
    )
    executor = ClaudeSDKExecutor(gateway=True)

    connect_env: dict[str, str] = {}

    class _StubClient:
        """Captures ``options.env`` at connect time (= CLI spawn time)."""

        def __init__(self, options) -> None:  # type: ignore[no-untyped-def]  # SDK options shape owned by stub
            self.options = options
            self._query = None
            self._transport = None

        async def connect(self) -> None:
            """Snapshot the env the CLI subprocess would receive."""
            connect_env.update(self.options.env)

        async def disconnect(self) -> None:
            """No-op disconnect for teardown."""

    class _StubSDK:
        """Minimal SDK namespace exposing ClaudeSDKClient."""

        ClaudeSDKClient = _StubClient

    from types import SimpleNamespace

    options = SimpleNamespace(
        env={"ANTHROPIC_BASE_URL": "https://example.databricks.com/ai-gateway/anthropic"},
        stderr=None,
    )
    try:
        await executor._get_or_create_client(
            _StubSDK,  # type: ignore[arg-type]  # stub stands in for the claude_agent_sdk module
            session_key="wiring-test",
            options=options,
            model="databricks-claude-opus-4-8",
        )
        # The CLI must talk to the loopback shim, not the gateway
        # directly — otherwise its stripped body reaches the gateway
        # and opus thinking stays silent.
        assert connect_env["ANTHROPIC_BASE_URL"].startswith("http://127.0.0.1:")
        assert executor._gateway_shim is not None
        assert connect_env["ANTHROPIC_BASE_URL"] == executor._gateway_shim.base_url
    finally:
        if executor._gateway_shim is not None:
            await executor._gateway_shim.aclose()

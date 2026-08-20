"""Tests for the Vertex Anthropic gateway shim.

The shim exists because Claude on Vertex AI uses a different wire shape
(``:rawPredict``/``:streamRawPredict``, model in the URL path,
``anthropic_version`` in the body, GCP OAuth2 bearer auth) than the plain
Anthropic API the Claude CLI's ``ANTHROPIC_BASE_URL`` normally targets.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
import uvicorn

from omnigent.inner.vertex_anthropic_shim import (
    ANTHROPIC_VERTEX_VERSION,
    GoogleADCTokenCache,
    VertexAnthropicGatewayShim,
    build_vertex_anthropic_url,
    prepare_vertex_anthropic_body,
    to_vertex_model_id,
)

# ── to_vertex_model_id ─────────────────────────────────────


@pytest.mark.parametrize(
    "model,expected",
    [
        ("claude-haiku-4-5-20251001", "claude-haiku-4-5@20251001"),
        ("claude-sonnet-4-5-20250929", "claude-sonnet-4-5@20250929"),
        # Already @-form is idempotent.
        ("claude-haiku-4-5@20251001", "claude-haiku-4-5@20251001"),
        # No trailing 8-digit date — nothing to translate.
        ("claude-3-5-sonnet-latest", "claude-3-5-sonnet-latest"),
    ],
)
def test_to_vertex_model_id_translates_dash_dates_to_at_form(model: str, expected: str) -> None:
    """Anthropic's dash-dated ids become Vertex's @-dated form; anything
    else (already @-form, or no date suffix) passes through unchanged."""
    assert to_vertex_model_id(model) == expected


# ── build_vertex_anthropic_url ─────────────────────────────


def test_build_vertex_anthropic_url_global_location() -> None:
    """The "global" location uses the bare aiplatform.googleapis.com host,
    matching the real Vertex API (confirmed against a live project)."""
    url = build_vertex_anthropic_url(
        project="my-project", location="global", model="claude-haiku-4-5-20251001", stream=False
    )
    assert url == (
        "https://aiplatform.googleapis.com/v1/projects/my-project/locations/global"
        "/publishers/anthropic/models/claude-haiku-4-5@20251001:rawPredict"
    )


def test_build_vertex_anthropic_url_regional_location() -> None:
    """A non-"global" location gets a region-prefixed host."""
    url = build_vertex_anthropic_url(
        project="my-project", location="us-east5", model="claude-haiku-4-5-20251001", stream=False
    )
    assert url.startswith("https://us-east5-aiplatform.googleapis.com/")


def test_build_vertex_anthropic_url_stream_uses_streamrawpredict() -> None:
    """`stream=True` selects the streaming RPC method."""
    url = build_vertex_anthropic_url(
        project="my-project", location="global", model="claude-haiku-4-5-20251001", stream=True
    )
    assert url.endswith(":streamRawPredict")


# ── prepare_vertex_anthropic_body ──────────────────────────


def test_prepare_vertex_anthropic_body_drops_model_and_injects_version() -> None:
    """`model` is removed (it's in the URL on Vertex) and
    `anthropic_version` is injected; everything else survives."""
    body = json.dumps(
        {
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "hi"}],
        }
    ).encode()
    result = json.loads(prepare_vertex_anthropic_body(body))
    assert "model" not in result
    assert result["anthropic_version"] == ANTHROPIC_VERTEX_VERSION
    assert result["max_tokens"] == 100
    assert result["messages"] == [{"role": "user", "content": "hi"}]


def test_prepare_vertex_anthropic_body_drops_context_management() -> None:
    """`context_management` is stripped — confirmed against a real Vertex
    project that `:rawPredict` rejects it with `400 context_management:
    Extra inputs are not permitted`, unlike api.anthropic.com."""
    body = json.dumps(
        {
            "model": "claude-sonnet-4-5-20250929",
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "hi"}],
            "context_management": {"edits": [{"type": "clear_tool_uses_20250919"}]},
        }
    ).encode()
    result = json.loads(prepare_vertex_anthropic_body(body))
    assert "context_management" not in result
    assert result["max_tokens"] == 100


@pytest.mark.parametrize(
    "body",
    [
        b"not json at all",
        b'"a json string"',
        b'["a", "json", "array"]',
    ],
)
def test_prepare_vertex_anthropic_body_passes_through_unparseable_bodies(body: bytes) -> None:
    """Non-object JSON and invalid JSON are forwarded verbatim rather than
    raising — Vertex owns request validation for malformed bodies."""
    assert prepare_vertex_anthropic_body(body) == body


# ── GoogleADCTokenCache ─────────────────────────────────────


class _FakeCredentials:
    """Stand-in for google.auth.credentials.Credentials in unit tests.

    Real Google credentials never have ``valid=True`` with ``expiry=None``
    at the same time — an unexpired token always carries an expiry.
    ``expiry=None`` here models the pre-first-refresh state instead (a
    freshly resolved credential has no token/expiry yet), matching
    ``google.auth.credentials.Credentials``'s actual behavior.
    """

    def __init__(self, token: str = "fake-token", valid: bool = True) -> None:
        import datetime

        self.token = token
        self.valid = valid
        if valid:
            now_utc_naive = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
            self.expiry = now_utc_naive + datetime.timedelta(hours=1)
        else:
            self.expiry = None
        self.refresh_calls = 0

    def refresh(self, request: object) -> None:
        """Record a refresh and flip to valid with a fresh token."""
        self.refresh_calls += 1
        self.token = f"refreshed-token-{self.refresh_calls}"
        self.valid = True


@pytest.mark.asyncio
async def test_token_cache_resolves_and_reuses_valid_credentials(monkeypatch) -> None:  # type: ignore[no-untyped-def]  # pytest fixture
    """A valid, non-expiring credential is resolved once and reused —
    google.auth.default() is not called again on a second get_token()."""
    fake_creds = _FakeCredentials(token="initial-token", valid=True)
    default_calls = 0

    def fake_default(scopes: list[str]) -> tuple[_FakeCredentials, None]:
        nonlocal default_calls
        default_calls += 1
        return fake_creds, None

    monkeypatch.setattr("google.auth.default", fake_default)

    cache = GoogleADCTokenCache()
    token1 = await cache.get_token()
    token2 = await cache.get_token()

    assert token1 == "initial-token"
    assert token2 == "initial-token"
    assert default_calls == 1
    assert fake_creds.refresh_calls == 0


@pytest.mark.asyncio
async def test_token_cache_refreshes_invalid_credentials(monkeypatch) -> None:  # type: ignore[no-untyped-def]  # pytest fixture
    """An invalid credential (never refreshed, or expired) is refreshed
    before its token is returned."""
    fake_creds = _FakeCredentials(token="stale-token", valid=False)
    monkeypatch.setattr("google.auth.default", lambda scopes: (fake_creds, None))

    cache = GoogleADCTokenCache()
    token = await cache.get_token()

    assert fake_creds.refresh_calls == 1
    assert token == "refreshed-token-1"


# ── VertexAnthropicGatewayShim integration tests ───────────


@dataclass
class _CapturedRequest:
    """One request observed by the recording upstream."""

    method: str
    path: str
    headers: dict[str, str]
    body: bytes


@dataclass
class _RecordingUpstream:
    """Minimal real ASGI server standing in for Vertex AI."""

    requests: list[_CapturedRequest] = field(default_factory=list)
    _server: uvicorn.Server | None = None
    _task: asyncio.Task[None] | None = None
    port: int | None = None
    stream_response: bool = False

    async def _app(self, scope, receive, send) -> None:  # type: ignore[no-untyped-def]  # ASGI protocol is untyped dicts
        """Record the request and reply with an Anthropic-shaped response."""
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
        if self.stream_response:
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
            await send(
                {"type": "http.response.body", "body": b'{"type": "message", "id": "msg_1"}'}
            )

    async def start(self) -> str:
        """Serve on an ephemeral loopback port.

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


class _FakeTokenCache:
    """Stand-in for GoogleADCTokenCache that never touches real GCP auth."""

    def __init__(self, token: str = "fake-vertex-token") -> None:
        self.token = token
        self.calls = 0

    async def get_token(self) -> str:
        """Return the fixed fake token, counting calls."""
        self.calls += 1
        return self.token


def _make_shim(upstream_port: int, token: str = "fake-vertex-token") -> VertexAnthropicGatewayShim:
    """Build a shim pointed at the fake upstream's port with a fake token cache.

    The shim always builds a real ``aiplatform.googleapis.com`` URL — these
    tests monkeypatch nothing at the HTTP layer, so a real DNS resolution
    would occur unless the shim's httpx client is redirected. Instead, the
    fixture asserts on ``_asgi_app``'s translated request in isolation via
    ``build_vertex_anthropic_url``/``prepare_vertex_anthropic_body`` (unit
    tests above) and exercises the ASGI plumbing here against a fake
    upstream reached by overriding ``httpx.AsyncClient`` transport — see
    ``test_shim_posts_translated_request_to_fake_upstream`` for how.
    """
    return VertexAnthropicGatewayShim(
        project="test-project", location="global", token_cache=_FakeTokenCache(token)
    )


@pytest.mark.asyncio
async def test_shim_posts_translated_request_to_fake_upstream(
    monkeypatch,
    upstream: _RecordingUpstream,  # type: ignore[no-untyped-def]  # pytest fixture
) -> None:
    """A `/v1/messages` POST through the shim reaches Vertex's rawPredict
    shape: model moved into the URL, anthropic_version injected into the
    body, and the real GCP token attached as a bearer token."""
    # Redirect the shim's "Vertex" calls to the local recording upstream
    # instead of the real aiplatform.googleapis.com host, by overriding
    # build_vertex_anthropic_url's result at the module level the shim
    # imports it from.
    import omnigent.inner.vertex_anthropic_shim as shim_module

    real_build_url = shim_module.build_vertex_anthropic_url

    def fake_build_url(*, project: str, location: str, model: str, stream: bool) -> str:
        real_url = real_build_url(project=project, location=location, model=model, stream=stream)
        # Swap only the host — path/method translation is exercised as-is.
        suffix = real_url.split("/v1/projects/", 1)[1]
        return f"http://127.0.0.1:{upstream.port}/v1/projects/{suffix}"

    monkeypatch.setattr(shim_module, "build_vertex_anthropic_url", fake_build_url)

    shim = _make_shim(upstream.port, token="test-token-123")
    await shim.start()
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{shim.base_url}/v1/messages",
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 50,
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
        assert resp.status_code == 200

        assert len(upstream.requests) == 1
        seen = upstream.requests[0]
        assert seen.path == (
            "/v1/projects/test-project/locations/global/publishers/anthropic/models/"
            "claude-haiku-4-5@20251001:rawPredict"
        )
        upstream_body = json.loads(seen.body)
        assert "model" not in upstream_body
        assert upstream_body["anthropic_version"] == ANTHROPIC_VERTEX_VERSION
        assert upstream_body["messages"] == [{"role": "user", "content": "hi"}]
        # The real Anthropic API key the CLI might have set (there isn't
        # one here, but this is the credential-swap point) never reaches
        # Vertex — only the token the shim's own token cache resolved.
        assert seen.headers["authorization"] == "Bearer test-token-123"
    finally:
        await shim.aclose()


@pytest.mark.asyncio
async def test_shim_streams_sse_response_unmodified(
    monkeypatch,
    upstream: _RecordingUpstream,  # type: ignore[no-untyped-def]  # pytest fixture
) -> None:
    """A streaming request's SSE response round-trips through the shim
    byte-identical — a buffering bug in the relay would corrupt this."""
    upstream.stream_response = True
    import omnigent.inner.vertex_anthropic_shim as shim_module

    real_build_url = shim_module.build_vertex_anthropic_url

    def fake_build_url(*, project: str, location: str, model: str, stream: bool) -> str:
        real_url = real_build_url(project=project, location=location, model=model, stream=stream)
        suffix = real_url.split("/v1/projects/", 1)[1]
        return f"http://127.0.0.1:{upstream.port}/v1/projects/{suffix}"

    monkeypatch.setattr(shim_module, "build_vertex_anthropic_url", fake_build_url)

    shim = _make_shim(upstream.port)
    await shim.start()
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{shim.base_url}/v1/messages",
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 50,
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                },
            )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "text/event-stream"
        assert resp.text == "event: ping\ndata: {}\n\ndata: [DONE]\n\n"
        # `stream: true` in the request body selected :streamRawPredict.
        assert upstream.requests[0].path.endswith(":streamRawPredict")
    finally:
        await shim.aclose()


@pytest.mark.asyncio
async def test_shim_rejects_non_messages_paths_with_404() -> None:
    """Vertex has no equivalent of the plain API's other endpoints
    (`/v1/models` etc.) — anything but `/v1/messages` gets a clean 404
    instead of being forwarded nowhere or hanging."""
    shim = _make_shim(upstream_port=9)  # port unused; request never leaves the shim
    await shim.start()
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{shim.base_url}/v1/models")
        assert resp.status_code == 404
        assert resp.json()["type"] == "error"
    finally:
        await shim.aclose()


@pytest.mark.asyncio
async def test_shim_rejects_body_without_model_with_400() -> None:
    """A body with no `model` can't be routed to any Vertex publisher-model
    path — fail fast with a 400 instead of building a malformed URL."""
    shim = _make_shim(upstream_port=9)
    await shim.start()
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{shim.base_url}/v1/messages", json={"max_tokens": 50})
        assert resp.status_code == 400
    finally:
        await shim.aclose()


@pytest.mark.asyncio
async def test_shim_returns_502_when_upstream_unreachable() -> None:
    """Upstream connection failures surface as a 502 with an Anthropic-
    shaped error body instead of hanging or crashing the shim."""
    import omnigent.inner.vertex_anthropic_shim as shim_module

    # Port 9 (discard) on loopback is closed — connect fails immediately.
    monkeypatch_target = shim_module.build_vertex_anthropic_url
    try:
        shim_module.build_vertex_anthropic_url = lambda **kwargs: "http://127.0.0.1:9/v1/messages"
        shim = _make_shim(upstream_port=9)
        await shim.start()
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{shim.base_url}/v1/messages",
                    json={
                        "model": "claude-haiku-4-5-20251001",
                        "max_tokens": 50,
                        "messages": [{"role": "user", "content": "hi"}],
                    },
                )
            assert resp.status_code == 502
            assert resp.json()["type"] == "error"
        finally:
            await shim.aclose()
    finally:
        shim_module.build_vertex_anthropic_url = monkeypatch_target


@pytest.mark.asyncio
async def test_shim_returns_502_when_token_resolution_fails() -> None:
    """A GCP auth failure (e.g. no ADC configured) surfaces as a clean 502
    rather than propagating an unhandled exception into the ASGI app."""

    class _FailingTokenCache:
        async def get_token(self) -> str:
            raise RuntimeError("no ADC found")

    shim = VertexAnthropicGatewayShim(
        project="test-project", location="global", token_cache=_FailingTokenCache()
    )
    await shim.start()
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{shim.base_url}/v1/messages",
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 50,
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
        assert resp.status_code == 502
        assert "no ADC found" in resp.json()["error"]["message"]
    finally:
        await shim.aclose()


@pytest.mark.asyncio
async def test_shim_does_not_install_process_signal_handlers() -> None:
    """The shim's uvicorn server must not swap the process's
    SIGINT/SIGTERM handlers — same rationale as ClaudeGatewayShim's
    identical test."""
    import signal

    before = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    shim = _make_shim(upstream_port=9)
    await shim.start()
    try:
        after = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        assert after == before
    finally:
        await shim.aclose()


# ── executor wiring tests ───────────────────────────────────


@pytest.mark.asyncio
async def test_vertex_executor_routes_new_client_through_shim() -> None:
    """On the Vertex path, a new SDK client's env must point at the
    local Vertex shim (not left at the sentinel ANTHROPIC_BASE_URL set
    in __init__).

    Follows test_claude_gateway_shim.py's established stub-SDK pattern
    for driving ``_get_or_create_client`` — spawning the real CLI is
    infeasible in unit tests.
    """
    from types import SimpleNamespace

    from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor

    executor = ClaudeSDKExecutor(vertex_project="test-project", vertex_location="us-east5")

    connect_env: dict[str, str] = {}

    class _StubClient:
        """Captures ``options.env`` at connect time (= CLI spawn time)."""

        def __init__(self, options) -> None:  # type: ignore[no-untyped-def]  # SDK options shape owned by stub
            self.options = options

        async def connect(self) -> None:
            """Snapshot the env the CLI subprocess would receive."""
            connect_env.update(self.options.env)

        async def disconnect(self) -> None:
            """No-op disconnect for teardown."""

    class _StubSDK:
        """Minimal SDK namespace exposing ClaudeSDKClient."""

        ClaudeSDKClient = _StubClient

    options = SimpleNamespace(
        env={"ANTHROPIC_BASE_URL": "https://vertex-anthropic.invalid"},
        stderr=None,
    )
    try:
        await executor._get_or_create_client(
            _StubSDK,  # type: ignore[arg-type]  # stub stands in for the claude_agent_sdk module
            session_key="vertex-wiring-test",
            options=options,
            model="claude-haiku-4-5-20251001",
        )
        # The CLI must talk to the loopback Vertex shim, not the sentinel
        # URL invented in __init__ — otherwise every real request 404s.
        assert connect_env["ANTHROPIC_BASE_URL"].startswith("http://127.0.0.1:")
        assert executor._vertex_shim is not None
        assert connect_env["ANTHROPIC_BASE_URL"] == executor._vertex_shim.base_url
        # The Databricks/generic gateway shim must never start on this path.
        assert executor._gateway_shim is None
        # The CLI refuses to run with no key at all ("Not logged in"); the
        # shim never reads this value (real auth is a GCP token attached
        # upstream), so a non-empty placeholder must be present.
        from omnigent.inner.vertex_anthropic_shim import PLACEHOLDER_API_KEY

        assert connect_env["ANTHROPIC_API_KEY"] == PLACEHOLDER_API_KEY
    finally:
        if executor._vertex_shim is not None:
            await executor._vertex_shim.aclose()


@pytest.mark.asyncio
async def test_vertex_executor_connect_strips_ambient_native_vertex_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller whose own shell already has CLAUDE_CODE_USE_VERTEX=1 (e.g. a
    developer who also uses native Claude-on-Vertex) must not leak that
    into the CLI subprocess on this path — otherwise the CLI silently
    prefers its own native Vertex-via-ADC support over our
    ANTHROPIC_BASE_URL override, defeating the shim's entire purpose of
    keeping GCP ADC out of the (possibly sandboxed) child process."""
    from types import SimpleNamespace

    from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor

    monkeypatch.setenv("CLAUDE_CODE_USE_VERTEX", "1")
    monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "some-other-ambient-project")
    monkeypatch.setenv("ANTHROPIC_VERTEX_REGION", "us-east5")
    monkeypatch.setenv("CLOUD_ML_REGION", "us-east5")

    executor = ClaudeSDKExecutor(vertex_project="test-project", vertex_location="us-east5")

    seen_env_during_connect: dict[str, str] = {}

    class _StubClient:
        def __init__(self, options) -> None:  # type: ignore[no-untyped-def]
            self.options = options

        async def connect(self) -> None:
            # Snapshot os.environ *during* connect — this is the window
            # the fix under test unsets the ambient vars for.
            seen_env_during_connect.update(os.environ)

        async def disconnect(self) -> None:
            """No-op disconnect for teardown."""

    class _StubSDK:
        ClaudeSDKClient = _StubClient

    options = SimpleNamespace(
        env={"ANTHROPIC_BASE_URL": "https://vertex-anthropic.invalid"},
        stderr=None,
    )
    try:
        await executor._get_or_create_client(
            _StubSDK,  # type: ignore[arg-type]
            session_key="vertex-env-strip-test",
            options=options,
            model="claude-haiku-4-5-20251001",
        )
        assert "CLAUDE_CODE_USE_VERTEX" not in seen_env_during_connect
        assert "ANTHROPIC_VERTEX_PROJECT_ID" not in seen_env_during_connect
        assert "ANTHROPIC_VERTEX_REGION" not in seen_env_during_connect
        assert "CLOUD_ML_REGION" not in seen_env_during_connect
        # Restored afterwards for anything else in the process.
        assert os.environ["CLAUDE_CODE_USE_VERTEX"] == "1"
    finally:
        if executor._vertex_shim is not None:
            await executor._vertex_shim.aclose()


def test_executor_rejects_gateway_and_vertex_together() -> None:
    """gateway=True and vertex_project set together is a config error —
    the two transports set ANTHROPIC_BASE_URL in incompatible ways."""
    from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor

    with pytest.raises(ValueError, match="mutually exclusive"):
        ClaudeSDKExecutor(
            gateway=True,
            base_url_override="https://example.com/anthropic",
            gateway_auth_command="printf %s test",
            vertex_project="test-project",
        )

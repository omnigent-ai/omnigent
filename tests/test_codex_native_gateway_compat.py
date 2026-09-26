"""Tests for the codex-native Responses gateway compat proxy and its gating.

Non-OpenAI gateway models reject any Responses request carrying
``parallel_tool_calls``; codex always sends it and offers no way to omit it,
so a non-OpenAI launch must route through the loopback compat proxy that
strips the field before it reaches the gateway.
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from omnigent.harnesses.codex_native.app_server import (
    _provider_override_base_url,
    _rewrite_provider_override_base_url,
    build_codex_native_server,
)
from omnigent.harnesses.codex_native.gateway_compat import (
    CodexResponsesCompatProxy,
    rewritten_responses_request_body,
)
from omnigent.inner.codex_executor import _provider_codex_config_overrides

_NON_OPENAI_MODEL = "system.ai.qwen35-122b-a10b"

_SSE_PAYLOAD = (
    b'event: response.created\ndata: {"type": "response.created"}\n\n'
    b'event: response.completed\ndata: {"type": "response.completed"}\n\n'
)


def test_rewrite_strips_field_for_non_openai_model() -> None:
    raw = json.dumps(
        {
            "model": _NON_OPENAI_MODEL,
            "input": "hello",
            "parallel_tool_calls": False,
            "stream": True,
        }
    ).encode()

    rewritten = json.loads(rewritten_responses_request_body(raw))

    assert "parallel_tool_calls" not in rewritten
    assert rewritten["model"] == _NON_OPENAI_MODEL
    assert rewritten["input"] == "hello"
    assert rewritten["stream"] is True


@pytest.mark.parametrize("value", [True, False])
def test_rewrite_strips_field_whatever_its_value(value: bool) -> None:
    # Both gateway rejection shapes fire on the field's presence, not its
    # value (qwen/gemma reject the unknown field outright).
    raw = json.dumps({"model": _NON_OPENAI_MODEL, "parallel_tool_calls": value}).encode()

    assert b"parallel_tool_calls" not in rewritten_responses_request_body(raw)


def test_rewrite_strips_field_for_gpt_oss() -> None:
    # gpt-oss keeps the gpt- spelling but is Databricks-hosted; its gateway
    # rejects the field with a capability gate.
    raw = json.dumps({"model": "system.ai.gpt-oss-20b", "parallel_tool_calls": True}).encode()

    assert b"parallel_tool_calls" not in rewritten_responses_request_body(raw)


@pytest.mark.parametrize("model", ["gpt-5.5", "gpt-5.6-luna", "codex-5", "o3"])
def test_rewrite_keeps_body_for_openai_model(model: str) -> None:
    raw = json.dumps({"model": model, "parallel_tool_calls": True}).encode()

    assert rewritten_responses_request_body(raw) == raw


@pytest.mark.parametrize(
    "raw",
    [
        b"not json at all",
        b'["a", "list"]',
        json.dumps({"model": _NON_OPENAI_MODEL, "input": "no field"}).encode(),
        b"",
    ],
)
def test_rewrite_leaves_other_bodies_unchanged(raw: bytes) -> None:
    assert rewritten_responses_request_body(raw) == raw


class _RecordingGateway:
    """Upstream stand-in rejecting Responses requests that carry the field."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict[str, str], bytes]] = []
        self._lock = threading.Lock()
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            def _record(self) -> bytes:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                with gateway._lock:
                    gateway.requests.append(
                        (self.command, self.path, dict(self.headers.items()), body)
                    )
                return body

            def do_POST(self) -> None:
                body = self._record()
                try:
                    decoded = json.loads(body)
                except ValueError:
                    decoded = {}
                if isinstance(decoded, dict) and "parallel_tool_calls" in decoded:
                    self._reply(
                        400,
                        b'{"error_code": "BAD_REQUEST", "message": '
                        b'"Bad request: json: unknown field \\"parallel_tool_calls\\"\\n"}',
                        "application/json",
                    )
                    return
                if isinstance(decoded, dict) and decoded.get("stream"):
                    self._reply(200, _SSE_PAYLOAD, "text/event-stream")
                    return
                self._reply(
                    200, b'{"object": "response", "status": "completed"}', "application/json"
                )

            def do_GET(self) -> None:
                self._record()
                self._reply(200, b'{"object": "list", "data": []}', "application/json")

            def _reply(self, status: int, payload: bytes, content_type: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=5)


@pytest.fixture
def gateway() -> _RecordingGateway:
    upstream = _RecordingGateway()
    yield upstream
    upstream.close()


async def test_proxy_strips_field_so_the_gateway_serves_the_turn(
    gateway: _RecordingGateway,
) -> None:
    proxy = CodexResponsesCompatProxy(gateway.base_url)
    await proxy.start()
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{proxy.base_url}/responses",
                json={
                    "model": _NON_OPENAI_MODEL,
                    "input": "hello",
                    "parallel_tool_calls": False,
                },
                headers={"authorization": "Bearer test-token"},
            )
    finally:
        await proxy.aclose()

    assert response.status_code == 200
    method, path, headers, body = gateway.requests[0]
    assert (method, path) == ("POST", "/v1/responses")
    assert "parallel_tool_calls" not in json.loads(body)
    assert json.loads(body)["input"] == "hello"
    # Codex's own credential is forwarded verbatim; the proxy adds none.
    assert headers.get("authorization") == "Bearer test-token"


async def test_proxy_streams_sse_bytes_intact(gateway: _RecordingGateway) -> None:
    proxy = CodexResponsesCompatProxy(gateway.base_url)
    await proxy.start()
    try:
        async with httpx.AsyncClient() as client:
            async with client.stream(
                "POST",
                f"{proxy.base_url}/responses",
                json={
                    "model": _NON_OPENAI_MODEL,
                    "stream": True,
                    "parallel_tool_calls": False,
                },
            ) as response:
                payload = b"".join([chunk async for chunk in response.aiter_bytes()])
    finally:
        await proxy.aclose()

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/event-stream"
    assert payload == _SSE_PAYLOAD


async def test_proxy_forwards_openai_model_requests_unchanged(
    gateway: _RecordingGateway,
) -> None:
    # A mid-session switch onto OpenAI's own service keeps the field, which
    # is valid there; the stand-in gateway then rejects it, proving both the
    # unchanged forward and the error passthrough.
    proxy = CodexResponsesCompatProxy(gateway.base_url)
    await proxy.start()
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{proxy.base_url}/responses",
                json={"model": "gpt-5.5", "parallel_tool_calls": True},
            )
    finally:
        await proxy.aclose()

    assert response.status_code == 400
    assert "parallel_tool_calls" in json.loads(gateway.requests[0][3])
    assert response.json()["error_code"] == "BAD_REQUEST"


async def test_proxy_passes_other_paths_through(gateway: _RecordingGateway) -> None:
    proxy = CodexResponsesCompatProxy(gateway.base_url)
    await proxy.start()
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{proxy.base_url}/models")
    finally:
        await proxy.aclose()

    assert response.status_code == 200
    assert gateway.requests[0][:2] == ("GET", "/v1/models")
    assert response.json() == {"object": "list", "data": []}


async def test_proxy_aclose_is_idempotent(gateway: _RecordingGateway) -> None:
    proxy = CodexResponsesCompatProxy(gateway.base_url)
    await proxy.start()
    await proxy.aclose()
    await proxy.aclose()


def _provider_overrides(model: str | None, base_url: str) -> list[str]:
    return _provider_codex_config_overrides(
        model=model,
        base_url=base_url,
        auth_command="printf test-token",
        wire_api="responses",
    )


def test_rewrite_provider_override_points_at_the_proxy() -> None:
    overrides = _provider_overrides(_NON_OPENAI_MODEL, "http://up.example/v1")

    rewritten = _rewrite_provider_override_base_url(
        overrides,
        upstream="http://up.example/v1",
        proxy_base_url="http://127.0.0.1:5555",
    )

    assert _provider_override_base_url(rewritten) == "http://127.0.0.1:5555"
    # Everything but the base_url survives byte-for-byte.
    assert [o for o in rewritten if not o.startswith("model_providers.")] == [
        o for o in overrides if not o.startswith("model_providers.")
    ]
    (table,) = [o for o in rewritten if o.startswith("model_providers.")]
    assert "printf test-token" in table
    assert 'wire_api="responses"' in table


def test_rewrite_provider_override_leaves_other_base_urls_alone() -> None:
    overrides = _provider_overrides(_NON_OPENAI_MODEL, "http://other.example/v1")

    assert (
        _rewrite_provider_override_base_url(
            overrides,
            upstream="http://up.example/v1",
            proxy_base_url="http://127.0.0.1:5555",
        )
        == overrides
    )


@pytest.mark.parametrize(
    "model",
    [_NON_OPENAI_MODEL, "system.ai.gpt-oss-20b", "system.ai.llama-4-maverick"],
)
def test_build_fronts_non_openai_provider_launch_with_the_proxy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    model: str,
) -> None:
    monkeypatch.setattr(
        "omnigent.harnesses.codex_native.app_server._find_codex_cli",
        lambda: sys.executable,
    )

    app_server = build_codex_native_server(
        socket_path=tmp_path / "codex.sock",
        codex_home=tmp_path / "codex-home",
        cwd=tmp_path,
        model=model,
        profile=None,
        extra_config_overrides=_provider_overrides(model, "http://up.example/v1"),
        bridge_dir=tmp_path / "bridge",
        ap_server_url=None,
        ap_auth_headers={},
    )

    assert app_server.gateway_compat_upstream == "http://up.example/v1"


@pytest.mark.parametrize("model", ["gpt-5.5", "databricks-gpt-5-6-luna", "codex-5"])
def test_build_keeps_openai_model_launch_direct(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    model: str,
) -> None:
    monkeypatch.setattr(
        "omnigent.harnesses.codex_native.app_server._find_codex_cli",
        lambda: sys.executable,
    )

    app_server = build_codex_native_server(
        socket_path=tmp_path / "codex.sock",
        codex_home=tmp_path / "codex-home",
        cwd=tmp_path,
        model=model,
        profile=None,
        extra_config_overrides=_provider_overrides(model, "http://up.example/v1"),
        bridge_dir=tmp_path / "bridge",
        ap_server_url=None,
        ap_auth_headers={},
    )

    assert app_server.gateway_compat_upstream is None


def test_build_keeps_unresolved_model_launch_direct(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Codex's own default (no resolved model) is left untouched, matching
    # the web_search gate's posture.
    monkeypatch.setattr(
        "omnigent.harnesses.codex_native.app_server._find_codex_cli",
        lambda: sys.executable,
    )

    app_server = build_codex_native_server(
        socket_path=tmp_path / "codex.sock",
        codex_home=tmp_path / "codex-home",
        cwd=tmp_path,
        model=None,
        profile=None,
        extra_config_overrides=_provider_overrides(None, "http://up.example/v1"),
        bridge_dir=tmp_path / "bridge",
        ap_server_url=None,
        ap_auth_headers={},
    )

    assert app_server.gateway_compat_upstream is None


@pytest.mark.parametrize(
    ("resolved_model", "expect_proxy"),
    [
        pytest.param("system.ai.qwen35-122b-a10b", True, id="qwen"),
        pytest.param("system.ai.gpt-oss-20b", True, id="gpt-oss-default"),
        pytest.param("databricks-gpt-5-6-luna", False, id="gpt"),
    ],
)
def test_build_profile_launch_proxy_follows_resolved_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    resolved_model: str,
    expect_proxy: bool,
) -> None:
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server
    from omnigent.inner.codex_executor import _databricks_codex_config_overrides

    monkeypatch.setattr(
        "omnigent.harnesses.codex_native.app_server._find_codex_cli",
        lambda: sys.executable,
    )
    monkeypatch.setattr(
        codex_native_app_server,
        "_databricks_launch_materialization",
        lambda *, model, profile, codex_path: (
            codex_native_app_server._DatabricksLaunchMaterialization(
                config_overrides=_databricks_codex_config_overrides(
                    model=resolved_model,
                    base_url="https://ws.example/ai-gateway/codex/v1",
                    auth_command="printf test-token",
                    auth_refresh_interval_ms=None,
                ),
                model=resolved_model,
                host="https://ws.example",
            )
        ),
    )

    app_server = build_codex_native_server(
        socket_path=tmp_path / "codex.sock",
        codex_home=tmp_path / "codex-home",
        cwd=tmp_path,
        model=None,
        profile="oss",
        bridge_dir=tmp_path / "bridge",
        ap_server_url=None,
        ap_auth_headers={},
    )

    expected = "https://ws.example/ai-gateway/codex/v1" if expect_proxy else None
    assert app_server.gateway_compat_upstream == expected

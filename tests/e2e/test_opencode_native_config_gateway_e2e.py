"""E2E repro: opencode-native ignores ``~/.omnigent/config.yaml`` gateway providers.

A default ``gateway`` provider in ``~/.omnigent/config.yaml`` exposes the AI
Gateway's Anthropic surface (``…/ai-gateway/anthropic``) and OpenAI surface
(``…/ai-gateway/openai/v1``) with ``eng_dev.ai_gateway.omni-*`` model ids. An
opencode-native session must route its model traffic through that configured
gateway (as pi does); instead the launch never consults the config, so the turn
never reaches the gateway on either surface and the non-``databricks-*`` model
id is dropped.

Journey per test: write the provider config into an isolated ``$HOME`` →
connect a host daemon under that HOME → create a host-bound opencode-native
session → send a chat message → the configured gateway must receive the model
request (the in-test gateway records every request it serves and answers both
surfaces' wire protocols, so a correctly routed turn can complete).

Needs a working ``opencode`` binary on PATH (the supported native-harness
range); skipped otherwise. No real LLM or credentials are used.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import yaml

from omnigent.entities.session_resources import terminal_resource_id
from omnigent.native.native_coding_agents import OPENCODE_NATIVE_AGENT_NAME
from tests._helpers.compat import apply_runner_env, compat_runner_cwd, runner_executable
from tests.e2e.helpers import POLL_INTERVAL_S

_ANTHROPIC_GATEWAY_PATH = "/ai-gateway/anthropic"
_OPENAI_GATEWAY_PATH = "/ai-gateway/openai/v1"
_ANTHROPIC_MODEL = "eng_dev.ai_gateway.omni-claude-fable-4"
_OPENAI_MODEL = "eng_dev.ai_gateway.omni-gpt-5.5"
_GATEWAY_API_KEY = "test-gateway-key"
_REPLY_TEXT = "GATEWAY-OK"

_HOST_ONLINE_TIMEOUT_S = 60.0
_TERMINAL_TIMEOUT_S = 120.0
_GATEWAY_HIT_TIMEOUT_S = 120.0
# Once the assistant reply landed, any gateway traffic the turn was going to
# produce has already been sent; a short grace catches stragglers.
_POST_REPLY_GRACE_S = 5.0


@pytest.fixture(scope="module")
def opencode_binary() -> str:
    """Skip unless a runnable ``opencode`` in the supported range is on PATH."""
    from omnigent.harnesses.opencode_native.app_server import (
        OpenCodeVersionError,
        check_opencode_version,
        resolve_opencode_version,
    )

    path = shutil.which("opencode")
    if path is None:
        pytest.skip("opencode CLI not on PATH")
    try:
        version = resolve_opencode_version(path)
        check_opencode_version(version)
    except OpenCodeVersionError as exc:
        pytest.skip(f"opencode CLI unusable for the native harness: {exc}")
    return path


@dataclass
class _GatewayRequest:
    method: str
    path: str
    authorization: str
    body: bytes


@dataclass
class _RequestLog:
    lock: threading.Lock = field(default_factory=threading.Lock)
    requests: list[_GatewayRequest] = field(default_factory=list)

    def record(self, request: _GatewayRequest) -> None:
        with self.lock:
            self.requests.append(request)

    def under(self, prefix: str) -> list[_GatewayRequest]:
        with self.lock:
            return [r for r in self.requests if r.path.startswith(prefix)]

    def model_calls(self, prefix: str) -> list[_GatewayRequest]:
        """POSTs to a model endpoint under *prefix* — the turn's actual LLM call.

        Excludes discovery traffic (e.g. a host model-options ``GET …/models``
        probe), which must not satisfy the routing assertion.
        """
        return [
            r
            for r in self.under(prefix)
            if r.method == "POST"
            and r.path.endswith(("/messages", "/chat/completions", "/responses"))
        ]

    def all(self) -> list[_GatewayRequest]:
        with self.lock:
            return list(self.requests)


class _GatewayHandler(BaseHTTPRequestHandler):
    """Records every request; speaks the two AI-gateway surfaces' protocols."""

    log: _RequestLog  # set on the server class per instance

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib signature
        pass

    def _record(self, body: bytes) -> None:
        self.log.record(
            _GatewayRequest(
                method=self.command,
                path=self.path,
                authorization=self.headers.get("Authorization", ""),
                body=body,
            )
        )

    def do_GET(self) -> None:
        self._record(b"")
        self._send_json(404, {"error": f"unexpected GET {self.path}"})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self._record(body)
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except ValueError:
            payload = {}
        stream = bool(payload.get("stream"))
        model = str(payload.get("model") or "mock-model")
        if self.path.startswith(_ANTHROPIC_GATEWAY_PATH) and self.path.endswith("/messages"):
            self._respond_anthropic(model, stream)
        elif self.path.startswith(_OPENAI_GATEWAY_PATH) and self.path.endswith(
            "/chat/completions"
        ):
            self._respond_openai_chat(model, stream)
        else:
            self._send_json(404, {"error": f"unexpected POST {self.path}"})

    def _send_json(self, status: int, payload: dict[str, object]) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_sse(self, events: list[tuple[str | None, dict[str, object] | str]]) -> None:
        chunks: list[bytes] = []
        for event_name, data in events:
            lines = []
            if event_name is not None:
                lines.append(f"event: {event_name}")
            rendered = data if isinstance(data, str) else json.dumps(data)
            lines.append(f"data: {rendered}")
            chunks.append(("\n".join(lines) + "\n\n").encode("utf-8"))
        body = b"".join(chunks)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _respond_anthropic(self, model: str, stream: bool) -> None:
        if not stream:
            self._send_json(
                200,
                {
                    "id": "msg_mock",
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": [{"type": "text", "text": _REPLY_TEXT}],
                    "stop_reason": "end_turn",
                    "stop_sequence": None,
                    "usage": {"input_tokens": 1, "output_tokens": 2},
                },
            )
            return
        self._send_sse(
            [
                (
                    "message_start",
                    {
                        "type": "message_start",
                        "message": {
                            "id": "msg_mock",
                            "type": "message",
                            "role": "assistant",
                            "model": model,
                            "content": [],
                            "stop_reason": None,
                            "stop_sequence": None,
                            "usage": {"input_tokens": 1, "output_tokens": 0},
                        },
                    },
                ),
                (
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {"type": "text", "text": ""},
                    },
                ),
                (
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": _REPLY_TEXT},
                    },
                ),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                (
                    "message_delta",
                    {
                        "type": "message_delta",
                        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                        "usage": {"output_tokens": 2},
                    },
                ),
                ("message_stop", {"type": "message_stop"}),
            ]
        )

    def _respond_openai_chat(self, model: str, stream: bool) -> None:
        if not stream:
            self._send_json(
                200,
                {
                    "id": "chatcmpl-mock",
                    "object": "chat.completion",
                    "created": 0,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": _REPLY_TEXT},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
                },
            )
            return
        chunk_base = {
            "id": "chatcmpl-mock",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": model,
        }
        self._send_sse(
            [
                (
                    None,
                    {
                        **chunk_base,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"role": "assistant", "content": _REPLY_TEXT},
                                "finish_reason": None,
                            }
                        ],
                    },
                ),
                (
                    None,
                    {
                        **chunk_base,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    },
                ),
                (None, "[DONE]"),
            ]
        )


class _MockAIGateway:
    """Loopback AI-gateway double that records every request it serves."""

    def __init__(self) -> None:
        self.log = _RequestLog()
        handler = type("_BoundGatewayHandler", (_GatewayHandler,), {"log": self.log})
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def __enter__(self) -> _MockAIGateway:
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=10)


def _write_provider_config(home: Path, families: dict[str, dict[str, object]]) -> None:
    config = {"providers": {"gw": {"kind": "gateway", "default": True, **families}}}
    path = home / ".omnigent" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(config), encoding="utf-8")


def _spawn_host_daemon(*, tmp_path: Path, live_server: str, home: Path) -> subprocess.Popen[bytes]:
    """Spawn an ``omnigent host`` daemon whose ``$HOME`` carries the provider config."""
    repo_root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    # Absolute sdk paths: the runner the daemon spawns runs from the session
    # workspace, where relative PYTHONPATH entries stop resolving omnigent_client.
    env["PYTHONPATH"] = os.pathsep.join(
        [
            str(repo_root),
            str(repo_root / "sdks" / "python-client"),
            str(repo_root / "sdks" / "ui"),
            env.get("PYTHONPATH", ""),
        ]
    )
    env["HOME"] = str(home)
    # Keep the daemon's + its runners' logs under the test tmp dir, where they
    # survive the suite's OMNIGENT_DATA_DIR cleanup and are inspectable on failure.
    env["OMNIGENT_DATA_DIR"] = str(tmp_path / "omnigent-data")
    # The config under $HOME must be the only provider source: drop ambient
    # provider credentials/config the CI or developer environment carries.
    for var in (
        "OMNIGENT_CONFIG_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
        "LLM_API_KEY",
        "OPENCODE_MODEL",
        "GATEWAY_BASE_URL",
        "OMNIGENT_DATABRICKS_GATEWAY_MODEL",
        "DATABRICKS_CONFIG_FILE",
        "DATABRICKS_CONFIG_PROFILE",
        "DATABRICKS_HOST",
        "DATABRICKS_TOKEN",
    ):
        env.pop(var, None)
    daemon_log = tmp_path / "host-daemon.log"
    with open(daemon_log, "w") as log_fh:
        return subprocess.Popen(
            [runner_executable(), "-m", "omnigent.host._daemon_entry", "--server", live_server],
            env=apply_runner_env(env),
            cwd=compat_runner_cwd(),
            stdout=log_fh,
            stderr=subprocess.STDOUT,
        )


def _online_host_ids(client: httpx.Client) -> set[str]:
    resp = client.get("/v1/hosts")
    if resp.status_code != 200:
        return set()
    return {str(h["host_id"]) for h in resp.json().get("hosts", []) if h["status"] == "online"}


def _wait_new_online_host(client: httpx.Client, known: set[str], timeout: float) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        fresh = _online_host_ids(client) - known
        if fresh:
            return fresh.pop()
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(f"No new host came online within {timeout}s")


def _wait_for_terminal(client: httpx.Client, *, session_id: str, timeout: float) -> None:
    resource_id = terminal_resource_id("opencode", "main")
    deadline = time.monotonic() + timeout
    last: list[object] = []
    while time.monotonic() < deadline:
        resp = client.get(f"/v1/sessions/{session_id}/resources")
        if resp.status_code == 200:
            data = resp.json().get("data", [])
            last = [r.get("id") for r in data]
            if any(r.get("id") == resource_id and r.get("type") == "terminal" for r in data):
                return
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(
        f"opencode terminal never appeared for {session_id} within {timeout}s; saw {last!r}"
    )


def _session_items(client: httpx.Client, session_id: str) -> list[dict[str, object]]:
    resp = client.get(f"/v1/sessions/{session_id}/items", params={"limit": 200, "order": "asc"})
    if resp.status_code != 200:
        return []
    return [it for it in resp.json().get("data", []) if isinstance(it, dict)]


def _items_summary(client: httpx.Client, session_id: str) -> str:
    lines = []
    for it in _session_items(client, session_id):
        text = ""
        for blk in it.get("content") or []:
            if isinstance(blk, dict) and isinstance(blk.get("text"), str):
                text += blk["text"]
        lines.append(
            f"pos={it.get('position')} type={it.get('type')} role={it.get('role')} "
            f"text={text[:120]!r}"
        )
    return "\n".join(lines) or "<no items>"


def _has_assistant_reply(items: list[dict[str, object]]) -> bool:
    return any(it.get("type") == "message" and it.get("role") == "assistant" for it in items)


def _drive_turn_and_collect_gateway_hits(
    client: httpx.Client,
    *,
    session_id: str,
    gateway: _MockAIGateway,
    surface_prefix: str,
) -> list[_GatewayRequest]:
    client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "message",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": "Reply with one word."}],
            },
        },
        timeout=30.0,
    ).raise_for_status()

    deadline = time.monotonic() + _GATEWAY_HIT_TIMEOUT_S
    reply_seen_at: float | None = None
    while time.monotonic() < deadline:
        if gateway.log.model_calls(surface_prefix):
            break
        if reply_seen_at is None and _has_assistant_reply(_session_items(client, session_id)):
            reply_seen_at = time.monotonic()
        if reply_seen_at is not None and time.monotonic() - reply_seen_at > _POST_REPLY_GRACE_S:
            break
        time.sleep(POLL_INTERVAL_S)
    return gateway.log.model_calls(surface_prefix)


def _create_opencode_session(
    client: httpx.Client,
    *,
    host_id: str,
    workspace: Path,
    model_override: str | None,
) -> str:
    resp = client.get("/v1/agents")
    resp.raise_for_status()
    agent_id = next(
        (a["id"] for a in resp.json()["data"] if a["name"] == OPENCODE_NATIVE_AGENT_NAME), None
    )
    assert agent_id is not None, f"{OPENCODE_NATIVE_AGENT_NAME!r} built-in agent not seeded"
    body: dict[str, object] = {
        "agent_id": agent_id,
        "host_id": host_id,
        "workspace": str(workspace),
    }
    if model_override is not None:
        body["model_override"] = model_override
    create = client.post("/v1/sessions", json=body, timeout=60.0)
    create.raise_for_status()
    return str(create.json()["id"])


def _run_gateway_journey(
    http_client: httpx.Client,
    *,
    tmp_path: Path,
    live_server: str,
    families: dict[str, dict[str, object]],
    model_override: str | None,
    surface_prefix: str,
) -> tuple[list[_GatewayRequest], _MockAIGateway, str]:
    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    with _MockAIGateway() as gateway:
        resolved = {
            name: {**family, "base_url": f"{gateway.base_url}{family['base_url']}"}
            for name, family in families.items()
        }
        _write_provider_config(home, resolved)
        known_hosts = _online_host_ids(http_client)
        daemon = _spawn_host_daemon(tmp_path=tmp_path, live_server=live_server, home=home)
        try:
            host_id = _wait_new_online_host(http_client, known_hosts, _HOST_ONLINE_TIMEOUT_S)
            session_id = _create_opencode_session(
                http_client,
                host_id=host_id,
                workspace=workspace,
                model_override=model_override,
            )
            _wait_for_terminal(http_client, session_id=session_id, timeout=_TERMINAL_TIMEOUT_S)
            hits = _drive_turn_and_collect_gateway_hits(
                http_client,
                session_id=session_id,
                gateway=gateway,
                surface_prefix=surface_prefix,
            )
            return hits, gateway, session_id
        finally:
            daemon.terminate()
            try:
                daemon.wait(timeout=10)
            except subprocess.TimeoutExpired:
                daemon.kill()


def test_opencode_native_routes_via_config_gateway_anthropic_surface(
    opencode_binary: str,
    http_client: httpx.Client,
    tmp_path: Path,
    live_server: str,
) -> None:
    """A config.yaml gateway's Anthropic surface must serve the opencode turn.

    The user pins the gateway's ``eng_dev.ai_gateway.omni-*`` (non-
    ``databricks-*``) Anthropic model, so the launch must adopt the config
    provider and the turn's model call must reach ``…/ai-gateway/anthropic``.
    """
    hits, gateway, session_id = _run_gateway_journey(
        http_client,
        tmp_path=tmp_path,
        live_server=live_server,
        families={
            "anthropic": {
                "base_url": _ANTHROPIC_GATEWAY_PATH,
                "api_key": _GATEWAY_API_KEY,
                "models": {"default": _ANTHROPIC_MODEL},
            }
        },
        model_override=_ANTHROPIC_MODEL,
        surface_prefix=_ANTHROPIC_GATEWAY_PATH,
    )
    all_requests = gateway.log.all()
    assert hits, (
        "opencode-native never consulted the config.yaml gateway provider: the turn's "
        f"model call never reached the configured Anthropic surface "
        f"({_ANTHROPIC_GATEWAY_PATH}) within {_GATEWAY_HIT_TIMEOUT_S}s of the user turn. "
        f"All gateway requests: {[(r.method, r.path) for r in all_requests]!r}. "
        f"Session items:\n{_items_summary(http_client, session_id)}"
    )
    assert any(_ANTHROPIC_MODEL.encode() in r.body for r in hits), (
        f"the pinned gateway model id {_ANTHROPIC_MODEL!r} was dropped: it appears in no "
        f"model call to the configured gateway. Bodies: {[r.body[:200] for r in hits]!r}"
    )


def test_opencode_native_routes_via_config_gateway_openai_surface(
    opencode_binary: str,
    http_client: httpx.Client,
    tmp_path: Path,
    live_server: str,
) -> None:
    """A config.yaml gateway's OpenAI surface must serve the opencode turn.

    No per-session model is pinned, so the launch must adopt the config
    provider's default ``eng_dev.ai_gateway.omni-*`` model and the turn's model
    call must reach ``…/ai-gateway/openai/v1``.
    """
    hits, gateway, session_id = _run_gateway_journey(
        http_client,
        tmp_path=tmp_path,
        live_server=live_server,
        families={
            "openai": {
                "base_url": _OPENAI_GATEWAY_PATH,
                "api_key": _GATEWAY_API_KEY,
                "wire_api": "chat",
                "models": {"default": _OPENAI_MODEL},
            }
        },
        model_override=None,
        surface_prefix=_OPENAI_GATEWAY_PATH,
    )
    all_requests = gateway.log.all()
    assert hits, (
        "opencode-native never consulted the config.yaml gateway provider: the turn's "
        f"model call never reached the configured OpenAI surface ({_OPENAI_GATEWAY_PATH}) "
        f"within {_GATEWAY_HIT_TIMEOUT_S}s of the user turn. All gateway requests: "
        f"{[(r.method, r.path) for r in all_requests]!r}. Session items:\n"
        f"{_items_summary(http_client, session_id)}"
    )
    assert any(_OPENAI_MODEL.encode() in r.body for r in hits), (
        f"the config default gateway model id {_OPENAI_MODEL!r} was dropped: it appears in "
        f"no model call to the configured gateway. Bodies: {[r.body[:200] for r in hits]!r}"
    )

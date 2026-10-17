"""Verify an unpinned Pi agent sends adaptive thinking to a gateway that rejects legacy thinking.

The fake gateway mirrors the Databricks Claude 4+/5 contract: ``thinking.type == "enabled"``
gets a 400, adaptive thinking gets a normal completion.

Usage::

    python -m pytest tests/e2e/test_pi_gateway_claude_adaptive_thinking_e2e.py -v
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from tests.e2e._harness_probes import cli_unavailable_reason

# tests/e2e/<this file> -> parents[2] is the repo root; threaded onto the CLI
# subprocess PYTHONPATH so it imports THIS worktree's code.
_REPO_ROOT = Path(__file__).resolve().parents[2]

# The only Claude model the fake workspace serves, so the unpinned default
# resolution is deterministic. Listed under both Databricks spellings.
_CLAUDE_MODEL = "databricks-claude-fable-5"
_CLAUDE_MODEL_SERVICE = "model-services/system.ai.claude-fable-5"

# The Databricks AI gateway's documented rejection of the legacy thinking
# payload on Claude-4+/5 models.
_ADAPTIVE_REJECT_MSG = (
    '"thinking.type.enabled" is not supported for this model. '
    'Use "thinking.type.adaptive" and "output_config.effort" to control thinking behavior.'
)

# Budget for the one-shot CLI journey: pi RPC boot + model discovery against
# the loopback workspace + one turn. Generous for a loaded CI box.
_RUN_TIMEOUT_S = 240

# Ambient credentials/proxies that would shadow the fake workspace or misroute
# the run; every OMNIGENT* var is also stripped below so a leaked data dir or
# runner var cannot route the run through a hosting session's daemon.
_STALE_ENV_VARS = (
    "DATABRICKS_HOST",
    "DATABRICKS_TOKEN",
    "DATABRICKS_CONFIG_PROFILE",
    "DATABRICKS_CLIENT_ID",
    "DATABRICKS_CLIENT_SECRET",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "http_proxy",
    "https_proxy",
    "RUNNER_SERVER_URL",
)


# --------------------------------------------------------------------------- #
# Fake Databricks workspace + AI gateway                                      #
# --------------------------------------------------------------------------- #
class _FakeWorkspaceHandler(BaseHTTPRequestHandler):
    """Fake workspace + AI gateway serving one Claude model; 400s ``thinking.type.enabled``."""

    protocol_version = "HTTP/1.1"
    # Populated per test run: every request the CLI sent, with parsed bodies.
    requests_seen: list[dict[str, Any]] = []
    _lock = threading.Lock()

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _path(self) -> str:
        """Request path without the query string Pi appends (``?beta=true``)."""
        return self.path.split("?", 1)[0]

    def _record(self, method: str, path: str, body: bytes) -> None:
        entry: dict[str, Any] = {"method": method, "path": path}
        if body:
            try:
                entry["json"] = json.loads(body)
            except ValueError:
                entry["raw"] = body[:500].decode("utf-8", "replace")
        with self._lock:
            type(self).requests_seen.append(entry)

    def _send_json(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = self._path()
        self._record("GET", path, b"")
        if path.startswith("/api/2.1/unity-catalog/model-services"):
            self._send_json(
                200,
                {
                    "model_services": [
                        {
                            "name": _CLAUDE_MODEL_SERVICE,
                            "supported_api_types": ["anthropic/v1/messages"],
                        }
                    ]
                },
            )
        elif path.startswith("/ai-gateway/anthropic/v1/models"):
            self._send_json(200, {"data": [{"id": _CLAUDE_MODEL}]})
        else:
            self._send_json(404, {"message": f"no handler for {path}"})

    def do_POST(self) -> None:
        body = self._read_body()
        path = self._path()
        self._record("POST", path, body)
        if "/serving-endpoints/anthropic" in path and path.endswith("/messages"):
            try:
                payload = json.loads(body)
            except ValueError:
                payload = {}
            thinking = payload.get("thinking") or {}
            if isinstance(thinking, dict) and thinking.get("type") == "enabled":
                self._send_json(400, {"message": _ADAPTIVE_REJECT_MSG})
                return
            self._send_sse_completion(payload)
        else:
            self._send_json(404, {"message": f"no handler for {path}"})

    def _send_sse_completion(self, payload: dict[str, Any]) -> None:
        """Stream a minimal valid Anthropic Messages completion ("PONG")."""
        events: list[tuple[str, dict[str, Any]]] = [
            (
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": "msg_fake_01",
                        "type": "message",
                        "role": "assistant",
                        "model": payload.get("model", _CLAUDE_MODEL),
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {"input_tokens": 10, "output_tokens": 1},
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
                    "delta": {"type": "text_delta", "text": "PONG"},
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
        chunks = b"".join(
            f"event: {name}\ndata: {json.dumps(data)}\n\n".encode() for name, data in events
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(chunks)))
        self.end_headers()
        self.wfile.write(chunks)

    def log_message(self, fmt: str, *args: object) -> None:
        pass


@pytest.fixture
def fake_workspace() -> Iterator[str]:
    """Run the fake workspace/gateway on a free loopback port.

    :yields: The workspace root URL, e.g. ``"http://127.0.0.1:44587"``.
    """
    _FakeWorkspaceHandler.requests_seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeWorkspaceHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _messages_requests() -> list[dict[str, Any]]:
    """Every anthropic-messages POST body the gateway received."""
    return [
        entry.get("json") or {}
        for entry in _FakeWorkspaceHandler.requests_seen
        if entry["method"] == "POST" and entry["path"].endswith("/messages")
    ]


def _thinking_types(bodies: list[dict[str, Any]]) -> list[object]:
    types: list[object] = []
    for body in bodies:
        thinking = body.get("thinking")
        types.append(thinking.get("type") if isinstance(thinking, dict) else None)
    return types


# --------------------------------------------------------------------------- #
# Live CLI journey (unpinned pi agent -> first turn)                          #
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(
    (_PI_REASON := cli_unavailable_reason("pi")) is not None,
    reason=(
        f"pi gateway adaptive-thinking journey requires a runnable 'pi' CLI; {_PI_REASON}. "
        "Install/fix Pi to run this test."
    ),
)
@pytest.mark.timeout(_RUN_TIMEOUT_S + 60)
def test_unpinned_pi_agent_first_turn_survives_claude_thinking_contract(
    fake_workspace: str, tmp_path: Path
) -> None:
    """Unpinned ``harness: pi`` agent -> ``omnigent run -p`` -> adaptive first turn completes."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".databrickscfg").write_text(
        f"[DEFAULT]\nhost = {fake_workspace}\ntoken = test-token\n", encoding="utf-8"
    )
    config_home = tmp_path / "omnigent-config"
    config_home.mkdir()
    spec = tmp_path / "agent.yaml"
    spec.write_text(
        "name: pi_gateway_adaptive_thinking\n"
        "prompt: You are a friendly assistant.\n"
        "executor:\n"
        "  harness: pi\n"
        "  auth:\n"
        "    type: databricks\n"
        "    profile: DEFAULT\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    for stale in _STALE_ENV_VARS:
        env.pop(stale, None)
    for leaked in [name for name in env if name.startswith(("HARNESS_PI_", "OMNIGENT"))]:
        env.pop(leaked, None)
    env["HOME"] = str(home)
    env["DATABRICKS_CONFIG_FILE"] = str(home / ".databrickscfg")
    env["OMNIGENT_CONFIG_HOME"] = str(config_home)
    env["OMNIGENT_NO_UPDATE_CHECK"] = "1"
    env["OMNIGENT_SKIP_ONBOARD"] = "1"
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env["no_proxy"] = "127.0.0.1,localhost"
    env["PYTHONPATH"] = os.pathsep.join([str(_REPO_ROOT), *filter(None, [env.get("PYTHONPATH")])])

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "omnigent",
            "run",
            str(spec),
            "-p",
            "Reply with exactly: PONG",
            "--no-log",
            "--no-session",
        ],
        env=env,
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=_RUN_TIMEOUT_S,
    )

    bodies = _messages_requests()
    seen = [(entry["method"], entry["path"]) for entry in _FakeWorkspaceHandler.requests_seen]
    assert bodies, (
        "the pi turn never reached the gateway's anthropic-messages surface — the "
        "journey did not run to the reported failure point.\n"
        f"  requests seen: {seen!r}\n"
        f"  exit_code: {result.returncode}\n"
        f"  stdout tail: {result.stdout[-1500:]!r}\n"
        f"  stderr tail: {result.stderr[-1500:]!r}"
    )

    thinking_types = _thinking_types(bodies)
    assert set(thinking_types) == {"adaptive"} and result.returncode == 0, (
        "expected every messages request to use adaptive thinking and the run to succeed.\n"
        f"  thinking payloads posted: {thinking_types!r}\n"
        f"  resolved model: {bodies[-1].get('model')!r}\n"
        f"  exit_code: {result.returncode}\n"
        f"  stdout tail: {result.stdout[-1500:]!r}\n"
        f"  stderr tail: {result.stderr[-1500:]!r}"
    )

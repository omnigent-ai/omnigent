"""in-process pi: Databricks Claude models 400 with ``thinking.type.enabled``.

The journey
-----------
A user configures Databricks workspace credentials (``~/.databrickscfg``) and
launches a ``harness: pi`` agent with **no** ``model:`` pinned (``omnigent run
agent.yaml -p ...``). The executor resolves the default model from the
workspace's Claude catalog, so the run lands on a Claude-4+/5 model routed
through the ``databricks-anthropic`` provider (``api: anthropic-messages``).
``_build_models_json`` in ``omnigent/inner/pi_executor.py`` writes that
provider with no ``compat`` block, so Pi (0.84.2+) sends the legacy
``thinking: {type: "enabled", budget_tokens: ...}`` payload, which the
Databricks AI gateway rejects for Claude-4+/5::

    400 {"message": "\\"thinking.type.enabled\\" is not supported for this
    model. Use \\"thinking.type.adaptive\\" and \\"output_config.effort\\" to
    control thinking behavior."}

The very first turn of an unpinned pi agent therefore fails. pi-native already
pairs ``api: anthropic-messages`` with ``compat: {forceAdaptiveThinking: true}``
(``omnigent/harnesses/pi_native/credentials.py``); the in-process path never
received the same treatment.

The fail -> pass contract
-------------------------
The fake workspace/gateway implements the documented Claude-4+/5 contract: it
rejects ``thinking.type == "enabled"`` with the exact 400 above and streams a
normal Anthropic Messages SSE completion for any other thinking mode. The live
journey test fails on the current build (Pi posts the legacy payload, the CLI
run exits non-zero with the 400) and passes once the provider forces adaptive
thinking (Pi posts ``thinking.type: adaptive`` and the turn completes). The
render-level test pins the provider-compat half deterministically, without
needing the ``pi`` CLI.

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

from omnigent.inner.pi_executor import _build_models_json
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

# Env vars that, leaked from this (possibly omnigent-hosted) process into the
# CLI subprocess, would shadow the fake workspace's credentials or misroute
# the run. Every OMNIGENT* var is stripped by prefix below: a leaked
# OMNIGENT_DATA_DIR (or runner var) routes the run through the hosting
# session's live host daemon instead of a standalone one-shot run.
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
    """Play a Databricks workspace whose gateway serves one Claude-5 model.

    - Unity Catalog model-services listing -> the single Claude entry (with
      its Anthropic Messages wire surface), so both the unpinned default
      resolution and the executor's catalog fetch see it.
    - Anthropic gateway model listing -> the same model id.
    - ``POST .../serving-endpoints/anthropic/v1/messages`` -> the documented
      Claude-4+/5 contract: 400 on ``thinking.type == "enabled"``, a normal
      SSE completion otherwise.
    """

    protocol_version = "HTTP/1.1"
    # Populated per test run: every request the CLI sent, with parsed bodies.
    requests_seen: list[dict[str, Any]] = []
    _lock = threading.Lock()

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _record(self, method: str, body: bytes) -> None:
        entry: dict[str, Any] = {"method": method, "path": self.path}
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
        self._record("GET", b"")
        if self.path.startswith("/api/2.1/unity-catalog/model-services"):
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
        elif self.path.startswith("/ai-gateway/anthropic/v1/models"):
            self._send_json(200, {"data": [{"id": _CLAUDE_MODEL}]})
        else:
            self._send_json(200, {})

    def do_POST(self) -> None:
        body = self._read_body()
        self._record("POST", body)
        if "/serving-endpoints/anthropic" in self.path and self.path.endswith("/messages"):
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
            self._send_json(404, {"message": f"no handler for {self.path}"})

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
# Render contract (deterministic, no pi CLI needed)                           #
# --------------------------------------------------------------------------- #
def test_models_json_databricks_anthropic_forces_adaptive_thinking() -> None:
    """The ``databricks-anthropic`` provider must force adaptive thinking.

    Its Claude model entries carry ``reasoning: true`` (Pi's thinking-level
    controls), and Pi only sends ``thinking.type.adaptive`` — required by
    Claude-4+/5 on the Databricks gateway — when the provider compat block
    sets ``forceAdaptiveThinking``; without it Pi sends the legacy
    ``thinking.type.enabled`` payload the gateway 400s. pi-native already
    pairs the two (``omnigent/harnesses/pi_native/credentials.py``).
    """
    config = _build_models_json("https://workspace.example.com", "test-token")
    provider = config["providers"]["databricks-anthropic"]
    compat = provider.get("compat")
    force_adaptive = compat.get("forceAdaptiveThinking") if isinstance(compat, dict) else None
    assert force_adaptive is True, (
        "the in-process pi models.json wires the 'databricks-anthropic' provider "
        f"(api: anthropic-messages) with compat={compat!r} — no forceAdaptiveThinking. "
        "Pi therefore sends the legacy thinking.type.enabled payload and every "
        "Claude-4+/5 first turn 400s against the Databricks AI gateway with "
        f"{_ADAPTIVE_REJECT_MSG!r}. Mirror pi-native: add "
        "compat={'forceAdaptiveThinking': True} to this provider in _build_models_json."
    )


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
    """Drive the real one-shot CLI journey and require the first turn to land.

    Journey: Databricks credentials in ``~/.databrickscfg`` -> ``harness: pi``
    agent with no ``model:`` pinned -> ``omnigent run agent.yaml -p ...`` ->
    the default model resolves to the workspace's Claude model and the first
    turn must complete. Fails on the current build: Pi posts
    ``thinking.type.enabled`` (no provider compat), the gateway answers the
    documented 400, and the run exits non-zero showing that error. Passes once
    the provider forces adaptive thinking.
    """
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

    env = dict(os.environ)
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
    legacy_payload_sent = "enabled" in thinking_types
    assert not legacy_payload_sent and result.returncode == 0, (
        "an unpinned `harness: pi` agent's FIRST TURN failed against a Databricks "
        f"gateway serving Claude-4+/5 (resolved model: {bodies[-1].get('model')!r}).\n"
        f"  thinking payloads posted: {thinking_types!r} (Claude-4+/5 reject 'enabled' "
        f"with 400 {_ADAPTIVE_REJECT_MSG!r})\n"
        f"  exit_code: {result.returncode}\n"
        f"  stdout tail: {result.stdout[-1500:]!r}\n"
        f"  stderr tail: {result.stderr[-1500:]!r}\n"
        "The 'databricks-anthropic' provider in _build_models_json must set "
        "compat={'forceAdaptiveThinking': True} (as pi-native does) so Pi sends "
        "thinking.type.adaptive and the turn completes."
    )

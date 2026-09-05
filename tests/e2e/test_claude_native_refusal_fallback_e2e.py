"""E2E regression test: claude-native refusal-fallback routes to a served model.

The claude-native sibling of ``test_claude_sdk_refusal_fallback_e2e.py``: a
safeguard-flagged turn on **claude-native** dies the way claude-sdk's once
did. Claude Code's safeguards can flag a message and return an API
*refusal*; the CLI then arms its refusal-fallback and re-issues the turn on a
model named by a route table internal to the CLI — a **canonical** vendor id
(e.g. ``claude-opus-4-8``). A gateway that serves Claude under its own
spellings (Databricks ``databricks-claude-*``, or any gateway with its own
ids) rejects that spelling with ``model_not_found``, so the flagged turn dies.

claude-sdk was fixed by handing Claude Code a ``modelOverrides`` map —
canonical id → served spelling — in the invocation-local settings the SDK
already builds per turn. claude-native writes its settings through the
bridge's sidecar instead (``build_hook_settings`` / ``augment_claude_args``
in ``omnigent/claude_native_bridge.py``), and without the map in that sidecar
the canonical id reaches the gateway unrewritten on every native launch path
(web/runner-spawned terminals and ucode-launched Claude Code).

This test drives the real user journey end to end — a real ``omnigent
server`` subprocess, a real runner subprocess with a mock-gateway provider
configured (``providers:`` in an isolated ``OMNIGENT_CONFIG_HOME``), a real
``claude`` CLI launched by the runner in a tmux pane (the claude-native
terminal every native web session gets), and a web-UI user message injected
into that terminal. The mock gateway refuses the launch-model turn (``cyber``
category) and the test asserts on the Anthropic wire, via the mock's request
capture, that the refusal-fallback re-issued on the gateway's **served** Opus
spelling rather than the canonical vendor id the gateway would reject.

Requirements: ``claude`` CLI and ``tmux`` on PATH. No Claude login is needed
— the provider config routes the CLI through the mock gateway with an
``apiKeyHelper``, exactly like the claude-sdk mock-mode e2e tests.

Usage::

    pytest tests/e2e/test_claude_native_refusal_fallback_e2e.py -v --timeout=600
"""

from __future__ import annotations

import io
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Every HTTP call in this test targets 127.0.0.1; CI shells can carry an
# egress proxy in the environment, so bypass proxy autodetection entirely.
_http = httpx.Client(trust_env=False)

# The runner imports ``omnigent_client`` / ``omnigent_ui_sdk``; in a worktree
# they resolve from sdks/, in an installed venv from site-packages.
_PYTHONPATH = os.pathsep.join(
    [
        str(_REPO_ROOT),
        str(_REPO_ROOT / "sdks" / "python-client"),
        str(_REPO_ROOT / "sdks" / "ui"),
        os.environ.get("PYTHONPATH", ""),
    ]
)

# The Claude ids the mock gateway serves under its own spelling — the launch
# model and the Opus the refusal-fallback must route to. Deliberately not a
# real vendor's spelling: the rewrite must work for any gateway's ids. The
# fallback target generation (``opus-4-8``) matches the canonical id the
# pinned CLI's refusal route table names — the same pair the claude-sdk
# sibling test pins.
_LAUNCH_MODEL = "gw-claude-fable-5"
_SERVED_FALLBACK_MODEL = "gw-claude-opus-4-8"
# A newer Opus, served alongside the fallback target. The ``opus`` alias pin
# tracks it (newest per family — what ucode/Databricks discovery pins), so the
# refusal-fallback can only reach the older generation its route table names
# through the canonical rewrites — which is the behavior under test. With a
# single served Opus the alias pin alone happens to carry the fallback; a
# gateway serving exactly this pair is where the pin stops being enough.
_SERVED_NEWER_OPUS = "gw-claude-opus-5"
_SERVED_HAIKU_MODEL = "gw-claude-haiku-4-5"
_SERVED_MODELS = [
    _LAUNCH_MODEL,
    _SERVED_FALLBACK_MODEL,
    _SERVED_NEWER_OPUS,
    "gw-claude-sonnet-5",
    _SERVED_HAIKU_MODEL,
]

# Unique content token the test's user turn carries, so the wire assertions
# see exactly the requests that carried this turn (the launch request, the
# CLI's haiku preflight, and the fallback re-issue all resend the user text)
# and never the runner's headless catalog probes or the CLI's startup pings.
_TRIGGER = "refusal-fallback-live-check"

_HEALTH_TIMEOUT_S = 120.0
_POLL_S = 1.0
# Terminal auto-create includes bridge prep, catalog probes against the mock,
# and a real Claude TUI boot; then the injected turn plus the fallback
# re-issue. Generous for CI.
_TURN_TIMEOUT_S = 300.0

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None or shutil.which("claude") is None,
    reason="claude-native terminals need tmux and the claude CLI on PATH",
)


def _find_free_port() -> int:
    """Grab an ephemeral port for a spawned server."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _localhost_env(extra: dict[str, str]) -> dict[str, str]:
    """Subprocess env with worktree imports and no proxy in the way.

    :param extra: Overrides/additions applied after the base env.
    :returns: Environment mapping for ``subprocess.Popen``.
    """
    env = {
        **os.environ,
        "PYTHONPATH": _PYTHONPATH,
        # CI shells often carry an egress proxy; localhost must bypass it —
        # for the server/runner and for the Claude CLI they spawn.
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    }
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env.pop(name, None)
    env.update(extra)
    return env


def _terminate(proc: subprocess.Popen[bytes] | None) -> None:
    """Best-effort SIGTERM -> SIGKILL teardown for a spawned process."""
    if proc is None or proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _wait_http_ok(url: str, deadline: float) -> None:
    """Poll *url* until it returns 200 or *deadline* (monotonic) passes."""
    last = "not polled"
    while time.monotonic() < deadline:
        try:
            if _http.get(url, timeout=2.0).status_code == 200:
                return
            last = "non-200"
        except httpx.HTTPError as exc:
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(_POLL_S)
    raise AssertionError(f"{url} never became healthy: {last}")


def _mock_configure(
    mock_url: str,
    responses: list[dict[str, Any]],
    *,
    key: str = "default",
    match: str | None = None,
) -> None:
    """Load a keyed/matched response queue on the mock LLM server."""
    payload: dict[str, Any] = {"key": key, "responses": responses}
    if match is not None:
        payload["match"] = match
    _http.post(f"{mock_url}/mock/configure", json=payload, timeout=5.0).raise_for_status()


def _mock_served_models(mock_url: str, models: list[str]) -> None:
    """Set the ids the mock gateway's ``GET /v1/models`` listing reports."""
    _http.post(
        f"{mock_url}/mock/served_models", json={"models": models}, timeout=5.0
    ).raise_for_status()


def _mock_requests(mock_url: str) -> list[dict[str, Any]]:
    """Return the requests the mock captured, oldest first."""
    resp = _http.get(f"{mock_url}/mock/requests", timeout=10.0)
    resp.raise_for_status()
    return list(resp.json().get("requests", []))


def _request_user_text(req: dict[str, Any]) -> str:
    """Flatten an Anthropic Messages request's user-role input text."""
    parts: list[str] = []
    for message in req.get("messages", []) or []:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    parts.append(block["text"])
    return " ".join(parts)


def _trigger_wire_models(mock_url: str) -> list[str]:
    """Models of captured requests whose user input carries the trigger token.

    This scopes the assertions to the turn under test — the launch request,
    the CLI's haiku preflight, and the fallback re-issue, all of which resend
    the user text. The runner's headless catalog probes and the CLI's startup
    pings never carry the token.
    """
    return [
        str(req.get("model"))
        for req in _mock_requests(mock_url)
        if _TRIGGER in _request_user_text(req)
    ]


def _fallback_models(wire_models: list[str]) -> list[str]:
    """The trigger-carrying request models that can only be the fallback.

    Everything the turn legitimately runs on before the refusal is excluded:
    the launch model itself and the pinned haiku (the CLI's preflight/topic
    requests resolve the haiku alias through the pin env). What remains is
    whatever the refusal-fallback re-issued on — the gateway's served Opus
    spelling when the canonical rewrites are in place, the bare canonical
    vendor id when they are not.
    """
    return [m for m in wire_models if m not in (_LAUNCH_MODEL, _SERVED_HAIKU_MODEL)]


def _create_claude_native_session(base_url: str) -> str:
    """Create a claude-native wrapper session exactly like ``omnigent claude``.

    Reuses the production spec materializer and stamps the same wrapper /
    terminal-first labels the CLI writes, so the runner's claude-native
    auto-bootstrap recognizes the session and creates the Claude terminal
    when the session binds to the runner.

    :param base_url: Spawned server base URL.
    :returns: The new session/conversation id.
    """
    from omnigent._wrapper_labels import (
        CLAUDE_NATIVE_WRAPPER_VALUE,
        UI_MODE_LABEL_KEY,
        UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY,
    )
    from omnigent.claude_native import _materialize_claude_agent_spec

    with tempfile.TemporaryDirectory() as tmp:
        yaml_text = _materialize_claude_agent_spec(Path(tmp)).read_text()

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = yaml_text.encode()
        # Non-config.yaml arcname routes through the omnigent compat
        # translator (the wrapper spec has no ``spec_version``).
        info = tarfile.TarInfo("claude-native-ui.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    labels = {
        UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY: CLAUDE_NATIVE_WRAPPER_VALUE,
    }
    create = _http.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps({"labels": labels})},
        files={
            "bundle": (
                "claude-native-ui.tar.gz",
                buf.getvalue(),
                "application/gzip",
            )
        },
        timeout=30.0,
    )
    create.raise_for_status()
    return str(create.json()["session_id"])


def test_claude_native_refusal_fallback_routes_to_served_model(
    tmp_path: Path,
) -> None:
    """A safeguard refusal on claude-native falls back to the served Opus id.

    The mock gateway serves Claude under its own spellings and refuses the
    launch-model turn with a ``cyber`` refusal. Claude Code arms its
    refusal-fallback and re-issues the turn on the Opus its internal route
    table names. On the buggy build the sidecar settings claude-native writes
    (``build_hook_settings``) carry no ``modelOverrides`` map, so that re-issue
    names the **canonical** vendor id — a spelling this gateway does not serve,
    which a real gateway rejects with ``model_not_found``, killing the flagged
    turn. Fixed behavior (claude-sdk parity): the canonical id is rewritten to
    the gateway's spelling, so the fallback request that reaches the wire names
    ``gw-claude-opus-4-8`` and the turn completes.

    :param tmp_path: Per-test temp dir (server DB, runner HOME, logs).
    """
    mock_port = _find_free_port()
    mock_url = f"http://127.0.0.1:{mock_port}"
    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    db_path = tmp_path / "chat.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner_home = tmp_path / "home"
    runner_home.mkdir()

    # The mock-gateway provider the runner resolves for claude-native — the
    # ``omnigent setup`` shape for any Anthropic-compatible gateway that
    # serves Claude under its own ids. The declared models are the gateway's
    # routable set (``ClaudeNativeUcodeConfig.routable_models``); the mock's
    # ``/v1/models`` listing below reports the same ids, so a fix may derive
    # the canonical rewrites from either source.
    config_home = runner_home / ".omnigent"
    config_home.mkdir(parents=True)
    (config_home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "providers": {
                    "mock-gateway": {
                        "kind": "gateway",
                        "default": "true",
                        "anthropic": {
                            "base_url": mock_url,
                            "api_key": "mock-key",
                            "models": {
                                "default": _LAUNCH_MODEL,
                                # The alias tracks the newest served Opus,
                                # exactly like ucode/Databricks discovery pins
                                # it — NOT the older generation the CLI's
                                # refusal route table names.
                                "opus": _SERVED_NEWER_OPUS,
                                "sonnet": "gw-claude-sonnet-5",
                                "haiku": _SERVED_HAIKU_MODEL,
                            },
                        },
                    }
                }
            }
        )
    )

    binding_token = secrets.token_urlsafe(32)
    from omnigent.runner.identity import token_bound_runner_id

    runner_id = token_bound_runner_id(binding_token)

    mock_log = (tmp_path / "mock_llm.log").open("w")
    server_log = (tmp_path / "server.log").open("w")
    runner_log = (tmp_path / "runner.log").open("w")
    mock_proc: subprocess.Popen[bytes] | None = None
    server_proc: subprocess.Popen[bytes] | None = None
    runner_proc: subprocess.Popen[bytes] | None = None
    try:
        mock_proc = subprocess.Popen(
            [
                sys.executable,
                str(_REPO_ROOT / "tests" / "server" / "integration" / "mock_llm_server.py"),
                str(mock_port),
            ],
            env=_localhost_env({}),
            stdout=mock_log,
            stderr=subprocess.STDOUT,
        )
        _wait_http_ok(f"{mock_url}/stats", time.monotonic() + 30.0)
        # What the gateway serves. The launch-model queue below is configured
        # right before the user turn is sent (after terminal bring-up), so
        # the runner's own headless catalog probes cannot consume it.
        _mock_served_models(mock_url, _SERVED_MODELS)

        server_proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "omnigent.cli",
                "server",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--database-uri",
                f"sqlite:///{db_path}",
                "--artifact-location",
                str(tmp_path / "artifacts"),
            ],
            env=_localhost_env({"OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token}),
            stdout=server_log,
            stderr=subprocess.STDOUT,
        )
        _wait_http_ok(f"{base_url}/health", time.monotonic() + _HEALTH_TIMEOUT_S)

        runner_proc = subprocess.Popen(
            [sys.executable, "-m", "omnigent.runner._entry"],
            env=_localhost_env(
                {
                    "OMNIGENT_RUNNER_ID": runner_id,
                    "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
                    "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
                    "RUNNER_SERVER_URL": base_url,
                    "OMNIGENT_RUNNER_WORKSPACE": str(workspace),
                    # Hermetic HOME: the Claude CLI's own state
                    # (``~/.claude.json`` — the bridge pre-accepts its
                    # onboarding/trust gates there) stays off the real HOME.
                    "HOME": str(runner_home),
                    # Provider config isolation: the mock-gateway provider
                    # written above is the runner's whole provider world.
                    "OMNIGENT_CONFIG_HOME": str(config_home),
                }
            ),
            stdout=runner_log,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + _HEALTH_TIMEOUT_S
        online = False
        while time.monotonic() < deadline:
            try:
                status = _http.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=2.0)
                if status.status_code == 200 and status.json().get("online") is True:
                    online = True
                    break
            except httpx.HTTPError:
                # The server/runner is still booting; transient connection
                # errors are expected while polling and simply retried.
                pass
            time.sleep(_POLL_S)
        assert online, (
            f"runner never came online; log:\n{(tmp_path / 'runner.log').read_text()[-3000:]}"
        )

        # THE JOURNEY: create a claude-native session (what the web UI /
        # ``omnigent claude`` does) and bind it to the runner — the runner
        # auto-creates the Claude Code terminal, routed through the
        # mock-gateway provider. The bind blocks on terminal bring-up
        # (including the runner's catalog probes against the mock).
        session_id = _create_claude_native_session(base_url)
        _http.patch(
            f"{base_url}/v1/sessions/{session_id}",
            json={"runner_id": runner_id},
            timeout=_TURN_TIMEOUT_S,
        ).raise_for_status()

        # Script the safeguard refusal for the user's turn, keyed by the
        # launch model exactly like the claude-sdk sibling test: the CLI's
        # haiku preflights and any probe traffic draw from the default queue
        # (plain text), the launch-model turn is refused once, and the
        # fallback re-issue (whatever model id it names) falls through to the
        # default queue so the turn can complete.
        _mock_configure(
            mock_url,
            [{"refusal_category": "cyber"}],
            key=_LAUNCH_MODEL,
        )

        # The user's message, sent from the web UI. The runner's bridge waits
        # for Claude's input prompt to render, then injects it into the TUI.
        # The mock refuses this turn, which arms the CLI's refusal-fallback.
        send = _http.post(
            f"{base_url}/v1/sessions/{session_id}/events",
            json={
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                f"{_TRIGGER}: please answer this message in one short sentence."
                            ),
                        }
                    ],
                },
            },
            timeout=60.0,
        )
        send.raise_for_status()

        # Wait for the refused launch turn and then the fallback re-issue to
        # reach the Anthropic wire (the fallback resends the conversation, so
        # it carries the trigger token too).
        deadline = time.monotonic() + _TURN_TIMEOUT_S
        wire_models: list[str] = []
        while time.monotonic() < deadline:
            wire_models = _trigger_wire_models(mock_url)
            if _LAUNCH_MODEL in wire_models and _fallback_models(wire_models):
                break
            time.sleep(_POLL_S)

        all_wire_models = [str(req.get("model")) for req in _mock_requests(mock_url)]

        # The launch turn ran on the gateway's launch model and was refused —
        # the precondition. If this is missing, the terminal never got the
        # message (bring-up/injection failure, not the bug under test).
        assert _LAUNCH_MODEL in wire_models, (
            f"the launch model {_LAUNCH_MODEL!r} never carried the user turn; "
            f"trigger-carrying wire models: {wire_models}; all wire models: "
            f"{all_wire_models}; runner log tail:\n"
            f"{(tmp_path / 'runner.log').read_text()[-3000:]}"
        )
        fallback_models = _fallback_models(wire_models)
        # The refusal-fallback re-issued at all.
        assert fallback_models, (
            "the refusal-fallback never re-issued the flagged turn: "
            f"trigger-carrying wire models {wire_models} contain no fallback "
            f"request (all wire models: {all_wire_models}). Claude Code should "
            "retry a safeguard-refused turn on its fallback model."
        )

        # THE BUG: the re-issue must name the gateway's spelling of the Opus
        # the CLI's route table picked. Without the canonical rewrites
        # (``modelOverrides`` in the settings sidecar claude-native writes),
        # the CLI sends the bare canonical id — a model this gateway does not
        # serve, which a real gateway rejects with ``model_not_found``,
        # killing the safeguard-flagged turn.
        assert _SERVED_FALLBACK_MODEL in fallback_models, (
            f"the refusal-fallback did not re-issue on the served Opus id "
            f"{_SERVED_FALLBACK_MODEL!r} — the canonical id Claude Code names "
            f"was not rewritten to this gateway's spelling, so the fallback "
            f"request named a model the gateway rejects and the flagged turn "
            f"dies. Fallback request models: {fallback_models}; all "
            f"trigger-carrying models: {wire_models}"
        )
        # No canonical spelling ever carried the turn: the rewrite happened
        # before the request, rather than the gateway happening to tolerate a
        # vendor id.
        canonical_leaks = [m for m in wire_models if m.startswith("claude-")]
        assert not canonical_leaks, (
            f"canonical vendor ids reached the gateway unrewritten: {canonical_leaks}"
        )
    finally:
        _terminate(runner_proc)
        _terminate(server_proc)
        _terminate(mock_proc)
        mock_log.close()
        server_log.close()
        runner_log.close()

"""E2E: a codex-native chat turn must not die when the TUI cold-starts slowly.

Regression test: the codex-native thread-start wait was cancelled by a
deadline, after which chat silently never forwarded.

A host-spawned codex-native session launches the ``--remote`` Codex TUI in
the session terminal and, in the background, runs
``_codex_discover_thread_and_forward`` -> ``wait_for_thread_started``. That
wait is bounded by ``_THREAD_START_TIMEOUT_SECONDS`` (30s) via
``asyncio.timeout``. When the TUI cold-start over the runner takes longer
than the deadline (a loaded host: the reporter saw this on released builds),
the ``asyncio.timeout`` cancels the ``client.iter_events()`` drain with a
``CancelledError`` -- a deadline expiring, NOT the app-server refusing. The
forwarder's ``except (TimeoutError, RuntimeError)`` handler then logs::

    Codex TUI never started a thread for conv_...; chat will not forward

writes a bridge startup error, and *returns* -- so the session is up but
permanently mute. The user's first chat turn fails with::

    inner executor error: Codex native thread never started: Codex
    app-server never started a thread (startup timed out: TimeoutError). ...

and every later turn in the session is dead too, because thread discovery is
never retried or restarted.

Reproduction knob
-----------------
The reported trigger is a *slow* TUI cold-start, not a missing credential
(that fail-fast path is already handled -- see
``test_codex_native_headless_login_timeout.py``). We induce the slow
cold-start deterministically through the supported per-harness command
override (``harness.codex-native.command``, the same hook Databricks' ``isaac``
wrapper uses): a tiny shell wrapper that ``sleep``s past the 30s deadline
before ``exec``-ing the real ``codex``. A provider is configured against an
in-test mock Responses server, so the launch is NOT ``login_required`` and
takes the deadline-bounded ``wait_for_thread_started`` path -- the exact code
the traceback names -- and, once the thread exists, the model call can
actually complete so a healthy turn produces a visible assistant reply.

Assertion polarity
------------------
While the bug is live the first turn dies with the ``startup timed out`` /
``never started a thread`` marker, so this test is RED. After a fix (keep
listening for the thread instead of giving up on the deadline, so the session
keeps forwarding) the turn survives the cold-start: the mock model's reply is
mirrored into the transcript and the marker never appears -- GREEN.

    .venv/bin/python -m pytest \\
        tests/e2e_ui/chat/test_codex_native_slow_coldstart_thread_timeout.py -v
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import _create_native_codex_session
from tests.e2e_ui.messages.test_message_render_parity import _ensure_chat_view, _send

_REPO_ROOT = Path(__file__).resolve().parents[3]

# Boot budget for the spawned server + runner pair.
_HEALTH_TIMEOUT_S = 90.0
# Seconds the wrapper stalls the ``--remote`` TUI launch before exec-ing the
# real codex. Comfortably past the 30s ``_THREAD_START_TIMEOUT_SECONDS`` so the
# deadline fires while the TUI is still cold-starting -- the reported timing.
_COLDSTART_DELAY_S = 45
# The buggy path errors after the 30s thread-start timeout plus the executor's
# bridge-state poll; give the terminal signal ample room past the delay.
_TURN_OUTCOME_TIMEOUT_S = 150.0

_ERROR_PILL = '[data-testid="error-pill"]'
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'

# The reported failure markers, asserted against the canonical transcript.
_STARTUP_TIMEOUT_MARKER = "startup timed out"
_THREAD_NEVER_STARTED_MARKER = "never started a thread"

# Deterministic reply the mock Responses server streams for every turn; its
# arrival in the transcript proves chat forwarded after the slow cold-start.
_MOCK_REPLY_TEXT = "Reviewed the diff after the slow cold-start; no issues found."


class _MockResponsesHandler(BaseHTTPRequestHandler):
    """Minimal OpenAI Responses API: streams one canned assistant reply."""

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        events = [
            {"type": "response.created", "response": {"id": "resp-slow-coldstart"}},
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "message",
                    "role": "assistant",
                    "id": "msg-slow-coldstart",
                    "content": [{"type": "output_text", "text": _MOCK_REPLY_TEXT}],
                },
            },
            {
                "type": "response.completed",
                "response": {
                    "id": "resp-slow-coldstart",
                    "usage": {
                        "input_tokens": 0,
                        "input_tokens_details": None,
                        "output_tokens": 0,
                        "output_tokens_details": None,
                        "total_tokens": 0,
                    },
                },
            },
        ]
        body = "".join(f"data: {json.dumps(event)}\n\n" for event in events).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        """Keep the canned server quiet under captured pytest output."""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# Proxy-blind client: CI forces an egress proxy via HTTP(S)_PROXY env vars
# that must not intercept loopback requests to the spawned server.
_client = httpx.Client(trust_env=False)

# Shared fixtures/helpers (e.g. the conftest session factory) use ambient
# ``httpx`` calls that DO trust env, so also exclude loopback from any forced
# proxy at import time.
for _var in ("NO_PROXY", "no_proxy"):
    os.environ[_var] = ",".join(filter(None, [os.environ.get(_var, ""), "127.0.0.1,localhost"]))


def _no_proxy_env() -> dict[str, str]:
    """Ambient env with loopback excluded from any forced HTTP(S) proxy."""
    env = os.environ.copy()
    for var in ("NO_PROXY", "no_proxy"):
        existing = env.get(var, "")
        env[var] = ",".join(filter(None, [existing, "127.0.0.1,localhost"]))
    return env


@pytest.fixture
def slow_coldstart_codex_session(
    built_spa: None,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str, Path]]:
    """A codex-native session whose ``--remote`` TUI cold-starts past the deadline.

    Spawns a dedicated server + runner (own ``CODEX_HOME`` /
    ``OMNIGENT_CONFIG_HOME`` so nothing leaks into other tests). A provider is
    configured so the launch routes normally (NOT ``login_required``), and a
    ``harness.codex-native.command`` wrapper stalls the TUI launch past the 30s
    ``wait_for_thread_started`` deadline -- the routing state in which the
    reported deadline-cancellation timeout fires.

    :returns: ``(base_url, session_id, runner_log_path)``.
    """
    if shutil.which("codex") is None:
        pytest.skip("codex CLI is required for the slow-coldstart codex-native rig")

    work = tmp_path_factory.mktemp("codex_slow_coldstart")
    config_home = work / "config-home"
    codex_home = work / "codex-home"
    home_dir = work / "home"
    state_dir = work / "codex-native-state"
    artifacts = work / "artifacts"
    for path in (config_home, codex_home, home_dir, state_dir, artifacts):
        path.mkdir(parents=True, exist_ok=True)

    # A wrapper that stalls the (``--remote``) TUI launch past the deadline,
    # then exec-s the real codex -- exactly the supported command-override hook
    # the runner applies at ``harness.codex-native.command`` (isaac's shape).
    codex_path = shutil.which("codex")
    wrapper = work / "slow-codex.sh"
    wrapper.write_text(
        f'#!/usr/bin/env bash\nsleep {_COLDSTART_DELAY_S}\nexec "{codex_path}" "$@"\n'
    )
    wrapper.chmod(0o755)

    # A configured provider makes the launch route normally (login_required is
    # False), so it takes the deadline-bounded wait_for_thread_started path.
    # It points at the in-test mock Responses server so a surviving turn can
    # complete with a real (canned) assistant reply once the thread starts.
    mock_model_server = ThreadingHTTPServer(("127.0.0.1", 0), _MockResponsesHandler)
    threading.Thread(target=mock_model_server.serve_forever, daemon=True).start()
    mock_model_url = f"http://127.0.0.1:{mock_model_server.server_address[1]}/v1"
    (config_home / "config.yaml").write_text(
        f"""\
providers:
  codex-e2e-mock:
    kind: key
    default: openai
    openai:
      base_url: "{mock_model_url}"
      api_key: "sk-e2e-mock"
      wire_api: responses
      models:
        default: mock-model
harness:
  codex-native:
    command: "{wrapper}"
""",
        encoding="utf-8",
    )

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    binding_token = secrets.token_urlsafe(32)

    from omnigent.runner.identity import token_bound_runner_id

    runner_id = token_bound_runner_id(binding_token)

    shared_env = {
        **_no_proxy_env(),
        "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        "OMNIGENT_CONFIG_HOME": str(config_home),
        "OMNIGENT_CODEX_NATIVE_STATE_DIR": str(state_dir),
        "CODEX_HOME": str(codex_home),
        "HOME": str(home_dir),
    }
    server_env = {**shared_env, "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token}
    runner_env = {
        **shared_env,
        "OMNIGENT_RUNNER_ID": runner_id,
        "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
        "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
        "RUNNER_SERVER_URL": base_url,
    }

    server_log = work / "server.log"
    runner_log = work / "runner.log"
    server_handle = server_log.open("w")
    runner_handle = runner_log.open("w")
    server_proc: subprocess.Popen[bytes] | None = None
    runner_proc: subprocess.Popen[bytes] | None = None
    session_id: str | None = None
    try:
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
                f"sqlite:///{work}/test.db",
                "--artifact-location",
                str(artifacts),
            ],
            env=server_env,
            stdout=server_handle,
            stderr=subprocess.STDOUT,
            cwd=str(_REPO_ROOT),
        )
        runner_proc = subprocess.Popen(
            [sys.executable, "-m", "omnigent.runner._entry"],
            env=runner_env,
            stdout=runner_handle,
            stderr=subprocess.STDOUT,
            cwd=str(_REPO_ROOT),
        )

        deadline = time.monotonic() + _HEALTH_TIMEOUT_S
        online = False
        while time.monotonic() < deadline:
            if server_proc.poll() is not None or runner_proc.poll() is not None:
                break
            try:
                if _client.get(f"{base_url}/health", timeout=2).status_code == 200:
                    status = _client.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=2)
                    if status.status_code == 200 and status.json().get("online"):
                        online = True
                        break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        if not online:
            raise RuntimeError(
                "slow-coldstart codex rig did not come online within "
                f"{_HEALTH_TIMEOUT_S:.0f}s.\nServer log:\n{server_log.read_text()[-3000:]}\n"
                f"Runner log:\n{runner_log.read_text()[-3000:]}"
            )

        session_id = _create_native_codex_session(base_url, runner_id)
        yield (base_url, session_id, runner_log)
    finally:
        if session_id is not None:
            with contextlib.suppress(httpx.HTTPError):
                _client.delete(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
        for proc in (runner_proc, server_proc):
            if proc is not None and proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
        for proc in (runner_proc, server_proc):
            if proc is not None:
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
        server_handle.close()
        runner_handle.close()
        mock_model_server.shutdown()
        mock_model_server.server_close()


@pytest.mark.timeout(400)
def test_codex_native_slow_coldstart_first_turn_survives_thread_start_deadline(
    page: Page,
    slow_coldstart_codex_session: tuple[str, str, Path],
) -> None:
    """The first chat turn must survive a slow TUI cold-start, not die on the deadline.

    Journey (the reported one): open a codex-native session whose TUI is
    still cold-starting, send the first chat message, and watch the outcome
    in the SPA. While the bug is live the background
    ``wait_for_thread_started`` is cancelled by the 30s deadline, thread
    discovery is abandoned, and the turn fails with the ``startup timed out``
    / ``never started a thread`` executor error, which the SPA renders as an
    error pill -- exactly what this test rejects.
    """
    base_url, session_id, runner_log = slow_coldstart_codex_session
    page.goto(f"{base_url}/c/{session_id}")
    _ensure_chat_view(page)

    _send(page, "Review this diff and reply with your findings.")
    sent_at = time.monotonic()

    # Wait for the turn to reach a terminal, user-visible outcome: either an
    # assistant reply (thread started, model responded) or an error pill.
    outcome = page.locator(_ERROR_PILL).or_(page.locator(_ASSISTANT))
    expect(outcome.first).to_be_visible(timeout=int(_TURN_OUTCOME_TIMEOUT_S * 1000))
    elapsed = time.monotonic() - sent_at

    # The durable assertion runs against the canonical transcript, not the
    # pill's summarized text: no error item of this turn may be the
    # thread-start deadline timeout.
    items = _client.get(f"{base_url}/v1/sessions/{session_id}/items?limit=50", timeout=10.0)
    items.raise_for_status()
    error_messages = [
        str(item.get("message", ""))
        for item in items.json()["data"]
        if item.get("type") == "error"
    ]
    timed_out_errors = [
        message
        for message in error_messages
        if _STARTUP_TIMEOUT_MARKER in message or _THREAD_NEVER_STARTED_MARKER in message
    ]
    assert not timed_out_errors, (
        "codex-native turn burned the thread-start deadline while the TUI was "
        f"still cold-starting (after {elapsed:.0f}s) instead of forwarding once "
        f"the thread started or failing fast with a clear error: "
        f"{timed_out_errors[0][:500]}\n"
        f"runner log tail:\n{runner_log.read_text()[-1500:]}"
    )

    # Forwarding proof: the surviving turn reached the mock model and its
    # reply was mirrored back into the canonical transcript.
    assistant_texts = [
        str(item.get("content", ""))
        for item in items.json()["data"]
        if item.get("type") == "message" and item.get("role") == "assistant"
    ]
    assert any(_MOCK_REPLY_TEXT in text for text in assistant_texts), (
        "chat did not forward after the slow cold-start: no assistant reply "
        f"was mirrored into the transcript (after {elapsed:.0f}s). "
        f"items={items.json()['data']!r}"[:2500]
        + f"\nrunner log tail:\n{runner_log.read_text()[-1500:]}"
    )

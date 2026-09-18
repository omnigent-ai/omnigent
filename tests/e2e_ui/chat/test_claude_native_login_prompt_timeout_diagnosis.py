"""E2E: a claude-native readiness timeout must identify a pending interactive prompt.

When the Claude Code terminal is parked on an interactive gate (a login /
consent prompt such as ``Select login method``) instead of its composer, a
message sent from the web UI trips the readiness gate
(``bridge._wait_for_claude_prompt_ready``) and the turn fails. No error the
user receives identifies the pending prompt: the readiness gate raises the
same generic "input prompt never rendered" diagnosis a blank or crashed
terminal produces, and on the live-pane path even that is masked — the
harness stream drops first and the turn surfaces only a bare "Harness
stream connection error." Either way the user is never told that finishing
the prompt in the terminal would unblock the session.

The journey: open a fresh Claude Code (claude-native) session whose terminal
is waiting on an interactive login prompt, send the first message from the
web composer, and wait for the turn's outcome.

* Buggy build: the turn's persisted error never mentions the interactive
  gate — only a generic timeout or stream-drop message. This test FAILS.
* Fixed build: the error identifies the pending interactive gate and directs
  the user to complete it in the terminal — while the turn still fails (the
  diagnosis must not change severity, declare the process healthy from
  visible text alone, or extend the readiness deadline). This test PASSES.

The rig mirrors ``test_claude_native_slow_ready_first_prompt``: a dedicated
server + runner pair whose claude-native harness command
(``OMNIGENT_CLAUDE_PATH``, the documented override) is a stub that renders a
generic login prompt (reserved example URLs only) and never mounts the
composer, so the readiness gate deterministically expires against a live,
input-waiting pane. No real ``claude`` CLI or live provider is needed.
"""

from __future__ import annotations

import contextlib
import os
import re
import secrets
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import _create_native_claude_session
from tests.e2e_ui.messages.test_message_render_parity import (
    _ensure_chat_view,
    _select_view_mode,
    _send,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]

# Boot budget for the spawned server + runner pair.
_HEALTH_TIMEOUT_S = 60.0
# The readiness gate's base budget (30s) extends to the slow-boot hard cap
# (180s) while the stub's pane stays verifiably alive, so the user-visible
# error lands only after that cap.
_ERROR_OUTCOME_TIMEOUT_S = 300.0
# claude-native auto-launch of the stub terminal + WS attach.
_TERMINAL_READY_TIMEOUT_MS = 120_000
# How long the persisted error item may lag the rendered error pill.
_ERROR_ITEM_SETTLE_S = 60.0

_ERROR_PILL = '[data-testid="error-pill"]'
_TERMINAL_VIEW = '[data-testid="terminal-view"]'

# Model baked into the rig's mock anthropic provider config (matches
# conftest._CLAUDE_MOCK_MODEL). The stub never reaches the LLM; the provider
# config only keeps the claude-native launch path identical to production.
_CLAUDE_MOCK_MODEL = "claude-sonnet-4-20250514"

_FIRST_PROMPT = "Summarize the repository layout."

# The interactive gate the stub terminal parks on. Generic wording and
# reserved example hosts only. Every line is kept short (< 45 columns) so a
# narrow pane cannot wrap one across two capture lines, which would defeat
# the pane-line stripping below.
_LOGIN_PANE_LINES = [
    "Welcome to Claude Code",
    "Select login method:",
    "  1. Claude account",
    "  2. Console account",
    "Visit https://device.example.com/code",
    "Enter code EXAMPLE-1234",
    "Waiting...",
]

# What a fixed diagnosis must do, judged only on the error's own words (the
# raw pane tail is stripped first so its "login" cannot satisfy the check):
# name the interactive gate, and direct the user to complete it.
_GATE_IDENTIFIED_RE = re.compile(r"interactive|login|log in|sign[- ]?in|consent|authenticat", re.I)
_USER_DIRECTED_RE = re.compile(r"complete|finish|answer|respond|in the terminal", re.I)


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


def _write_login_gate_claude_stub(bin_dir: Path) -> Path:
    """Write a ``claude`` stub that parks on an interactive login prompt.

    The stub renders a generic login-method prompt and then sleeps forever,
    so the pane stays alive and input-waiting while the composer never
    mounts — the state a real Claude Code sits in when it needs the user to
    finish a login or consent flow in the terminal.

    :param bin_dir: Directory to write the stub into.
    :returns: The absolute path of the stub executable.
    """
    lines = "\n".join(f"echo {shlex.quote(line)}" for line in _LOGIN_PANE_LINES)
    stub = bin_dir / "claude"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        "# Rig: a Claude Code launch waiting on an interactive login gate.\n"
        f"{lines}\n"
        "while true; do sleep 60; done\n"
    )
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return stub


def _await_error_items(
    base_url: str, session_id: str, *, timeout_s: float = _ERROR_ITEM_SETTLE_S
) -> list[str]:
    """Poll the transcript until the failed turn's error item is persisted.

    :param base_url: Base URL of the rig's server.
    :param session_id: Session/conversation identifier.
    :param timeout_s: How long to keep polling before returning what is there.
    :returns: Error item messages from the newest transcript page.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        items = _client.get(
            f"{base_url}/v1/sessions/{session_id}/items",
            params={"limit": 100, "order": "desc"},
            timeout=10.0,
        )
        items.raise_for_status()
        messages = [
            str(item.get("message", ""))
            for item in items.json()["data"]
            if item.get("type") == "error"
        ]
        if messages or time.monotonic() >= deadline:
            return messages
        time.sleep(0.5)


def _strip_pane_lines(message: str) -> str:
    """Drop the stub pane's own lines from an error message.

    The buggy error attaches the raw pane tail, whose ``Select login
    method`` text would otherwise satisfy the gate-identification check by
    accident. A message line is dropped when its text appears inside one of
    the stub's pane lines, which also catches a line the pane wrapped.

    :param message: The full error message.
    :returns: The message's own diagnostic words, pane echo removed.
    """
    kept = []
    for line in message.splitlines():
        text = line.strip().lstrip("…").strip()
        if text and any(text in pane_line for pane_line in _LOGIN_PANE_LINES):
            continue
        kept.append(line)
    return "\n".join(kept)


@pytest.fixture
def login_gate_claude_session(
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str]]:
    """A claude-native session whose terminal is stuck on a login prompt.

    Spawns a dedicated server + runner whose claude-native harness command
    (``OMNIGENT_CLAUDE_PATH``) is the login-gate stub, with an isolated
    ``HOME`` / ``OMNIGENT_CONFIG_HOME`` carrying a mock anthropic provider,
    then creates and binds the same claude-native wrapper session
    ``omnigent claude`` ships.

    :returns: ``(base_url, session_id)``.
    """
    if shutil.which("tmux") is None:
        pytest.skip("tmux is required for the claude-native terminal rig")

    work = tmp_path_factory.mktemp("claude_login_gate")
    config_home = work / "config-home"
    home_dir = work / "home"
    stub_bin = work / "stub-bin"
    artifacts = work / "artifacts"
    for path in (config_home, home_dir, stub_bin, artifacts):
        path.mkdir(parents=True, exist_ok=True)
    stub = _write_login_gate_claude_stub(stub_bin)

    (config_home / "config.yaml").write_text(
        "providers:\n"
        "  mock-claude:\n"
        "    kind: key\n"
        "    default: [anthropic]\n"
        "    anthropic:\n"
        f'      base_url: "{mock_llm_server_url}"\n'
        '      api_key: "mock-key"\n'
        "      models:\n"
        f"        default: {_CLAUDE_MOCK_MODEL}\n"
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
        "HOME": str(home_dir),
        # Force the mock provider even if the CI env carries a real
        # LLM_API_KEY: the rig must exercise the readiness gate, not a live
        # gateway.
        "LLM_API_KEY": "",
    }
    server_env = {**shared_env, "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token}
    runner_env = {
        **shared_env,
        "OMNIGENT_RUNNER_ID": runner_id,
        "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
        "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
        "RUNNER_SERVER_URL": base_url,
        # The fault injection: the runner's claude-native terminal launches
        # the login-gate stub instead of the real Claude Code CLI.
        "OMNIGENT_CLAUDE_PATH": str(stub),
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
            with contextlib.suppress(httpx.HTTPError):
                if _client.get(f"{base_url}/health", timeout=2).status_code == 200:
                    status = _client.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=2)
                    if status.status_code == 200 and status.json().get("online"):
                        online = True
                        break
            time.sleep(0.5)
        if not online:
            raise RuntimeError(
                "login-gate claude rig did not come online within "
                f"{_HEALTH_TIMEOUT_S:.0f}s.\nServer log:\n{server_log.read_text()[-3000:]}\n"
                f"Runner log:\n{runner_log.read_text()[-3000:]}"
            )

        session_id = _create_native_claude_session(base_url, runner_id)
        yield (base_url, session_id)
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


@pytest.mark.timeout(600)
def test_claude_native_readiness_timeout_identifies_pending_login_prompt(
    page: Page,
    login_gate_claude_session: tuple[str, str],
) -> None:
    """A readiness timeout against a login-gated terminal must say so.

    Journey: open a fresh claude-native session whose terminal is waiting on
    an interactive login prompt, send the first message from the web
    composer, and read the turn's error. The error must identify the pending
    interactive gate and direct the user to complete it in the terminal —
    not just repeat the generic "input prompt never rendered" diagnosis a
    blank terminal gets.
    """
    base_url, session_id = login_gate_claude_session

    page.goto(f"{base_url}/c/{session_id}")

    # The session is terminal-first: wait for the runner to create the
    # session terminal (the login-gate stub) and attach, so the journey
    # starts from the state the reporter saw — a pane asking for a login.
    expect(page.get_by_test_id("view-mode-toggle")).to_be_visible(
        timeout=_TERMINAL_READY_TIMEOUT_MS
    )
    _select_view_mode(page, "Terminal")
    terminal = page.locator(_TERMINAL_VIEW).last
    expect(terminal).to_have_attribute(
        "data-state", "connected", timeout=_TERMINAL_READY_TIMEOUT_MS
    )
    # Let the stub's login prompt render in the attached pane so the journey
    # visibly starts from the interactive gate.
    page.wait_for_timeout(3_000)

    _ensure_chat_view(page)
    _send(page, _FIRST_PROMPT)

    # The turn must still fail — the fix changes only the diagnosis, never
    # the severity. The wait covers the readiness gate's slow-boot hard cap.
    expect(page.locator(_ERROR_PILL).first).to_be_visible(
        timeout=int(_ERROR_OUTCOME_TIMEOUT_S * 1000)
    )

    error_messages = _await_error_items(base_url, session_id)
    assert error_messages, "the failed turn persisted no error item"
    combined = "\n".join(error_messages)

    diagnostic = _strip_pane_lines(combined)
    assert _GATE_IDENTIFIED_RE.search(diagnostic), (
        "readiness-timeout error does not identify the pending interactive "
        f"login/consent gate; the user got only the generic diagnosis: {combined!r}"
    )
    assert _USER_DIRECTED_RE.search(diagnostic), (
        "readiness-timeout error does not direct the user to complete the "
        f"pending prompt in the terminal: {combined!r}"
    )

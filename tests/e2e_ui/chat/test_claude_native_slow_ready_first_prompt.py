"""E2E: a claude-native first prompt must survive a slow-to-become-ready terminal.

On a host where connecting/booting the Claude Code terminal is slow, the
terminal shows output but takes longer than 30s to mount its input prompt.
The executor's ``_wait_for_claude_prompt_ready`` gate gives up at a hard 30s,
reaps the terminal, and the user's very first web-composer prompt is silently
dropped — the reported failure was::

    RuntimeError: inner executor error: Claude Code terminal did not become
    ready within 30.0s (input prompt never rendered in 197 polls, 0 empty
    captures). The message was not delivered.

The prompt never reaches the agent, the task cannot proceed, and the session
is abandoned. This is the "slow host connect" case reporters hit on remote
sandbox hosts: the terminal *would* have become ready given more time.

What this test drives
---------------------
The rig launches the session's Claude Code terminal through a wrapper
(``OMNIGENT_CLAUDE_PATH``, the documented harness-command override) that
prints boot output — so ``capture-pane`` frames are non-empty, matching the
reported "0 empty captures" — sleeps past the 30s readiness gate, and only
then exec's the real ``claude`` CLI. So the terminal becomes ready *late*,
exactly like a slow host connect.

The journey (the reported one): open a fresh Claude Code (claude-native)
session, send the first prompt from the web composer while the terminal is
still booting, and wait for the turn's outcome. The first prompt must be
delivered and answered once the terminal is ready.

* Buggy build (hard 30s gate): the gate times out before the wrapper finishes
  booting, the terminal is reaped, the prompt is silently dropped, and the
  turn fails — no assistant reply ever appears. This test FAILS.
* Fixed build (readiness detection that tolerates a slow terminal): the gate
  waits for the terminal to become ready, the first prompt is delivered, and
  the agent answers. This test PASSES.

Making the failure deterministic
--------------------------------
The wrapper's boot delay (:data:`_READY_DELAY_S`) is set comfortably past
the 30s gate, so the buggy path is hit on every run — no timing races. The
real ``claude`` CLI runs against the mock LLM (a mock anthropic provider is
written into the rig's isolated ``OMNIGENT_CONFIG_HOME``), so no live
Anthropic credentials are needed and the delivered turn gets a mock reply.

The rig mirrors ``test_codex_native_headless_login_timeout``: a dedicated
server + runner pair (own ``HOME`` / ``OMNIGENT_CONFIG_HOME``) so the wrapper
binary and the temp config cannot leak into other tests.
"""

from __future__ import annotations

import contextlib
import importlib.util
import logging
import os
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import sysconfig
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

_log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[3]

# Boot budget for the spawned server + runner pair.
_HEALTH_TIMEOUT_S = 60.0
# Seconds the wrapper prints output and sleeps before exec'ing the real
# ``claude``. Must comfortably exceed the executor's hard 30s readiness gate
# (``_wait_for_claude_prompt_ready`` / ``_TMUX_READY_TIMEOUT_S``) so the buggy
# path reaps the terminal before the terminal becomes ready, dropping the
# first prompt on every run.
_READY_DELAY_S = 45
# The turn must reach a terminal outcome within: gate wait + slow boot + a
# mock LLM turn + terminal attach.
_TURN_OUTCOME_TIMEOUT_S = 200.0
# claude-native auto-launch of the (slow) terminal + WS attach.
_TERMINAL_READY_TIMEOUT_MS = 120_000

_ERROR_PILL = '[data-testid="error-pill"]'
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_WORKING = '[data-testid="working-indicator"]'
_TERMINAL_VIEW = '[data-testid="terminal-view"]'

# Model baked into the rig's mock anthropic provider config (matches
# conftest._CLAUDE_MOCK_MODEL).
_CLAUDE_MOCK_MODEL = "claude-sonnet-4-20250514"

# The first prompt of the reported journey (a SQL anti-pattern audit) plus a
# verbatim echo token so the delivered turn is unambiguous in the transcript.
_ECHO_TOKEN = "FIRST_PROMPT_DELIVERED"
_FIRST_PROMPT = (
    "Audit the universe DAO layer for SQL anti-patterns. "
    f"Reply with exactly this token and nothing else: {_ECHO_TOKEN}"
)


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


def _write_slow_ready_claude_wrapper(bin_dir: Path, real_claude: str) -> Path:
    """Write a ``claude`` wrapper that becomes ready only after a slow boot.

    The wrapper prints boot output (so ``capture-pane`` frames are non-empty,
    matching the reported "0 empty captures"), sleeps past the executor's 30s
    readiness gate, then exec's the real ``claude`` — modeling a slow host
    connect where the TUI input box mounts late but would eventually work.

    :param bin_dir: Directory to write the wrapper into.
    :param real_claude: Absolute path of the real ``claude`` binary to exec.
    :returns: The absolute path of the wrapper executable.
    """
    wrapper = bin_dir / "claude"
    wrapper.write_text(
        "#!/usr/bin/env bash\n"
        "# Rig: a Claude Code launch that becomes ready only after a slow\n"
        "# boot (past the executor's default readiness gate).\n"
        'echo "Claude Code — connecting to host..."\n'
        'echo "This is taking longer than usual."\n'
        f"sleep {_READY_DELAY_S}\n"
        f'exec "{real_claude}" "$@"\n'
    )
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return wrapper


def _rig_python(work: Path) -> str:
    """Build an interpreter whose isolated mode can import this checkout.

    The claude-native bridge invokes its hook scripts (the transcript
    forwarder that mirrors the TUI into the web session) as
    ``<runner python> -I -m omnigent...``. ``-I`` drops ``PYTHONPATH``, so
    when this checkout is importable only via ``PYTHONPATH`` (the CI
    worktree layout) every hook dies with ``ModuleNotFoundError`` and the
    web transcript never receives the delivered turn. A dedicated venv
    whose ``site-packages`` carries a ``.pth`` naming the checkout (plus
    the parent environment's site-packages for dependencies) survives
    ``-I``, so the runner spawned from it produces working hooks.

    :param work: The rig's scratch directory.
    :returns: Absolute path of the rig venv's ``python``.
    """
    venv_dir = work / "rig-venv"
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(venv_dir)],
        check=True,
        capture_output=True,
    )
    site_packages = next((venv_dir / "lib").glob("python*/site-packages"))
    parent_purelib = sysconfig.get_paths()["purelib"]
    # The sibling SDK packages are resolved to their real source roots
    # because an *editable* install in the parent venv is a ``.pth`` finder
    # hook, and site only executes ``.pth`` files from real site dirs — a
    # directory added via another ``.pth`` line doesn't get its hooks run.
    roots = [str(_REPO_ROOT)]
    for pkg in ("omnigent_client", "omnigent_ui_sdk"):
        spec = importlib.util.find_spec(pkg)
        if spec is not None and spec.origin:
            root = str(Path(spec.origin).resolve().parents[1])
            if root not in roots:
                roots.append(root)
    (site_packages / "omnigent_rig.pth").write_text("\n".join([*roots, parent_purelib]) + "\n")
    return str(venv_dir / "bin" / "python")


@pytest.fixture
def slow_ready_claude_session(
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str]]:
    """A claude-native wrapper session whose Claude terminal becomes ready late.

    Spawns a dedicated server + runner whose claude-native harness command
    (``OMNIGENT_CLAUDE_PATH``) is the slow-ready wrapper, with an isolated
    ``HOME`` / ``OMNIGENT_CONFIG_HOME`` carrying a mock anthropic provider
    (so the real ``claude`` boots against the mock LLM without live
    credentials), then creates and binds the same claude-native wrapper
    session ``omnigent claude`` ships.

    :returns: ``(base_url, session_id)``.
    """
    if shutil.which("tmux") is None:
        pytest.skip("tmux is required for the claude-native terminal rig")
    real_claude = shutil.which("claude")
    if real_claude is None:
        pytest.skip("claude CLI is required for the slow-ready claude-native rig")

    work = tmp_path_factory.mktemp("claude_slow_ready")
    config_home = work / "config-home"
    home_dir = work / "home"
    wrapper_bin = work / "wrapper-bin"
    artifacts = work / "artifacts"
    for path in (config_home, home_dir, wrapper_bin, artifacts):
        path.mkdir(parents=True, exist_ok=True)
    wrapper = _write_slow_ready_claude_wrapper(wrapper_bin, real_claude)
    rig_python = _rig_python(work)

    # Mock anthropic provider so the real ``claude`` boots against the mock
    # LLM (no live credentials) — mirrors ``native_claude_mock_session``.
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
        # the slow-ready wrapper instead of the real Claude Code CLI directly.
        "OMNIGENT_CLAUDE_PATH": str(wrapper),
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
                rig_python,
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
            [rig_python, "-m", "omnigent.runner._entry"],
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
                "slow-ready claude rig did not come online within "
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


@pytest.mark.timeout(400)
def test_claude_native_first_prompt_survives_slow_terminal_boot(
    page: Page,
    slow_ready_claude_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """The first prompt must be delivered even when the terminal is slow to become ready.

    Journey (the reported one): open a fresh Claude Code (claude-native)
    session whose terminal is slow to mount its input prompt, send the first
    prompt from the web composer while it is still booting, and wait for the
    turn's outcome. While the bug is live the executor's 30s readiness gate
    times out, reaps the terminal, and the first prompt is silently dropped —
    no assistant reply ever appears, only a failure. That silent drop is
    exactly what this test rejects: the first prompt must be delivered and
    answered once the terminal becomes ready.
    """
    from tests.e2e_ui.conftest import set_fallback_mock_llm

    base_url, session_id = slow_ready_claude_session

    # Make the mock LLM echo the token so a delivered turn is unmistakable.
    set_fallback_mock_llm(mock_llm_server_url, "default", _ECHO_TOKEN)
    set_fallback_mock_llm(mock_llm_server_url, _CLAUDE_MOCK_MODEL, _ECHO_TOKEN)

    page.goto(f"{base_url}/c/{session_id}")

    # The session is terminal-first: wait for the runner to create the
    # session terminal (the slow-ready wrapper) and attach — exactly what
    # the reporter saw before sending the first prompt.
    expect(page.get_by_test_id("view-mode-toggle")).to_be_visible(
        timeout=_TERMINAL_READY_TIMEOUT_MS
    )
    _select_view_mode(page, "Terminal")
    terminal = page.locator(_TERMINAL_VIEW).last
    expect(terminal).to_have_attribute(
        "data-state", "connected", timeout=_TERMINAL_READY_TIMEOUT_MS
    )

    # Send the FIRST prompt from the web composer while the terminal is still
    # booting (the racy path): the readiness gate must hold the message until
    # the terminal is ready rather than dropping it at the 30s cap.
    _ensure_chat_view(page)
    _send(page, _FIRST_PROMPT)
    sent_at = time.monotonic()
    _log.info("first prompt sent; waiting for the turn outcome")

    # Wait for the turn to reach a terminal, user-visible outcome: either an
    # assistant reply (the prompt was delivered and answered) or an error
    # pill (the turn failed — the silent-drop bug).
    outcome = page.locator(_ERROR_PILL).or_(page.locator(_ASSISTANT))
    expect(outcome.first).to_be_visible(timeout=int(_TURN_OUTCOME_TIMEOUT_S * 1000))
    elapsed = time.monotonic() - sent_at
    _log.info("turn reached a user-visible outcome after %.0fs", elapsed)

    # Durable assertion against the canonical transcript: the first prompt
    # must have been delivered and answered (an assistant item echoing the
    # token), and the turn must not have failed with an error item.
    items = _client.get(f"{base_url}/v1/sessions/{session_id}/items?limit=50", timeout=10.0)
    items.raise_for_status()
    data = items.json()["data"]

    error_messages = [str(item.get("message", "")) for item in data if item.get("type") == "error"]
    assistant_texts = [
        block.get("text", "")
        for item in data
        if item.get("role") == "assistant" and isinstance(item.get("content"), list)
        for block in item["content"]
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    ]
    delivered = any(_ECHO_TOKEN in text for text in assistant_texts)

    assert delivered, (
        "claude-native first prompt was NOT delivered: no assistant reply "
        f"echoing {_ECHO_TOKEN!r} appeared (after {elapsed:.0f}s). The slow "
        "terminal-readiness gate dropped the first prompt. Turn errors: "
        f"{error_messages or '<none>'}"
    )

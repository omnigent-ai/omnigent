"""E2E: a claude-sdk turn must not die at startup on a broken resolved CLI.

Regression test for the reported ``[Errno 8] Exec format error`` startup
failure: on the reporter's macOS arm64 machine the
``claude`` their interactive shell runs is a working arm64 Mach-O, but the
binary the harness *resolves* and execs is not runnable, so the very first
claude-sdk chat turn dies with::

    Error · RuntimeError
    Claude SDK connect failed: Failed to start Claude Code: [Errno 8] Exec
    format error

The resolver (``_find_system_claude`` →
``omnigent._platform.resolve_cli_binary``) probes ``PATH`` and then a ladder
of global install dirs (``~/.local/bin``, ``/usr/local/bin``,
``/opt/homebrew/bin``, ``~/.npm-global/bin``, ``~/.nvm/versions/node/*/bin``)
and returns the FIRST existing executable file — without checking that the
file can actually run on this machine, and without falling through to a later
runnable candidate when it can't. A stale wrong-architecture install on an
early rung therefore shadows a perfectly good ``claude`` further down.

This rig recreates exactly that: the runner daemon's ``PATH`` has no
``claude`` (the daemon's frozen ``PATH`` misses the shell-visible install —
the situation the resolver's fallback ladder exists for), a wrong-format
binary sits at ``~/.local/bin/claude`` (an early ladder rung), and the REAL
working ``claude`` sits at ``~/.nvm/versions/node/*/bin/claude`` (a later
rung). The journey is one chat message on a claude-sdk session backed by the
mock LLM. While the bug is live the resolver picks the broken binary and the
turn fails at SDK connect (``Claude SDK connect failed`` — the raw
``[Errno 8] Exec format error`` on macOS/asyncio, an exit-127 spawn death on
a uvloop runner); once fixed the harness must reach the working CLI and the
turn completes with the mock reply.
"""

from __future__ import annotations

import contextlib
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
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import yaml
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import configure_mock_llm

_REPO_ROOT = Path(__file__).resolve().parents[3]

# Boot budget for the spawned server + runner pair.
_HEALTH_TIMEOUT_S = 90.0
# The buggy path errors within the SDK connect window; a healthy turn against
# the mock LLM completes well inside this.
_TURN_OUTCOME_TIMEOUT_S = 150.0

_ERROR_PILL = '[data-testid="error-pill"]'
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'

# The startup-failure marker of the reported bug: the executor wraps every
# CLI-spawn death at connect time in this message (claude_sdk_executor's
# ``_get_or_create_client``). On macOS/asyncio the wrapped cause is the raw
# ``[Errno 8] Exec format error``; on a uvloop runner the same broken exec
# surfaces as an exit-127 spawn death — both carry this prefix.
_CONNECT_FAILED_MARKER = "Claude SDK connect failed"

_MOCK_MODEL = "mock-broken-cli"
_MOCK_REPLY = "hello from the mock model"
_AGENT_NAME = "sdk-broken-cli-rig"

# Wrong-executable-format payload for whatever kernel runs this test: ELF
# magic on macOS, Mach-O arm64 magic everywhere else — either execs with
# ENOEXEC, the exact inverse of the reporter's wrong-format binary on macOS.
_WRONG_FORMAT_BINARY = (
    b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 64
    if sys.platform == "darwin"
    else b"\xcf\xfa\xed\xfe\x0c\x00\x00\x01" + b"\x00" * 64
)

# Proxy-blind client: CI forces an egress proxy via HTTP(S)_PROXY env vars
# that must not intercept loopback requests to the spawned server.
_client = httpx.Client(trust_env=False, timeout=15.0)

# Shared helpers use ambient ``httpx`` calls that DO trust env, so also
# exclude loopback from any forced proxy at import time.
for _var in ("NO_PROXY", "no_proxy"):
    os.environ[_var] = ",".join(filter(None, [os.environ.get(_var, ""), "127.0.0.1,localhost"]))


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _no_proxy_env() -> dict[str, str]:
    """Ambient env with loopback excluded from any forced HTTP(S) proxy."""
    env = os.environ.copy()
    for var in ("NO_PROXY", "no_proxy"):
        existing = env.get(var, "")
        env[var] = ",".join(filter(None, [existing, "127.0.0.1,localhost"]))
    return env


def _stage_reporter_home(home: Path, real_claude: str) -> None:
    """Recreate the reporter's CLI layout under a fresh ``$HOME``.

    A non-runnable ``claude`` on an early resolver-ladder rung
    (``~/.local/bin``) and the working install on a later rung
    (``~/.nvm/versions/node/*/bin`` — where npm-under-nvm global installs
    land, per the resolver's own docstring).

    :param home: The runner's staged home directory.
    :param real_claude: Absolute path to a working ``claude`` binary.
    """
    broken_dir = home / ".local" / "bin"
    broken_dir.mkdir(parents=True, exist_ok=True)
    broken = broken_dir / "claude"
    broken.write_bytes(_WRONG_FORMAT_BINARY)
    broken.chmod(0o755)

    nvm_bin = home / ".nvm" / "versions" / "node" / "v20.0.0" / "bin"
    nvm_bin.mkdir(parents=True, exist_ok=True)
    (nvm_bin / "claude").symlink_to(real_claude)


def _register_sdk_agent_session(base_url: str, mock_url: str, runner_id: str) -> str:
    """Register a mock-backed claude-sdk agent and bind its session.

    Uploads a minimal single-file spec whose executor routes
    ``ANTHROPIC_BASE_URL`` at the mock LLM (the Anthropic SDK appends
    ``/v1/messages``, so the base URL must not include ``/v1``), then binds
    the returned session to *runner_id* so events dispatch.

    :param base_url: Spawned server base URL.
    :param mock_url: Mock LLM server base URL.
    :param runner_id: The token-bound runner id to bind.
    :returns: The new session/conversation id.
    """
    from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN

    config = {
        "name": _AGENT_NAME,
        "prompt": "You are a concise assistant.",
        "executor": {
            "harness": "claude-sdk",
            "model": _MOCK_MODEL,
            "auth": {"type": "api_key", "api_key": "mock-key", "base_url": mock_url},
        },
    }
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = yaml.safe_dump(config).encode()
        info = tarfile.TarInfo(f"{_AGENT_NAME}.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    resp = _client.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", buf.getvalue(), "application/gzip")},
        headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
    )
    resp.raise_for_status()
    session_id = str(resp.json()["session_id"])
    bind = _client.patch(f"{base_url}/v1/sessions/{session_id}", json={"runner_id": runner_id})
    bind.raise_for_status()
    return session_id


@pytest.fixture
def broken_cli_claude_sdk_session(
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str]]:
    """A claude-sdk session on a rig whose resolved ``claude`` cannot exec.

    Spawns a dedicated server + runner. The runner's ``HOME`` carries the
    reporter's layout (wrong-format ``~/.local/bin/claude``, working
    ``~/.nvm/.../bin/claude``) and its ``PATH`` has no ``claude``, so the
    executor's resolver must walk the fallback ladder — and today it stops on
    the broken rung. ``OMNIGENT_CLAUDE_SDK_NO_SANDBOX`` matches the macOS
    behavior of exec'ing the resolved CLI directly (on darwin the tight
    launcher wrap is skipped anyway).

    :returns: ``(base_url, session_id)``.
    """
    real_claude = shutil.which("claude")
    if real_claude is None:
        pytest.skip("a working claude CLI is required for the broken-resolver rig")
    real_claude = str(Path(real_claude).resolve())

    work = tmp_path_factory.mktemp("claude_sdk_broken_cli")
    home_dir = work / "home"
    artifacts = work / "artifacts"
    for path in (home_dir, artifacts):
        path.mkdir(parents=True, exist_ok=True)
    _stage_reporter_home(home_dir, real_claude)

    # The runner daemon's PATH misses every dir that holds a ``claude`` —
    # the frozen-daemon-PATH situation the resolver's ladder exists for.
    clean_path = os.pathsep.join(
        p
        for p in os.environ.get("PATH", "").split(os.pathsep)
        if p and not (Path(p) / "claude").exists()
    )

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    binding_token = secrets.token_urlsafe(32)

    from omnigent.runner.identity import token_bound_runner_id

    runner_id = token_bound_runner_id(binding_token)

    shared_env = {
        **_no_proxy_env(),
        "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
    }
    server_env = {**shared_env, "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token}
    runner_env = {
        **shared_env,
        "HOME": str(home_dir),
        "PATH": clean_path,
        "OMNIGENT_CLAUDE_SDK_NO_SANDBOX": "1",
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
                "broken-cli claude-sdk rig did not come online within "
                f"{_HEALTH_TIMEOUT_S:.0f}s.\nServer log:\n{server_log.read_text()[-3000:]}\n"
                f"Runner log:\n{runner_log.read_text()[-3000:]}"
            )

        # A healthy (post-fix) turn draws these; give retries headroom.
        configure_mock_llm(
            mock_llm_server_url,
            [{"text": _MOCK_REPLY}] * 4,
            key=_MOCK_MODEL,
        )

        session_id = _register_sdk_agent_session(base_url, mock_llm_server_url, runner_id)
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
def test_claude_sdk_first_turn_survives_broken_cli_on_resolver_ladder(
    page: Page,
    broken_cli_claude_sdk_session: tuple[str, str],
) -> None:
    """The first chat turn must not die on the broken resolved ``claude``.

    Journey (the reported one): start a claude-sdk session on a machine
    whose resolvable ``claude`` install is not runnable while a working one
    exists, send the first chat message in the UI, and watch the outcome.
    While the bug is live the resolver hands the SDK the wrong-format binary
    and the turn dies at connect with the ``Claude SDK connect failed``
    startup error (``[Errno 8] Exec format error`` on macOS), rendered as an
    error pill — exactly what this test rejects. Once the harness validates
    or falls through to the runnable CLI, the same turn completes with the
    mock reply.
    """
    base_url, session_id = broken_cli_claude_sdk_session
    page.goto(f"{base_url}/c/{session_id}")

    # Locate the composer by aria-label — the placeholder mutates with state.
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill("say hello please")
    page.get_by_role("button", name="Send", exact=True).click()

    # Wait for the turn's terminal, user-visible outcome: an assistant reply
    # (CLI started, mock answered) or an error pill (the reported death).
    outcome = page.locator(_ERROR_PILL).or_(page.locator(_ASSISTANT))
    expect(outcome.first).to_be_visible(timeout=int(_TURN_OUTCOME_TIMEOUT_S * 1000))

    # Durable assertion against the canonical transcript: no error item of
    # this turn may be the CLI-startup connect failure.
    items = _client.get(f"{base_url}/v1/sessions/{session_id}/items?limit=50", timeout=10.0)
    items.raise_for_status()
    error_messages = [
        str(item.get("message", ""))
        for item in items.json()["data"]
        if item.get("type") == "error"
    ]
    connect_failures = [m for m in error_messages if _CONNECT_FAILED_MARKER in m]
    assert not connect_failures, (
        "claude-sdk turn died at CLI startup on the broken resolved binary "
        "instead of reaching the working claude install: "
        f"{connect_failures[0][:500]}"
    )

    # And the journey must actually complete: the mock-backed assistant
    # reply reaches the transcript.
    expect(page.locator(_ASSISTANT).filter(has_text=_MOCK_REPLY).first).to_be_visible(
        timeout=30_000
    )

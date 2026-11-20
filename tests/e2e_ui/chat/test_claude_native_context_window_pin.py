"""E2E: a claude-native session on a 1M-capable gateway Opus runs with a 1M context window."""

from __future__ import annotations

import atexit
import contextlib
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pyte
import pytest
import yaml
from playwright.sync_api import Page, expect
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect as ws_connect

from tests._helpers.native_session import create_native_session
from tests.e2e_ui.conftest import set_fallback_mock_llm

_REPO_ROOT = Path(__file__).resolve().parents[3]

_OPUS = "system.ai.claude-opus-5"
_SONNET = "system.ai.claude-sonnet-5"
_REPLY = "context-window pin check: acknowledged."
_ONE_MILLION = 1_000_000

_TERMINAL = '[data-testid="terminal-view"]'
_XTERM_INPUT = ".xterm-helper-textarea"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_WORKING = '[data-testid="working-indicator"]'
_HEALTH_TIMEOUT_S = 90.0
# claude-native auto-launch + first-run pre-accept + WS attach.
_TERMINAL_READY_TIMEOUT_MS = 240_000
_MOCK_TURN_TIMEOUT_MS = 120_000
_CONTEXT_READOUT_TIMEOUT_S = 60.0
_SNAPSHOT_WINDOW_TIMEOUT_S = 45.0
# ``53.1k/200k tokens (27%)`` as Claude Code's /context prints it.
_TOKENS_RE = re.compile(r"([\d.]+)\s*([km]?)/([\d.]+)\s*([km]?)\s+tokens\s*\(", re.IGNORECASE)
_UNITS = {"": 1, "k": 1_000, "m": 1_000_000}

# Ambient provider, credential, and runner state that would otherwise leak
# into the rig's provider resolution or make its runner take the zygote path.
_AMBIENT_ENV_PREFIXES = (
    "OPENAI_",
    "ANTHROPIC_",
    "CLAUDE_",
    "DATABRICKS_",
    "OMNIGENT_RUNNER_",
    "OMNIGENT_HOST_",
)
_AMBIENT_ENV_KEYS = frozenset(
    {"RUNNER_SERVER_URL", "OMNIGENT_REMOTE_AUTH_TOKEN", "OMNIGENT_CONFIG_HOME", "LLM_API_KEY"}
)

# Proxy-blind client: CI forces an egress proxy that must not intercept loopback.
_client = httpx.Client(trust_env=False)
atexit.register(_client.close)


@dataclass
class Gateway1mRig:
    """A dedicated server + runner pair whose provider pins the 1M-capable Opus."""

    base_url: str
    runner_id: str
    work: Path
    server_log: Path
    runner_log: Path

    def runner_log_tail(self, chars: int = 2500) -> str:
        """The runner's own log file under its data dir, else its captured stdout."""
        logs = sorted((self.work / "data" / "logs" / "runner").glob("*.log"))
        source = logs[-1] if logs else self.runner_log
        return source.read_text(errors="replace")[-chars:]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def write_gateway_1m_provider(config_home: Path, mock_url: str) -> Path:
    """Write the kind:key provider routing Claude Code to the mock with bare 1M-capable ids."""
    config = {
        "runner": {"idle_timeout_s": 0},
        "providers": {
            "gateway-1m-claude": {
                "kind": "key",
                "default": ["anthropic"],
                "anthropic": {
                    "base_url": mock_url,
                    "api_key": "mock-key",
                    "models": {"default": _OPUS, "opus": _OPUS, "sonnet": _SONNET},
                },
            }
        },
    }
    config_home.mkdir(parents=True, exist_ok=True)
    config_path = config_home / "config.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return config_path


def _seed_claude_first_run(claude_dir: Path, workspace: Path) -> None:
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / ".claude.json").write_text(
        json.dumps(
            {
                "hasCompletedOnboarding": True,
                "projects": {str(workspace): {"hasTrustDialogAccepted": True}},
            }
        ),
        encoding="utf-8",
    )


def _rig_env(work: Path) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(_AMBIENT_ENV_PREFIXES) and key not in _AMBIENT_ENV_KEYS
    }
    for var in ("NO_PROXY", "no_proxy"):
        env[var] = ",".join(filter(None, [env.get(var, ""), "127.0.0.1,localhost"]))
    env.update(
        PYTHONPATH=os.pathsep.join(
            filter(
                None,
                [
                    str(_REPO_ROOT),
                    str(_REPO_ROOT / "sdks" / "python-client"),
                    str(_REPO_ROOT / "sdks" / "ui"),
                    os.environ.get("PYTHONPATH", ""),
                ],
            )
        ),
        OMNIGENT_CONFIG_HOME=str(work / "config-home"),
        OMNIGENT_DATA_DIR=str(work / "data"),
        CLAUDE_CONFIG_DIR=str(work / "claude-config"),
        OMNIGENT_DISABLE_CATALOG_LOOKUP="1",
        CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
    )
    return env


def _wait_online(
    base_url: str, runner_id: str, procs: list[subprocess.Popen[bytes]], logs: list[Path]
) -> None:
    deadline = time.monotonic() + _HEALTH_TIMEOUT_S
    while time.monotonic() < deadline:
        if any(proc.poll() is not None for proc in procs):
            break
        try:
            if _client.get(f"{base_url}/health", timeout=2).status_code == 200:
                status = _client.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=2)
                if status.status_code == 200 and status.json().get("online"):
                    return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    tails = "\n".join(f"{log.name}:\n{log.read_text(errors='replace')[-3000:]}" for log in logs)
    crashed = [proc for proc in procs if proc.poll() is not None]
    reason = (
        f"a process exited early with codes {[proc.poll() for proc in crashed]}"
        if crashed
        else f"it did not come online within {_HEALTH_TIMEOUT_S:.0f}s"
    )
    raise RuntimeError(f"gateway-1m rig failed: {reason}.\n{tails}")


@contextlib.contextmanager
def gateway_1m_rig(work: Path, mock_url: str) -> Iterator[Gateway1mRig]:
    """Boot an isolated server + runner whose provider config already pins the Opus.

    The pin is written before the runner starts, so the launch reads it from its own
    ``OMNIGENT_CONFIG_HOME`` regardless of where the test process runs.
    """
    from omnigent.runner.identity import token_bound_runner_id

    write_gateway_1m_provider(work / "config-home", mock_url)
    _seed_claude_first_run(work / "claude-config", _REPO_ROOT)
    artifacts = work / "artifacts"
    artifacts.mkdir(exist_ok=True)
    env = _rig_env(work)
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    binding_token = secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(binding_token)
    server_log = work / "server.log"
    runner_log = work / "runner.log"
    procs: list[subprocess.Popen[bytes]] = []
    with server_log.open("w") as server_out, runner_log.open("w") as runner_out:
        try:
            procs.append(
                subprocess.Popen(
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
                    env={**env, "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token},
                    stdout=server_out,
                    stderr=subprocess.STDOUT,
                    cwd=str(_REPO_ROOT),
                )
            )
            procs.append(
                subprocess.Popen(
                    [sys.executable, "-m", "omnigent.runner._entry"],
                    env={
                        **env,
                        "OMNIGENT_RUNNER_ID": runner_id,
                        "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
                        "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
                        "RUNNER_SERVER_URL": base_url,
                    },
                    stdout=runner_out,
                    stderr=subprocess.STDOUT,
                    cwd=str(_REPO_ROOT),
                )
            )
            _wait_online(base_url, runner_id, procs, [server_log, runner_log])
            yield Gateway1mRig(
                base_url=base_url,
                runner_id=runner_id,
                work=work,
                server_log=server_log,
                runner_log=runner_log,
            )
        finally:
            for proc in procs:
                if proc.poll() is None:
                    proc.send_signal(signal.SIGTERM)
            for proc in procs:
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)


@pytest.fixture
def gateway_1m_claude_rig(
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
    request: pytest.FixtureRequest,
) -> Iterator[Gateway1mRig]:
    if request.config.getoption("--ui-base-url"):
        pytest.skip("the 1M-window journey needs its own spawned server + runner")
    if shutil.which("claude") is None:
        pytest.skip("claude CLI is required for the native 1M-window e2e")
    work = tmp_path_factory.mktemp("claude_1m_window")
    with gateway_1m_rig(work, mock_llm_server_url) as rig:
        yield rig


def create_pinned_claude_session(rig: Gateway1mRig) -> str:
    """Create the claude-native wrapper session; binding the rig runner launches Claude Code."""
    created = create_native_session(
        _client, rig.base_url, harness="claude", metadata={"workspace": str(_REPO_ROOT)}
    )
    session_id = str(created["session_id"])
    try:
        bind = _client.patch(
            f"{rig.base_url}/v1/sessions/{session_id}",
            json={"runner_id": rig.runner_id},
            timeout=10.0,
        )
        bind.raise_for_status()
    except httpx.HTTPError:
        # The session (and its auto-launched terminal) exists before the bind
        # returns, so clean it up before propagating a bind failure.
        with contextlib.suppress(httpx.HTTPError):
            _client.delete(f"{rig.base_url}/v1/sessions/{session_id}", timeout=10.0)
        raise
    return session_id


def _terminal_id(base_url: str, session_id: str) -> str | None:
    response = _client.get(f"{base_url}/v1/sessions/{session_id}/resources", timeout=10.0)
    response.raise_for_status()
    payload = response.json()
    rows = payload.get("data") if isinstance(payload, dict) else payload
    for row in rows or []:
        if isinstance(row, dict) and row.get("type") == "terminal":
            return str(row["id"])
    return None


def pane_text(base_url: str, session_id: str, *, seconds: float = 2.0) -> str:
    """Render the terminal screen from a read-only attach, seeded by ``capture-pane``."""
    terminal_id = _terminal_id(base_url, session_id)
    if terminal_id is None:
        return ""
    url = (
        base_url.replace("http://", "ws://", 1).replace("https://", "wss://", 1)
        + f"/v1/sessions/{session_id}/resources/terminals/{terminal_id}/attach?read_only=true"
    )
    raw = bytearray()
    deadline = time.monotonic() + seconds
    # The terminal can drop the attach while Claude Code is still launching; a
    # transient close just renders what arrived so wait_pane retries its poll.
    with contextlib.suppress(ConnectionClosed, OSError):
        with ws_connect(url, open_timeout=15, max_size=None) as ws:
            while time.monotonic() < deadline:
                try:
                    frame = ws.recv(timeout=max(0.1, deadline - time.monotonic()))
                except TimeoutError:
                    break
                if isinstance(frame, bytes):
                    raw.extend(frame)
    screen = pyte.Screen(220, 200)
    pyte.ByteStream(screen).feed(bytes(raw))
    return "\n".join(line.rstrip() for line in screen.display)


def wait_pane(base_url: str, session_id: str, needle: str, *, timeout_s: float) -> str:
    deadline = time.monotonic() + timeout_s
    text = ""
    while time.monotonic() < deadline:
        text = pane_text(base_url, session_id)
        if needle.lower() in text.lower():
            return text
        time.sleep(1.0)
    return text


def context_window_from_readout(pane: str) -> tuple[int | None, str]:
    """Parse the window out of Claude Code's ``/context`` usage line."""
    for line in pane.splitlines():
        match = _TOKENS_RE.search(line)
        if match:
            window = float(match.group(3)) * _UNITS[match.group(4).lower()]
            return int(window), line.strip()
    return None, ""


def open_terminal(page: Page, base_url: str, session_id: str) -> None:
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_test_id("view-mode-toggle")).to_be_visible(
        timeout=_TERMINAL_READY_TIMEOUT_MS
    )
    page.get_by_test_id("view-mode-terminal").click()
    terminal = page.locator(_TERMINAL).last
    expect(terminal).to_have_attribute(
        "data-state", "connected", timeout=_TERMINAL_READY_TIMEOUT_MS
    )
    pane = wait_pane(base_url, session_id, "manual mode", timeout_s=90.0)
    assert "manual mode" in pane.lower(), f"terminal never reached manual mode:\n{pane[-1500:]}"


def type_slash_command(page: Page, command: str) -> None:
    xterm_input = page.locator(_TERMINAL).last.locator(_XTERM_INPUT)
    expect(xterm_input).to_be_attached(timeout=30_000)
    xterm_input.focus()
    page.keyboard.type(command, delay=40)
    page.wait_for_timeout(1000)
    page.keyboard.press("Enter")


def send_turn(page: Page) -> None:
    page.get_by_test_id("view-mode-chat").click()
    composer = page.get_by_role("textbox", name="Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill("Reply with one short sentence.")
    page.get_by_role("button", name="Send", exact=True).click()
    expect(page.locator(_ASSISTANT, has_text=_REPLY).first).to_be_visible(
        timeout=_MOCK_TURN_TIMEOUT_MS
    )
    expect(page.locator(_WORKING)).to_have_count(0, timeout=_MOCK_TURN_TIMEOUT_MS)


def snapshot_context_window(base_url: str, session_id: str) -> int | None:
    deadline = time.monotonic() + _SNAPSHOT_WINDOW_TIMEOUT_S
    while time.monotonic() < deadline:
        response = _client.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
        if response.status_code != 200:
            # A transient 5xx may carry a non-JSON body; retry rather than fail
            # the poll with an opaque decode error.
            time.sleep(2.0)
            continue
        snapshot = response.json()
        window = snapshot.get("context_window")
        if isinstance(window, int) and snapshot.get("last_total_tokens"):
            return window
        time.sleep(2.0)
    return None


@pytest.mark.timeout(600)
def test_claude_native_1m_capable_default_model_gets_1m_window(
    request: pytest.FixtureRequest,
    mock_llm_server_url: str,
    gateway_1m_claude_rig: Gateway1mRig,
) -> None:
    """Claude Code reports a 1M window for the pinned 1M-capable Opus, and so does Omnigent."""
    rig = gateway_1m_claude_rig
    for key in ("default", _OPUS, _SONNET):
        set_fallback_mock_llm(mock_llm_server_url, key, _REPLY)
    session_id = create_pinned_claude_session(rig)
    try:
        page: Page = request.getfixturevalue("page")
        open_terminal(page, rig.base_url, session_id)
        type_slash_command(page, "/context")
        pane = wait_pane(
            rig.base_url, session_id, "tokens (", timeout_s=_CONTEXT_READOUT_TIMEOUT_S
        )
        # Setup check, not the bug: the launch must be running the pinned model at all.
        assert _OPUS in pane, (
            f"Claude Code is not running the pinned model {_OPUS}, so the provider pin "
            f"never reached this launch (setup failure, not the window bug):\n{pane[-1500:]}\n"
            f"Runner log tail:\n{rig.runner_log_tail()}"
        )
        claude_window, tokens_line = context_window_from_readout(pane)
        assert claude_window is not None, f"/context printed no usage line:\n{pane[-2000:]}"

        # Claude Code sizes the window from the launch model id, before any
        # composer turn: a 1M-capable pinned model must size the session at 1M.
        assert claude_window >= _ONE_MILLION, (
            f"{_OPUS} is a 1M-capable model, but Claude Code sized the session at "
            f"{claude_window:,} tokens ({tokens_line!r})"
        )

        send_turn(page)
        omnigent_window = snapshot_context_window(rig.base_url, session_id)
        assert (omnigent_window or 0) >= _ONE_MILLION, (
            f"Claude Code reported {claude_window:,} tokens for {_OPUS}, but Omnigent's "
            f"session snapshot sizes the composer ring at context_window={omnigent_window}"
        )
    finally:
        with contextlib.suppress(httpx.HTTPError):
            _client.delete(f"{rig.base_url}/v1/sessions/{session_id}", timeout=10.0)

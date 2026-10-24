"""E2E: the first web message of a fresh hermes-native session must survive a slow Hermes boot.

A new Hermes TUI can take 15-25 s to become input-ready (plugin discovery,
state.db init, MCP registration, agent init); a web message sent in that window
is pasted into a pane nothing is reading yet, so the bridge must keep delivering
until Hermes accepts it - exactly once.

The rig launches the session's Hermes TUI through a wrapper (``OMNIGENT_HERMES_PATH``)
that holds startup for :data:`_BOOT_DELAY_S` seconds before exec'ing the real
``hermes``, which talks to the mock LLM through a ``custom`` provider in the rig's
isolated ``HOME``. The test creates a fresh hermes-native session, sends the first
composer message while the TUI is still booting, and checks that the turn does not
fail, Hermes's store holds exactly one user row, and the reply reaches the chat.
``HERMES_BOOT_DELAY_S=0`` drives the same journey against Hermes's real boot time.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import secrets
import shlex
import shutil
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import _create_native_hermes_session
from tests.e2e_ui.messages.test_message_render_parity import _ensure_chat_view, _select_view_mode

_log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[3]

_HEALTH_TIMEOUT_S = 90.0
# Seconds the wrapper holds Hermes startup before exec'ing the real CLI. Hermes
# itself then needs a few seconds, so the TUI becomes input-ready inside the
# reported 15-25 s window, well after the bridge's first paste.
_BOOT_DELAY_S = float(os.environ.get("HERMES_BOOT_DELAY_S", "18"))
# Send -> user-visible outcome: delayed boot + the bridge's delivery budget +
# first-turn MCP discovery + a mock LLM turn.
_TURN_OUTCOME_TIMEOUT_S = 240.0
_TRANSCRIPT_SETTLE_S = 60.0
_PANE_POLL_S = 0.5

_ERROR_PILL = '[data-testid="error-pill"]'
_USER = '[data-testid="message-bubble"][data-role="user"]'
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_TERMINAL_VIEW = '[data-testid="terminal-view"]'

_HERMES_MOCK_MODEL = "gpt-4o"
_ECHO_TOKEN = "FIRST_HERMES_MESSAGE_DELIVERED"
_NOT_ACCEPTED = "hermes did not accept the message"

# Proxy-blind client: CI forces an egress proxy via HTTP(S)_PROXY env vars
# that must not intercept loopback requests to the spawned server.
_client = httpx.Client(trust_env=False)
for _var in ("NO_PROXY", "no_proxy"):
    os.environ[_var] = ",".join(filter(None, [os.environ.get(_var, ""), "127.0.0.1,localhost"]))


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _first_message(marker: str) -> str:
    return (
        f"First message of a fresh Hermes session, marker {marker}. "
        f"Reply with exactly this token and nothing else: {_ECHO_TOKEN}"
    )


def _resolve_runnable_hermes() -> str | None:
    """Return the real ``hermes`` the runner would launch, or ``None`` when unusable."""
    import click

    from omnigent.harnesses.hermes_native.main import resolve_hermes_executable

    try:
        executable = resolve_hermes_executable()
    except click.ClickException:
        return None
    try:
        probe = subprocess.run(
            [executable, "--help"], capture_output=True, text=True, timeout=120, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return executable if probe.returncode == 0 else None


def _write_slow_boot_hermes_wrapper(bin_dir: Path, real_hermes: str, delay_s: float) -> Path:
    """Write a ``hermes`` wrapper that becomes input-ready only after a slow boot."""
    wrapper = bin_dir / "hermes"
    wrapper.write_text(
        "#!/usr/bin/env bash\n"
        "# Rig: a Hermes launch that is still booting when the first message arrives.\n"
        'echo "Hermes Agent - starting up..."\n'
        'echo "Discovering plugins and MCP servers..."\n'
        f"sleep {delay_s:g}\n"
        f'exec {shlex.quote(real_hermes)} "$@"\n'
    )
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return wrapper


def _state_db_rows(db: Path, marker: str) -> list[tuple[int, str, str]]:
    """``(id, role, content)`` of Hermes ``messages`` rows containing *marker*."""
    if not db.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error:
        return []
    try:
        rows = con.execute(
            "SELECT id, role, coalesce(content, '') FROM messages ORDER BY id"
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()
    return [(int(r[0]), str(r[1]), str(r[2])) for r in rows if marker in str(r[2])]


def _transcript(base_url: str, session_id: str) -> list[dict]:
    items = _client.get(
        f"{base_url}/v1/sessions/{session_id}/items",
        params={"limit": 200, "order": "asc"},
        timeout=10.0,
    )
    items.raise_for_status()
    return list(items.json()["data"])


def _await_transcript_outcome(
    base_url: str, session_id: str, *, timeout_s: float = _TRANSCRIPT_SETTLE_S
) -> tuple[list[str], list[str]]:
    """Poll until an error item or the echoed token is persisted; returns
    ``(error_messages, assistant_texts)``."""
    deadline = time.monotonic() + timeout_s
    while True:
        data = _transcript(base_url, session_id)
        errors = [str(i.get("message", "")) for i in data if i.get("type") == "error"]
        assistant_texts = [
            block.get("text", "")
            for item in data
            if item.get("role") == "assistant" and isinstance(item.get("content"), list)
            for block in item["content"]
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        ]
        if (
            errors
            or any(_ECHO_TOKEN in t for t in assistant_texts)
            or time.monotonic() >= deadline
        ):
            return errors, assistant_texts
        time.sleep(0.5)


class _PaneWatch:
    """Dump the Hermes tmux pane every poll until stopped; note readiness and marker sightings."""

    def __init__(self, session_id: str, marker: str, out_dir: Path) -> None:
        from omnigent.harnesses.hermes_native.bridge import bridge_dir_for_session_id

        self.bridge_dir = bridge_dir_for_session_id(session_id)
        self.state_db = self.bridge_dir / "hermes_home" / "state.db"
        self.marker = marker
        self.out_dir = out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.events: list[tuple[float, str]] = []
        self.last_pane = ""
        self._stop = threading.Event()
        self._t0 = time.monotonic()
        self._thread = threading.Thread(target=self._run, name="hermes-pane-watch", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=15)

    def note(self, what: str) -> None:
        self.events.append((round(time.monotonic() - self._t0, 1), what))

    def _run(self) -> None:
        from omnigent.harnesses.hermes_native.bridge import read_tmux_info

        info = None
        while not self._stop.is_set() and info is None:
            info = read_tmux_info(self.bridge_dir)
            if info is None:
                time.sleep(0.2)
        if info is None:
            return
        self.note("tmux target advertised")
        seen: set[str] = set()
        n = 0
        while not self._stop.is_set():
            n += 1
            proc = subprocess.run(
                [
                    "tmux",
                    "-S",
                    info["socket_path"],
                    "capture-pane",
                    "-p",
                    "-t",
                    info["tmux_target"],
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            pane = proc.stdout if proc.returncode == 0 else ""
            if pane:
                self.last_pane = pane
                (self.out_dir / f"{n:04d}_{time.monotonic() - self._t0:06.1f}.txt").write_text(
                    pane
                )
            if "prompt" not in seen and "❯" in pane:
                seen.add("prompt")
                self.note("hermes input prompt visible")
            if "marker" not in seen and self.marker in pane:
                seen.add("marker")
                self.note("marker text visible in pane")
            if "db" not in seen and self.state_db.exists():
                seen.add("db")
                self.note("state.db exists")
            rows = _state_db_rows(self.state_db, self.marker)
            if rows and f"rows{len(rows)}" not in seen:
                seen.add(f"rows{len(rows)}")
                self.note(f"state.db has {len(rows)} row(s) containing the marker")
            self._stop.wait(_PANE_POLL_S)


@pytest.fixture
def slow_boot_hermes_rig(
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str, Path]]:
    """A dedicated server + runner whose hermes-native TUI becomes ready late: the
    runner launches the slow-boot wrapper, and an isolated ``HOME`` carries a
    ``custom``-provider config at the mock LLM. Yields ``(base_url, runner_id, work)``."""
    if shutil.which("tmux") is None:
        pytest.skip("tmux is required for the hermes-native terminal rig")
    real_hermes = _resolve_runnable_hermes()
    if real_hermes is None:
        pytest.skip("a runnable `hermes` CLI is required for the hermes-native rig")

    work = tmp_path_factory.mktemp("hermes_slow_boot")
    home_dir = work / "home"
    wrapper_bin = work / "wrapper-bin"
    artifacts = work / "artifacts"
    for path in (home_dir / ".hermes", wrapper_bin, artifacts):
        path.mkdir(parents=True, exist_ok=True)
    (home_dir / ".hermes" / "config.yaml").write_text(
        "model:\n"
        "  provider: custom\n"
        f"  base_url: {mock_llm_server_url}/v1\n"
        "  api_key: mock-key\n"
        f"  default: {_HERMES_MOCK_MODEL}\n"
    )
    wrapper = _write_slow_boot_hermes_wrapper(wrapper_bin, real_hermes, _BOOT_DELAY_S)

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    binding_token = secrets.token_urlsafe(32)
    from omnigent.runner.identity import token_bound_runner_id

    runner_id = token_bound_runner_id(binding_token)
    env = os.environ.copy()
    for var in ("NO_PROXY", "no_proxy"):
        env[var] = ",".join(filter(None, [env.get(var, ""), "127.0.0.1,localhost"]))
    shared_env = {
        **env,
        "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        "HOME": str(home_dir),
        "OMNIGENT_LOG_LEVEL": "INFO",
        "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
        "OPENAI_API_KEY": "mock-key",
        "ANTHROPIC_API_KEY": "",
        "LLM_API_KEY": "",
        "OMNIGENT_WEB_UI_DIST": str(_REPO_ROOT / "omnigent" / "server" / "static" / "web-ui"),
    }
    server_env = {**shared_env, "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token}
    runner_env = {
        **shared_env,
        "OMNIGENT_RUNNER_ID": runner_id,
        "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
        "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
        "RUNNER_SERVER_URL": base_url,
        # The slow boot: the runner's hermes-native terminal launches the wrapper.
        "OMNIGENT_HERMES_PATH": str(wrapper),
    }
    agent_yaml = work / "hello_world.yaml"
    agent_yaml.write_text(
        "name: hello_world\nprompt: You are a friendly assistant.\n"
        "executor:\n  model: gpt-4o-mini\n  harness: openai-agents\n"
    )
    server_handle = (work / "server.log").open("w")
    runner_handle = (work / "runner.log").open("w")
    server_proc: subprocess.Popen[bytes] | None = None
    runner_proc: subprocess.Popen[bytes] | None = None
    try:
        server_proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "omnigent",
                "server",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--database-uri",
                f"sqlite:///{work / 'test.db'}",
                "--artifact-location",
                str(artifacts),
                "--agent",
                str(agent_yaml),
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
            with contextlib.suppress(httpx.HTTPError, ValueError):
                if _client.get(f"{base_url}/health", timeout=2).status_code == 200:
                    status = _client.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=2)
                    if status.status_code == 200 and status.json().get("online"):
                        online = True
                        break
            time.sleep(0.5)
        if not online:
            raise RuntimeError(
                f"hermes slow-boot rig did not come online within {_HEALTH_TIMEOUT_S:.0f}s.\n"
                f"Server log:\n{(work / 'server.log').read_text()[-3000:]}\n"
                f"Runner log:\n{(work / 'runner.log').read_text()[-3000:]}"
            )
        yield (base_url, runner_id, work)
    finally:
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
def test_hermes_native_first_message_survives_slow_boot(
    request: pytest.FixtureRequest,
    slow_boot_hermes_rig: tuple[str, str, Path],
    mock_llm_server_url: str,
) -> None:
    """The first message of a fresh hermes-native session is delivered exactly once:
    send it from the web composer while the Hermes TUI is still booting, then expect
    no "did not accept" failure, one user row in Hermes's store, and the echoed reply."""
    from tests.e2e_ui.conftest import set_fallback_mock_llm

    base_url, runner_id, work = slow_boot_hermes_rig
    evidence = Path(os.environ.get("HERMES_E2E_EVIDENCE_DIR") or (work / "evidence"))
    evidence.mkdir(parents=True, exist_ok=True)
    set_fallback_mock_llm(mock_llm_server_url, _HERMES_MOCK_MODEL, _ECHO_TOKEN)
    set_fallback_mock_llm(mock_llm_server_url, "default", _ECHO_TOKEN)
    set_fallback_mock_llm(mock_llm_server_url, "_policy_llm_", '{"action":"allow","reason":""}')

    marker = f"hermes-first-{uuid.uuid4().hex[:8]}"
    message = _first_message(marker)
    watch: _PaneWatch | None = None
    session_id: str | None = None
    try:
        # Browser setup happens before the TUI launch so the page is ready to send
        # the first message within seconds of the fresh session's bind.
        page: Page = request.getfixturevalue("page")
        page.goto(f"{base_url}/")
        expect(page.get_by_test_id("new-chat-landing-agent-select")).to_be_visible(timeout=60_000)

        # Fresh hermes-native session: binding launches the Hermes TUI in the
        # session terminal (the runner's auto-bootstrap path).
        session_id = _create_native_hermes_session(base_url, runner_id)
        launched_at = time.monotonic()
        watch = _PaneWatch(session_id, marker, evidence / "pane-dumps")
        watch.start()

        page.goto(f"{base_url}/c/{session_id}")
        composer = page.get_by_role("textbox", name="Message the agent")
        expect(composer).to_be_editable(timeout=60_000)
        _ensure_chat_view(page)
        composer.fill(message)
        send = page.get_by_role("button", name="Send", exact=True)
        expect(send).to_be_enabled(timeout=30_000)
        send.click()
        sent_offset = time.monotonic() - launched_at
        watch.note(f"first message sent from the composer {sent_offset:.1f}s after the TUI launch")
        _log.info(
            "first message sent %.1fs after the Hermes TUI launch; waiting for the outcome",
            sent_offset,
        )

        outcome = page.locator(_ERROR_PILL).or_(page.locator(_ASSISTANT))
        expect(outcome.first).to_be_visible(timeout=int(_TURN_OUTCOME_TIMEOUT_S * 1000))
        outcome_at = time.monotonic() - launched_at
        watch.note(f"user-visible outcome in the chat view at {outcome_at:.1f}s after launch")
        page.screenshot(path=str(evidence / "outcome-chat.png"))
        # A collapsed error pill shows a generic label; expand it so the exact
        # failure text is on screen (and in any recording) before asserting.
        if page.locator(_ERROR_PILL).count():
            with contextlib.suppress(Exception):
                page.locator(_ERROR_PILL).first.click()
                page.wait_for_timeout(2_500)
                page.screenshot(path=str(evidence / "outcome-chat-expanded.png"))
        errors, assistant_texts = _await_transcript_outcome(base_url, session_id)
        # Let a late duplicate submission (the retry) surface before counting.
        page.wait_for_timeout(5_000)
        expect(page.get_by_test_id("view-mode-toggle")).to_be_visible(timeout=30_000)
        _select_view_mode(page, "Terminal")
        expect(page.locator(_TERMINAL_VIEW).last).to_be_visible(timeout=30_000)
        page.wait_for_timeout(4_000)
        page.screenshot(path=str(evidence / "outcome-terminal.png"))
        _ensure_chat_view(page)
        page.wait_for_timeout(2_000)

        rows = _state_db_rows(watch.state_db, marker)
        user_rows = [r for r in rows if r[1] == "user"]
        pane = watch.last_pane
        pane_marker_hits = pane.count(marker)
        items = _transcript(base_url, session_id)
        user_items = [
            i for i in items if i.get("role") == "user" and marker in json.dumps(i.get("content"))
        ]
        summary = {
            "session_id": session_id,
            "boot_delay_s": _BOOT_DELAY_S,
            "sent_after_launch_s": round(sent_offset, 1),
            "outcome_after_launch_s": round(outcome_at, 1),
            "errors": errors,
            "assistant_texts": assistant_texts,
            "state_db_user_rows_with_marker": len(user_rows),
            "pane_marker_hits": pane_marker_hits,
            "transcript_user_items_with_marker": len(user_items),
            "events": watch.events,
        }
        (evidence / "summary.json").write_text(json.dumps(summary, indent=1))
        (evidence / "transcript.json").write_text(json.dumps(items, indent=1, default=str))
        (evidence / "final-pane.txt").write_text(pane)
        _log.info("outcome summary: %s", json.dumps(summary))

        dropped = [e for e in errors if _NOT_ACCEPTED in e]
        assert not dropped, (
            f"First message was dropped: the turn failed {outcome_at - sent_offset:.0f}s after "
            f"Send with {dropped[0]!r}; Hermes state.db user rows containing the marker: "
            f"{len(user_rows)}; marker occurrences in the Hermes pane: {pane_marker_hits}."
        )
        assert not errors, f"turn failed: {errors}"
        assert len(user_rows) == 1, (
            f"Hermes received the first message {len(user_rows)} times (state.db user rows "
            f"containing the marker); pane occurrences: {pane_marker_hits}."
        )
        assert any(_ECHO_TOKEN in t for t in assistant_texts), (
            f"No assistant reply echoed {_ECHO_TOKEN!r}; "
            f"assistant text: {assistant_texts or '<none>'}"
        )
    finally:
        if watch is not None:
            watch.stop()
            for _db in sorted({watch.state_db, *work.rglob("state.db")}):
                if not _db.exists():
                    continue
                with contextlib.suppress(Exception):
                    _con = sqlite3.connect(_db)
                    _con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    _con.close()
                with contextlib.suppress(Exception):
                    _rel = "-".join(_db.relative_to(work).parts)
                    shutil.copy2(_db, evidence / f"state--{_rel}")
        for path in work.rglob("*.log"):
            with contextlib.suppress(OSError):
                shutil.copy2(path, evidence / f"{path.parent.name}-{path.name}")
        if session_id is not None:
            with contextlib.suppress(httpx.HTTPError):
                _client.delete(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)

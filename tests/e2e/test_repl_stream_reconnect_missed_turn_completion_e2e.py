"""REPL must render a turn that completes while its SSE stream is reconnecting.

User journey: attach the terminal REPL to a live orchestrator session on a
remote server and submit a task that dispatches a sub-agent. When the worker
finishes, the parent is auto-woken for a final autonomous turn. While that
turn is in flight the ``/v1/sessions/{id}/stream`` transport is interrupted
(hosted ingresses cut long-lived streams periodically) and the turn completes
before the REPL's pump has resubscribed. Expected: the REPL still prints the
final message and the toolbar returns to ``state: sleeping``, matching the
server. Reported: nothing renders and the toolbar keeps
``streaming… NNNNs / state: running`` forever.

The interruption is injected with a transparent TCP proxy between the REPL
and the server that aborts the live stream connection and refuses new stream
subscriptions until the server reports the turn idle. Every other request
passes through untouched, so the server and runner stay healthy throughout.

The REPL runs in a private tmux pane and its screen is read with
``capture-pane``: prompt-toolkit repaints only changed cells, so the toolbar
state cannot be read reliably from the raw PTY byte stream.

Usage::

    python -m pytest tests/e2e/test_repl_stream_reconnect_missed_turn_completion_e2e.py -v
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.e2e.conftest import (
    configure_mock_llm,
    find_free_port,
    reset_mock_llm,
    set_fallback_mock_llm,
)
from tests.e2e.omnigent._pexpect_harness import (
    STATE_RUNNING,
    STATE_SLEEPING,
    ensure_repl_test_theme_env,
)
from tests.e2e.test_headless_lost_final_response import _build_orchestrator_bundle

pytestmark = [
    pytest.mark.min_server_version("0.3.0"),
    pytest.mark.timeout(300, method="signal"),
    pytest.mark.skipif(
        shutil.which("tmux") is None, reason="tmux is required to read the REPL screen"
    ),
]

_REPO_ROOT = Path(__file__).resolve().parents[2]

# capture-pane trims trailing blanks, so match the bare prompt glyph.
_PROMPT_READY = "❯"
_PROMPT = "Dispatch the worker for one job, then report its result."
_WORKER_REPLY = "WORKER_DONE"
_RECONNECT_LOG_LINES = (
    "SSE transport interrupted, reconnecting",
    "SSE stream error, reconnecting",
)
_STREAM_REQUEST_RE = re.compile(rb"GET /v1/sessions/[^ ?]+/stream[ ?]")
# Well past the pump's 5 s maximum reconnect backoff plus send()'s 1 s poll.
_OBSERVATION_WINDOW_S = 20.0


class _StreamCutProxy:
    """Transparent TCP proxy that can black out the session SSE stream.

    Forwards all bytes between clients and the upstream Omnigent server.
    :meth:`cut_streams` aborts every connection currently carrying a
    ``GET /v1/sessions/{id}/stream`` response and, until :meth:`restore`,
    aborts any new connection that sends such a request — the same
    transport interruption a hosted ingress produces when it caps a
    long-lived stream. All other requests pass through untouched.
    """

    def __init__(self, upstream_host: str, upstream_port: int, listen_port: int) -> None:
        self.upstream_host = upstream_host
        self.upstream_port = upstream_port
        self.listen_port = listen_port
        self.stream_aborts = 0
        self._blackout = False
        self._lock = threading.Lock()
        self._stream_conns: set[tuple[asyncio.StreamWriter, asyncio.StreamWriter]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.listen_port}"

    def cut_streams(self) -> None:
        """Abort live stream connections and refuse new ones until :meth:`restore`."""
        with self._lock:
            self._blackout = True
        loop = self._loop
        assert loop is not None, "proxy not started"
        asyncio.run_coroutine_threadsafe(self._abort_stream_conns(), loop).result(timeout=10)

    def restore(self) -> None:
        with self._lock:
            self._blackout = False

    def _in_blackout(self) -> bool:
        with self._lock:
            return self._blackout

    async def _abort_stream_conns(self) -> None:
        for cwriter, uwriter in list(self._stream_conns):
            self.stream_aborts += 1
            cwriter.transport.abort()
            uwriter.transport.abort()
        self._stream_conns.clear()

    async def _handle(
        self,
        creader: asyncio.StreamReader,
        cwriter: asyncio.StreamWriter,
    ) -> None:
        try:
            ureader, uwriter = await asyncio.open_connection(
                self.upstream_host, self.upstream_port
            )
        except OSError:
            cwriter.close()
            return

        pair = (cwriter, uwriter)
        killed = asyncio.Event()

        async def pump_c2s() -> None:
            # Rolling tail so a request line split across reads still matches.
            scan = b""
            try:
                while True:
                    data = await creader.read(65536)
                    if not data:
                        break
                    scan = (scan + data)[-16384:]
                    if _STREAM_REQUEST_RE.search(scan):
                        if self._in_blackout():
                            self.stream_aborts += 1
                            killed.set()
                            cwriter.transport.abort()
                            uwriter.transport.abort()
                            return
                        self._stream_conns.add(pair)
                    uwriter.write(data)
                    await uwriter.drain()
            except (ConnectionError, OSError):
                pass
            finally:
                if not killed.is_set():
                    with contextlib.suppress(Exception):
                        uwriter.write_eof()

        async def pump_s2c() -> None:
            try:
                while True:
                    data = await ureader.read(65536)
                    if not data:
                        break
                    cwriter.write(data)
                    await cwriter.drain()
            except (ConnectionError, OSError):
                pass
            finally:
                if not killed.is_set():
                    with contextlib.suppress(Exception):
                        cwriter.close()

        await asyncio.gather(pump_c2s(), pump_s2c(), return_exceptions=True)
        self._stream_conns.discard(pair)

    async def _serve(self) -> None:
        server = await asyncio.start_server(self._handle, "127.0.0.1", self.listen_port)
        self._ready.set()
        async with server:
            await server.serve_forever()

    def start(self) -> None:
        def _run() -> None:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            with contextlib.suppress(asyncio.CancelledError):
                self._loop.run_until_complete(self._serve())

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=10):
            raise RuntimeError("stream-cut proxy did not start listening in time")

    def stop(self) -> None:
        loop = self._loop
        if loop is not None:
            for task in asyncio.all_tasks(loop):
                loop.call_soon_threadsafe(task.cancel)
        if self._thread is not None:
            self._thread.join(timeout=10)


class _TmuxPane:
    """A 160x40 bash pane on a private tmux server; the pane is the screen under test."""

    def __init__(self, env: dict[str, str], cwd: Path) -> None:
        self.socket = f"omnigent-repl-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.target = "repl"
        self._env = env
        self._cwd = cwd

    def _tmux(self, *args: str) -> str:
        proc = subprocess.run(
            ["tmux", "-L", self.socket, *args],
            env=self._env,
            cwd=self._cwd,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return proc.stdout

    def start(self) -> None:
        # The pane inherits the environment of the client that starts the server.
        self._tmux(
            "-f",
            "/dev/null",
            "new-session",
            "-d",
            "-s",
            self.target,
            "-x",
            "160",
            "-y",
            "40",
            "bash",
            "--norc",
            "--noprofile",
        )
        self._tmux("set-option", "-g", "status", "off")
        self._tmux("set-option", "-g", "default-terminal", "screen-256color")
        self._tmux("set-option", "-g", "history-limit", "5000")

    def send_keys(self, text: str, *, enter: bool = True, char_delay: float = 0.0) -> None:
        if char_delay:
            for ch in text:
                self._tmux("send-keys", "-t", self.target, "-l", ch)
                time.sleep(char_delay)
        else:
            self._tmux("send-keys", "-t", self.target, "-l", text)
        if enter:
            self._tmux("send-keys", "-t", self.target, "Enter")

    def screen(self, *, ansi: bool = False) -> str:
        args = ["capture-pane", "-p", "-t", self.target]
        if ansi:
            args.insert(1, "-e")
        return self._tmux(*args)

    def transcript(self) -> str:
        return self._tmux("capture-pane", "-p", "-S", "-", "-t", self.target)

    def wait_for(self, pattern: str, *, timeout: float) -> str:
        deadline = time.monotonic() + timeout
        seen: list[str] = []
        while time.monotonic() < deadline:
            text = self.screen()
            if re.search(pattern, text):
                return text
            if not seen or seen[-1] != text:
                seen = [*seen[-2:], text]
            time.sleep(0.3)
        frames = "\n=====\n".join(seen)
        raise AssertionError(
            f"pane never showed {pattern!r} within {timeout}s; last distinct screens:\n{frames}"
        )

    def kill(self) -> None:
        subprocess.run(["tmux", "-L", self.socket, "kill-server"], check=False, timeout=15)


def _create_orchestrator_session(
    client: httpx.Client,
    *,
    runner_id: str,
    mock_llm_server_url: str,
    uid: str,
) -> tuple[str, str]:
    """Upload a parent+worker bundle whose final wake-up turn is gated.

    The parent dispatches the worker with ``sys_session_send``, ends its
    first turn with no text, and is auto-woken once the worker finishes.
    That final autonomous turn is held at the mock gate (``block``) so the
    test can interrupt the stream while it is in flight.

    :returns: ``(session_id, final_summary_marker)``.
    """
    marker = f"FINAL_SUMMARY_{uid}"
    parent_model = f"mock-reconnect-parent-{uid}"
    child_model = f"mock-reconnect-child-{uid}"

    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_dispatch_worker",
                        "name": "sys_session_send",
                        "arguments": json.dumps(
                            {"agent": "worker", "title": "job", "args": "Acknowledge and finish."}
                        ),
                    }
                ]
            },
            {"text": ""},
            {"text": marker, "block": True},
        ],
        key=parent_model,
    )
    set_fallback_mock_llm(mock_llm_server_url, parent_model, marker)
    configure_mock_llm(mock_llm_server_url, [{"text": _WORKER_REPLY}], key=child_model)
    set_fallback_mock_llm(mock_llm_server_url, child_model, _WORKER_REPLY)

    bundle = _build_orchestrator_bundle(
        name=f"gap-{uid}",
        parent_model=parent_model,
        child_model=child_model,
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
    )
    resp = client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
    )
    resp.raise_for_status()
    session_id = str(resp.json()["session_id"])
    client.patch(f"/v1/sessions/{session_id}", json={"runner_id": runner_id}).raise_for_status()
    return session_id, marker


def _repl_env(tmp_home: Path) -> dict[str, str]:
    """Env for the ``omnigent attach`` REPL (pure client) and its tmux pane."""
    # The checkout under test must precede any ambient PYTHONPATH, or a
    # sibling checkout shadows the console script's ``omnigent`` import.
    source_paths = [
        str(_REPO_ROOT),
        str(_REPO_ROOT / "sdks" / "python-client"),
        str(_REPO_ROOT / "sdks" / "ui"),
    ]
    existing_pp = os.environ.get("PYTHONPATH", "")
    merged_pp = os.pathsep.join([*source_paths, existing_pp] if existing_pp else source_paths)
    config_home = tmp_home / ".omnigent"
    config_home.mkdir(parents=True, exist_ok=True)
    (config_home / "config.yaml").write_text(
        "auto_open_conversation: false\ntui:\n  theme: dark\n"
    )
    venv_bin = Path(sys.executable).parent
    assert (venv_bin / "omnigent").exists(), f"omnigent console script missing from {venv_bin}"
    env = os.environ.copy()
    env.update(
        {
            "PATH": os.pathsep.join([str(venv_bin), env.get("PATH", "")]),
            "HOME": str(tmp_home),
            "OMNIGENT_CONFIG_HOME": str(config_home),
            # Keeps the REPL's always-on CLI diagnostics log under tmp_home.
            "OMNIGENT_DATA_DIR": str(config_home),
            "OMNIGENT_SKIP_ONBOARD": "1",
            "OMNIGENT_NO_UPDATE_CHECK": "1",
            "PYTHONPATH": merged_pp,
            "TERM": "xterm-256color",
            "PS1": "$ ",
        }
    )
    # prompt-toolkit draws the bottom toolbar only after a CPR round-trip,
    # which tmux answers; the NO_CPR override would hide the toolbar.
    env.pop("PROMPT_TOOLKIT_NO_CPR", None)
    # Strip ambient credential/harness markers so the REPL is a pure client.
    for k in list(env):
        if k.startswith("DATABRICKS_"):
            env.pop(k, None)
    for k in ("ANTHROPIC_API_KEY", "CLAUDE_CODE", "CLAUDECODE", "CODEX", "TMUX"):
        env.pop(k, None)
    return ensure_repl_test_theme_env(env)


def _attach_command(session_id: str, server_url: str) -> str:
    # The console script (not ``python -m omnigent.cli``) runs ``main()``,
    # which writes the always-on CLI diagnostics log the report quotes.
    return f"omnigent attach {session_id} --server {server_url}"


def _session_snapshot(client: httpx.Client, session_id: str) -> dict[str, Any]:
    resp = client.get(f"/v1/sessions/{session_id}")
    resp.raise_for_status()
    return resp.json()


def _wait_for_gated_wake_turn(
    mock_llm_server_url: str,
    client: httpx.Client,
    session_id: str,
    *,
    timeout: float,
) -> None:
    """Block until the parent's final autonomous turn is held at the mock gate."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pending = httpx.get(f"{mock_llm_server_url}/gate/pending", timeout=5.0).json()["pending"]
        if pending:
            return
        snap = _session_snapshot(client, session_id)
        assert snap.get("status") != "failed", (
            f"orchestrator failed before its wake-up turn: {snap}"
        )
        time.sleep(0.2)
    pytest.fail(f"parent wake-up turn never reached the mock gate within {timeout}s")


def _wait_for_idle_with_text(
    client: httpx.Client, session_id: str, marker: str, *, timeout: float
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    snap: dict[str, Any] = {}
    while time.monotonic() < deadline:
        snap = _session_snapshot(client, session_id)
        if snap.get("status") == "idle" and marker in json.dumps(snap.get("items", [])):
            return snap
        time.sleep(0.2)
    pytest.fail(f"session never became idle with {marker!r} persisted; last snapshot={snap}")


def _observe_pane(
    pane: _TmuxPane, seconds: float, done: Callable[[str, str], bool]
) -> tuple[str, str]:
    """Poll the pane for up to *seconds*; returns the final ``(screen, transcript)``."""
    deadline = time.monotonic() + seconds
    while True:
        screen, transcript = pane.screen(), pane.transcript()
        if done(screen, transcript) or time.monotonic() >= deadline:
            return screen, transcript
        time.sleep(0.5)


def _reconnect_logged(*roots: Path) -> bool:
    for root in roots:
        candidates = [root] if root.is_file() else list(root.rglob("*.log"))
        for path in candidates:
            with contextlib.suppress(OSError):
                text = path.read_text(errors="replace")
                if any(line in text for line in _RECONNECT_LOG_LINES):
                    return True
    return False


def test_repl_renders_turn_that_completes_during_stream_reconnect(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str | None,
    tmp_path: Path,
) -> None:
    """A turn finishing inside a stream reconnect gap must still reach the REPL.

    Journey: attach the REPL → submit the task → the worker runs and the parent
    is woken for its final turn (held at the mock gate, toolbar ``state:
    running``) → the SSE stream is cut → the turn completes while the pump is
    still reconnecting → the stream is restored. Expected: the REPL prints the
    final summary and the toolbar returns to ``state: sleeping``.
    """
    assert mock_llm_server_url is not None, "reproduction requires the mock LLM server"

    uid = uuid.uuid4().hex[:6]
    session_id, marker = _create_orchestrator_session(
        http_client,
        runner_id=live_runner_id,
        mock_llm_server_url=mock_llm_server_url,
        uid=uid,
    )
    print(f"orchestrator session {session_id} (final summary marker {marker})")
    upstream = httpx.URL(str(http_client.base_url))
    proxy = _StreamCutProxy(upstream.host, upstream.port, find_free_port())
    proxy.start()
    tmp_home = tmp_path / "home"
    pane = _TmuxPane(_repl_env(tmp_home), _REPO_ROOT)
    try:
        pane.start()
        pane.send_keys(_attach_command(session_id, proxy.url))
        pane.wait_for(_PROMPT_READY, timeout=90)
        pane.send_keys(_PROMPT)
        _wait_for_gated_wake_turn(mock_llm_server_url, http_client, session_id, timeout=120)
        pane.wait_for(STATE_RUNNING, timeout=15)

        proxy.cut_streams()
        # Let the pump hit the abort and start its reconnect backoff.
        time.sleep(1.5)
        released = httpx.post(f"{mock_llm_server_url}/gate/release", timeout=5.0).json()
        assert released["released"], "the gated wake-up turn was not pending at release time"
        _wait_for_idle_with_text(http_client, session_id, marker, timeout=60)
        proxy.restore()

        screen, transcript = _observe_pane(
            pane,
            _OBSERVATION_WINDOW_S,
            lambda scr, log: marker in log and re.search(STATE_SLEEPING, scr) is not None,
        )

        assert proxy.stream_aborts >= 1, (
            f"fault was not injected (stream_aborts={proxy.stream_aborts}); the REPL's "
            "stream subscription never crossed the proxy — test harness issue."
        )
        assert _reconnect_logged(tmp_home), (
            "REPL never logged the SSE reconnect after the stream cut; the fault did not "
            f"reach the pump. Logs under {tmp_home}"
        )
        assert marker in transcript, (
            f"REPL never rendered the parent's final message {marker!r} within "
            f"{_OBSERVATION_WINDOW_S:.0f}s of the server reporting the session idle "
            f"(session {session_id}). Screen:\n{screen}"
        )
        assert re.search(STATE_SLEEPING, screen), (
            "REPL toolbar never returned to 'state: sleeping' after the turn completed during "
            f"the stream reconnect (session {session_id}). Screen:\n{screen}"
        )
    finally:
        pane.kill()
        proxy.stop()

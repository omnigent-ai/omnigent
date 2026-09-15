"""
REPL elicitation-resolve transport-fault e2e test.

User journey: attach the terminal REPL to a live session, send a message,
answer the approval prompt with ``y`` — while a transient network fault
interrupts the verdict POST (``POST /v1/sessions/{id}/elicitations/{eid}/
resolve``). The REPL must survive the blip (retry or degrade gracefully):
the turn should still complete and the TUI must NOT crash into
prompt_toolkit's ``Unhandled exception in event loop`` /
``Press ENTER to continue...`` pause.

The fault is injected with a transparent TCP proxy between the REPL and
the Omnigent server that aborts exactly ONE connection: the first one
carrying an elicitation ``/resolve`` request, before any response bytes
are sent. Every other request (including a retry of the same resolve)
passes through untouched — the server and runner stay healthy the whole
time, exactly like the reported transient interruption.

Usage::

    python -m pytest tests/e2e/test_repl_elicitation_resolve_transport_fault.py -v --timeout=300
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from tests.e2e.conftest import configure_mock_llm, find_free_port, reset_mock_llm

pexpect = pytest.importorskip("pexpect")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ASK_DEMO_YAML = _REPO_ROOT / "tests" / "resources" / "agents" / "ask-demo" / "ask-demo.yaml"
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

_REPLY_MARKER = "ELICIT_RETRY_OK_MARKER"
_CRASH_MARKERS = (
    "Unhandled exception in event loop",
    "Press ENTER to continue",
)


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences before substring search."""
    return _ANSI_RE.sub("", text)


class _FaultProxy:
    """Transparent TCP proxy that aborts ONE elicitation-resolve request.

    Forwards all bytes between clients and the upstream Omnigent server.
    The first client connection whose bytes carry an elicitation
    ``/resolve`` HTTP request is aborted (RST, no response) — a transient
    transport interruption exactly at the verdict POST. Everything else,
    including a retry of the same resolve, passes through untouched.
    """

    def __init__(self, upstream_host: str, upstream_port: int, listen_port: int) -> None:
        self.upstream_host = upstream_host
        self.upstream_port = upstream_port
        self.listen_port = listen_port
        self.kills = 0
        self._armed = True
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    def _should_kill(self, buf: bytes) -> bool:
        if b"/resolve" not in buf or b"/elicitations/" not in buf:
            return False
        with self._lock:
            if not self._armed:
                return False
            self._armed = False
            self.kills += 1
            return True

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

        killed = asyncio.Event()

        async def pump_c2s() -> None:
            # Keep a rolling tail so a request line split across reads
            # still matches; kill on the FIRST resolve request only.
            scan = b""
            try:
                while True:
                    data = await creader.read(65536)
                    if not data:
                        break
                    scan = (scan + data)[-16384:]
                    if self._should_kill(scan):
                        killed.set()
                        cwriter.transport.abort()
                        uwriter.transport.abort()
                        return
                    uwriter.write(data)
                    await uwriter.drain()
            except (ConnectionError, OSError):
                pass
            finally:
                if not killed.is_set():
                    with contextlib.suppress(OSError):
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
                    with contextlib.suppress(OSError):
                        cwriter.close()

        await asyncio.gather(pump_c2s(), pump_s2c(), return_exceptions=True)

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
            raise RuntimeError("fault proxy did not start listening in time")

    def stop(self) -> None:
        loop = self._loop
        if loop is not None:
            for task in asyncio.all_tasks(loop):
                loop.call_soon_threadsafe(task.cancel)
        # Join the proxy thread so no background pump outlives the test.
        if self._thread is not None:
            self._thread.join(timeout=10)


class _ProxiedSession:
    """A live gated session reachable through the fault proxy."""

    def __init__(self, proxy: _FaultProxy, proxy_url: str, session_id: str) -> None:
        self.proxy = proxy
        self.proxy_url = proxy_url
        self.session_id = session_id


@pytest.fixture
def proxied_gated_session(
    mock_llm_server_url: str,
    tmp_path: Path,
) -> Iterator[_ProxiedSession]:
    """Boot server + runner with the always-ask agent, create a session,
    and put a one-shot fault proxy in front of the server.
    """
    from omnigent.chat import _start_local_server, _stop_local_server, _wait_for_server

    reset_mock_llm(mock_llm_server_url)
    # The always-ask policy gates the turn BEFORE the LLM; once the
    # verdict lands the turn proceeds and draws this scripted reply.
    configure_mock_llm(mock_llm_server_url, [{"text": _REPLY_MARKER}] * 3, key="default")

    saved_env = {
        k: os.environ.get(k)
        for k in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "PYTHONPATH", "NO_PROXY", "no_proxy")
    }
    os.environ["OPENAI_API_KEY"] = "mock-key"
    os.environ["OPENAI_BASE_URL"] = f"{mock_llm_server_url}/v1"
    # The spawned server + runner import omnigent from the worktree, and
    # loopback traffic must bypass any ambient corporate proxy.
    existing_pp = os.environ.get("PYTHONPATH", "")
    os.environ["PYTHONPATH"] = (
        os.pathsep.join([str(_REPO_ROOT), existing_pp]) if existing_pp else str(_REPO_ROOT)
    )
    for proxy_key in ("NO_PROXY", "no_proxy"):
        parts = [p for p in os.environ.get(proxy_key, "").split(",") if p]
        for host in ("127.0.0.1", "localhost"):
            if host not in parts:
                parts.append(host)
        os.environ[proxy_key] = ",".join(parts)

    server_port = find_free_port()
    server = _start_local_server(_ASK_DEMO_YAML, server_port, ephemeral=True)
    proxy: _FaultProxy | None = None
    try:
        _wait_for_server(server_port, server)
        base_url = f"http://127.0.0.1:{server_port}"

        with httpx.Client(base_url=base_url, timeout=30.0) as client:
            resp = client.get("/v1/agents", params={"limit": 100})
            resp.raise_for_status()
            agents = resp.json().get("data", [])
            agent_id = next((str(a["id"]) for a in agents if a.get("name") == "ask-demo"), None)
            assert agent_id, f"ask-demo not registered: {[a.get('name') for a in agents]}"

            resp = client.post(
                "/v1/sessions",
                json={"agent_id": agent_id},
                headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
            )
            resp.raise_for_status()
            session_id = str(resp.json()["id"])

            resp = client.patch(f"/v1/sessions/{session_id}", json={"runner_id": server.runner_id})
            resp.raise_for_status()

            # Attach fails loud when the session's runner is not online yet;
            # wait for the runner's tunnel to come up.
            deadline = time.monotonic() + 60.0
            while time.monotonic() < deadline:
                snap = client.get(f"/v1/sessions/{session_id}").json()
                if snap.get("runner_online") in (True, None):
                    break
                time.sleep(0.5)
            else:
                pytest.fail("runner never came online for the gated session")

        proxy_port = find_free_port()
        proxy = _FaultProxy("127.0.0.1", server_port, proxy_port)
        proxy.start()

        yield _ProxiedSession(
            proxy=proxy,
            proxy_url=f"http://127.0.0.1:{proxy_port}",
            session_id=session_id,
        )
    finally:
        if proxy is not None:
            proxy.stop()
        _stop_local_server(server)
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _build_repl_env(tmp_home: Path) -> dict[str, str]:
    """Env for the spawned ``omnigent attach`` REPL (pure client)."""
    from tests.e2e.omnigent._pexpect_harness import ensure_repl_test_theme_env

    sdk_paths = [
        str(_REPO_ROOT / "sdks" / "python-client"),
        str(_REPO_ROOT / "sdks" / "ui"),
    ]
    existing_pp = os.environ.get("PYTHONPATH", "")
    merged_pp = (
        os.pathsep.join([*sdk_paths, existing_pp]) if existing_pp else os.pathsep.join(sdk_paths)
    )
    config_home = tmp_home / ".omnigent"
    config_home.mkdir(parents=True, exist_ok=True)
    (config_home / "config.yaml").write_text(
        "auto_open_conversation: false\ntui:\n  theme: dark\n",
    )
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(tmp_home),
            "OMNIGENT_CONFIG_HOME": str(config_home),
            "OMNIGENT_SKIP_ONBOARD": "1",
            "OMNIGENT_NO_UPDATE_CHECK": "1",
            "PYTHONPATH": merged_pp,
            "TERM": "xterm-256color",
            "LINES": "40",
            "COLUMNS": "120",
            "PROMPT_TOOLKIT_NO_CPR": "1",
        }
    )
    # Strip every ambient credential/harness marker so the spawned REPL is a
    # pure client — a leaked provider var reroutes its credential resolution.
    for k in list(env):
        if k.startswith("DATABRICKS_"):
            env.pop(k, None)
    for k in ("ANTHROPIC_API_KEY", "CLAUDE_CODE", "CLAUDECODE", "CODEX"):
        env.pop(k, None)
    return ensure_repl_test_theme_env(env)


def _read_pending(child: Any, seconds: float) -> str:
    """Non-blocking read of buffered PTY output, ANSI-stripped."""
    collected = ""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        with contextlib.suppress(pexpect.EOF):
            child.expect(pexpect.TIMEOUT, timeout=min(0.5, max(0.05, deadline - time.monotonic())))
        chunk = child.before or ""
        if isinstance(chunk, bytes):
            chunk = chunk.decode("utf-8", errors="replace")
        collected += chunk
        # ``before`` accumulates on TIMEOUT; reset the internal buffer so
        # each iteration only appends fresh bytes.
        child.buffer = child.string_type()
        child.before = child.string_type()
    return _strip_ansi(collected)


def _clean_exit(child: Any) -> None:
    """Best-effort clean exit of the REPL."""
    try:
        child.sendcontrol("d")
        child.expect(pexpect.EOF, timeout=10)
    except pexpect.ExceptionPexpect:
        pass
    if child.isalive():
        child.terminate(force=True)


def test_repl_survives_transient_transport_error_on_elicitation_resolve(
    proxied_gated_session: _ProxiedSession,
    tmp_path: Path,
) -> None:
    """A transient connection drop on the verdict POST must not crash the REPL.

    Journey: attach the REPL to a live gated session → send a message →
    ``approval required`` surfaces → answer ``y`` → the verdict POST hits a
    one-shot transport fault. Expected: the REPL retries (or degrades
    gracefully), the turn completes with the agent's reply, and the TUI
    never pauses at ``Unhandled exception in event loop`` /
    ``Press ENTER to continue...``.
    """
    sess = proxied_gated_session
    env = _build_repl_env(tmp_path / "home")

    child = pexpect.spawn(
        sys.executable,
        [
            "-m",
            "omnigent.cli",
            "attach",
            sess.session_id,
            "--server",
            sess.proxy_url,
        ],
        env=env,
        cwd=str(_REPO_ROOT),
        encoding="utf-8",
        codec_errors="replace",
        timeout=120,
        dimensions=(40, 120),
    )
    try:
        child.expect("❯", timeout=90)
        child.send("Hello there\r")
        child.expect("approval required", timeout=60)
        child.send("y\r")
        child.expect("approved", timeout=15)

        # The verdict POST fires right after the ``approved`` echo; the
        # proxy aborts that one request. Give the REPL time to retry,
        # complete the turn, and render the reply.
        buffered = _read_pending(child, seconds=20.0)

        assert sess.proxy.kills == 1, (
            f"fault was not injected (kills={sess.proxy.kills}); "
            "the resolve POST never crossed the proxy — test harness issue."
        )
        for marker in _CRASH_MARKERS:
            assert marker not in buffered, (
                f"REPL crashed after the transient transport fault: found "
                f"{marker!r}.\nBuffer:\n{buffered[-3000:]}"
            )
        assert _REPLY_MARKER in buffered, (
            "Turn never completed after the transient fault — the verdict "
            "was not retried/delivered (elicitation left unresolved).\n"
            f"Buffer:\n{buffered[-3000:]}"
        )
    except pexpect.EOF:
        buf = _strip_ansi(child.before or "")
        pytest.fail(f"REPL exited early. Full buffer:\n{buf[-3000:]}")
    finally:
        _clean_exit(child)

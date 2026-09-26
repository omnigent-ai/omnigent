"""E2E: a transient 5xx on the agent-bundle fetch must not brick turn setup.

When a user sends a message, the runner resolves the session's agent spec in
the background by fetching ``GET /v1/sessions/{id}/agent/contents`` from the
server (``_resolve_agent_spec_from_server`` in ``omnigent/runner/_entry.py``).
A server 5xx there is usually a restarting backend or a proxy blip. If the
resolver gives up on the first one, turn setup aborts before a harness is
selected, the turn surfaces as ``session.status: failed`` with
``code: runner_error``, and the user sees the SPA error pill ("Something went
wrong setting up the turn on the host.") instead of a reply.

The journey is the reported one: create a ``hello_world`` session bound to a
runner, open it in the web app, and send a message while the runner -> server
path returns a transient 5xx on the agent-bundle fetch. A loopback TCP proxy
between the runner and the server answers a leading burst of agent-bundle GETs
with a synthetic 503 and forwards everything else untouched (including the
WebSocket tunnel that keeps the runner online). It is armed before the session
is bound, so the session-init resolve, the SPA's page-load reads, and the
turn-setup resolves all land inside the fault window; the window is sized so a
retrying resolver exhausts it and a later attempt reaches a real 200.

The rig mirrors ``test_cursor_native_launch_config_timeout.py`` (dedicated
server + runner with an isolated ``HOME`` / ``OMNIGENT_CONFIG_HOME``), plus the
interposed proxy. A recovered turn needs a working model, so the openai-agents
harness is pointed at the shared mock LLM server.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _build_hello_world_bundle,
    configure_mock_llm,
    set_fallback_mock_llm,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]

# Boot budget for the spawned server + proxy + runner trio.
_HEALTH_TIMEOUT_S = 120.0
# Turn-outcome budget (ms): time for the failed status (bug) or the mock reply
# (fixed) to reach the SPA after the message is sent.
_OUTCOME_TIMEOUT_MS = 60_000

# Answer this many leading agent-bundle GETs with a synthetic 503, then forward.
# A no-retry resolver 5xx's on every resolve inside this window and bricks the
# turn; a retrying resolver exhausts the window and recovers on a later attempt.
_FAIL_FIRST_N = int(os.environ.get("SPEC_5XX_E2E_FAIL_FIRST_N", "8"))

_ERROR_PILL = '[data-testid="error-pill"]'
_ERROR_HEADLINE = '[data-testid="error-headline"]'
_COMPOSER = "Send a message…"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'

# Proxy-blind client: CI forces an egress proxy via HTTP(S)_PROXY env vars that
# must not intercept loopback requests to the spawned server.
_client = httpx.Client(trust_env=False)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


#: Ambient variables that must not leak into the rig: provider credentials and
#: config (the rig uses its isolated HOME / OMNIGENT_CONFIG_HOME and the mock
#: LLM), and runner/host identity from any outer Omnigent runner running this
#: test (a leaked OMNIGENT_RUNNER_* makes the spawned runner take the
#: zygote-fork path and hang before coming online).
_AMBIENT_STRIP_PREFIXES = (
    "OPENAI_",
    "ANTHROPIC_",
    "CLAUDE_CODE_",
    "DATABRICKS_",
    "CODEX_",
    "OMNIGENT_RUNNER_",
    "OMNIGENT_HOST_",
)
_AMBIENT_STRIP_EXACT = (
    "RUNNER_SERVER_URL",
    "OMNIGENT_REMOTE_AUTH_TOKEN",
    "OMNIGENT_CONFIG_HOME",
    "OMNIGENT_PROCESS_LOG_FILE",
    "OMNIGENT_DATA_DIR",
    "OMNIGENT_USER_ID",
)


def _no_proxy_env() -> dict[str, str]:
    """Ambient env with loopback proxy-exempt and rig-hostile vars stripped."""
    env = os.environ.copy()
    for var in list(env):
        if var.startswith(_AMBIENT_STRIP_PREFIXES) or var in _AMBIENT_STRIP_EXACT:
            env.pop(var)
    for var in ("NO_PROXY", "no_proxy"):
        existing = env.get(var, "")
        env[var] = ",".join(filter(None, [existing, "127.0.0.1,localhost"]))
    return env


def _suffix_prefix_len(buf: bytes, pat: bytes) -> int:
    """Largest ``k > 0`` such that *buf* ends with ``pat[:k]`` (``k < len(pat)``).

    Holds back a request line split across socket reads so it is never
    partially forwarded before it can be recognized. Returns 0 (forward
    everything) when no suffix of *buf* is a prefix of *pat* -- the common case,
    so ordinary traffic (including the WebSocket tunnel) relays with no latency.
    """
    m = min(len(buf), len(pat) - 1)
    for k in range(m, 0, -1):
        if buf[-k:] == pat[:k]:
            return k
    return 0


_SYNTHETIC_503_BODY = (
    b'{"error":"spec_resolver_failed",'
    b'"detail":"injected transient 5xx on agent-bundle fetch (e2e)"}'
)
_SYNTHETIC_503 = (
    b"HTTP/1.1 503 Service Unavailable\r\n"
    b"Content-Type: application/json\r\n"
    b"Content-Length: " + str(len(_SYNTHETIC_503_BODY)).encode() + b"\r\n"
    b"Connection: close\r\n"
    b"\r\n" + _SYNTHETIC_503_BODY
)


class _Spec503Proxy:
    """Loopback TCP proxy (runner -> server) injecting a transient 5xx.

    While armed, the first :data:`_FAIL_FIRST_N` requests whose line contains
    ``GET /v1/sessions/<sid>/agent/contents`` are answered with a synthetic
    HTTP 503 (``Connection: close``) instead of being forwarded. Every other
    request -- other routes, later agent-bundle GETs, and the long-lived
    WebSocket tunnel -- relays untouched. After the budget is spent the proxy
    disarms, so a later fetch reaches the backend and gets a real response.
    """

    def __init__(self, backend_port: int) -> None:
        self._backend_port = backend_port
        self.port = _free_port()
        self._pattern: bytes | None = None
        self._fail_first_n = _FAIL_FIRST_N
        self.match_count = 0
        self.injected_count = 0
        self._started = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread = threading.Thread(target=self._run, name="spec-503-proxy", daemon=True)
        self._thread.start()
        if not self._started.wait(timeout=10.0):
            raise RuntimeError("spec-503 proxy failed to start")

    def arm(self, session_id: str, *, fail_first_n: int = _FAIL_FIRST_N) -> None:
        """Answer the first *fail_first_n* agent-bundle GETs for *session_id* with 503."""
        self._fail_first_n = fail_first_n
        self._pattern = f"GET /v1/sessions/{session_id}/agent/contents".encode()

    def stop(self) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=10.0)

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        server = self._loop.run_until_complete(
            asyncio.start_server(self._handle, "127.0.0.1", self.port)
        )
        self._started.set()
        try:
            self._loop.run_forever()
        finally:
            server.close()
            with contextlib.suppress(Exception):
                self._loop.run_until_complete(server.wait_closed())
            self._loop.close()

    async def _handle(self, creader: asyncio.StreamReader, cwriter: asyncio.StreamWriter) -> None:
        try:
            breader, bwriter = await asyncio.open_connection("127.0.0.1", self._backend_port)
        except OSError:
            with contextlib.suppress(Exception):
                cwriter.close()
            return

        async def client_to_backend() -> None:
            buf = b""
            transparent = False
            while True:
                chunk = await creader.read(65536)
                if not chunk:
                    if buf:
                        bwriter.write(buf)
                        await bwriter.drain()
                    return
                buf += chunk
                pattern = self._pattern
                if transparent or pattern is None:
                    bwriter.write(buf)
                    await bwriter.drain()
                    buf = b""
                    continue
                # Once identified as the WebSocket tunnel, stop scanning: it is
                # long-lived binary frames, never the target request line.
                if b"upgrade: websocket" in buf.lower():
                    transparent = True
                    bwriter.write(buf)
                    await bwriter.drain()
                    buf = b""
                    continue
                if pattern in buf:
                    self.match_count += 1
                    idx = self.match_count
                    sys.stderr.write(
                        f"[spec-503-proxy] agent/contents match #{idx} "
                        f"(fail_first_n={self._fail_first_n}) at t={time.monotonic():.1f}\n"
                    )
                    sys.stderr.flush()
                    if idx <= self._fail_first_n:
                        # Transient blip: answer 503 and drop the connection
                        # (Connection: close) instead of forwarding.
                        self.injected_count += 1
                        cwriter.write(_SYNTHETIC_503)
                        await cwriter.drain()
                        return
                    # Past the fault budget: forward untouched so a retrying
                    # resolver's later attempt reaches the backend and recovers.
                    bwriter.write(buf)
                    await bwriter.drain()
                    buf = b""
                    continue
                keep = _suffix_prefix_len(buf, pattern)
                if len(buf) > keep:
                    bwriter.write(buf[: len(buf) - keep])
                    await bwriter.drain()
                    buf = buf[len(buf) - keep :]

        async def backend_to_client() -> None:
            while True:
                chunk = await breader.read(65536)
                if not chunk:
                    return
                cwriter.write(chunk)
                await cwriter.drain()

        t1 = asyncio.ensure_future(client_to_backend())
        t2 = asyncio.ensure_future(backend_to_client())
        try:
            await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
        except Exception:
            pass
        finally:
            for task in (t1, t2):
                task.cancel()
            for writer in (cwriter, bwriter):
                with contextlib.suppress(Exception):
                    writer.close()


@dataclass
class _Rig:
    """A dedicated server + interposed proxy + runner, with isolated home."""

    base_url: str
    runner_id: str
    proxy: _Spec503Proxy
    work: Path
    server_log: Path
    runner_log: Path


@pytest.fixture
def spec_5xx_rig(
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
    request: pytest.FixtureRequest,
) -> Iterator[_Rig]:
    """Spawn an isolated server + spec-503 proxy + runner.

    ``RUNNER_SERVER_URL`` points the runner at the proxy, so every runner ->
    server HTTP call (including the agent-bundle fetch) and the WebSocket tunnel
    ride through it, while the test client and the browser talk to the real
    server directly. The openai-agents harness is pointed at the shared mock LLM
    so a recovered turn completes without real provider credentials.
    """
    if request.config.getoption("--ui-base-url"):
        pytest.skip("spec-5xx turn-setup e2e requires an isolated spawned server")

    work = tmp_path_factory.mktemp("spec_5xx_turn_setup")
    config_home = work / "config-home"
    home_dir = work / "home"
    artifacts = work / "artifacts"
    for path in (config_home, home_dir, artifacts):
        path.mkdir(parents=True, exist_ok=True)

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    proxy = _Spec503Proxy(backend_port=port)
    runner_server_url = f"http://127.0.0.1:{proxy.port}"
    binding_token = secrets.token_urlsafe(32)

    from omnigent.runner.identity import token_bound_runner_id

    runner_id = token_bound_runner_id(binding_token)

    pythonpath = os.pathsep.join(
        [
            str(_REPO_ROOT),
            str(_REPO_ROOT / "sdks" / "python-client"),
            str(_REPO_ROOT / "sdks" / "ui"),
            os.environ.get("PYTHONPATH", ""),
        ]
    )
    shared_env = {
        **_no_proxy_env(),
        "PYTHONPATH": pythonpath,
        "OMNIGENT_CONFIG_HOME": str(config_home),
        "HOME": str(home_dir),
        "PYTHONUNBUFFERED": "1",
        # Route the openai-agents harness (hello_world pins gpt-4o-mini) at the
        # mock LLM so a recovered turn needs no real provider credentials.
        "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
        "OPENAI_API_KEY": "mock-key",
    }
    server_env = {**shared_env, "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token}
    runner_env = {
        **shared_env,
        "OMNIGENT_RUNNER_ID": runner_id,
        "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
        "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
        # The runner reaches the server through the interposed proxy.
        "RUNNER_SERVER_URL": runner_server_url,
        "OMNIGENT_LOG_TO_STDERR": "1",
        "OMNIGENT_LOG_LEVEL": "DEBUG",
        "OMNIGENT_DATA_DIR": str(work / "runner-data"),
    }

    server_log = work / "server.log"
    runner_log = work / "runner.log"
    server_handle = server_log.open("w")
    runner_handle = runner_log.open("w")
    server_proc: subprocess.Popen[bytes] | None = None
    runner_proc: subprocess.Popen[bytes] | None = None
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
                time.sleep(0.5)
                continue
            time.sleep(0.5)
        if not online:
            raise RuntimeError(
                "spec-5xx rig did not come online within "
                f"{_HEALTH_TIMEOUT_S:.0f}s.\nServer log:\n{server_log.read_text()[-3000:]}\n"
                f"Runner log:\n{runner_log.read_text()[-3000:]}"
            )

        yield _Rig(
            base_url=base_url,
            runner_id=runner_id,
            proxy=proxy,
            work=work,
            server_log=server_log,
            runner_log=runner_log,
        )
    finally:
        proxy.stop()
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


def _create_hello_world_session(base_url: str) -> str:
    """Upload a ``hello_world`` bundle and return the new session id (unbound)."""
    bundle = _build_hello_world_bundle()
    create = _client.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        timeout=30.0,
    )
    create.raise_for_status()
    return str(create.json()["session_id"])


def _bind_session_to_runner(base_url: str, session_id: str, runner_id: str) -> None:
    """PATCH-bind *session_id* to *runner_id* (this triggers the runner's
    session-init handshake, the first agent-bundle resolve)."""
    patch = _client.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"runner_id": runner_id},
        timeout=10.0,
    )
    patch.raise_for_status()


def _real_runner_log(work: Path) -> str:
    """Text of the runner's real process-log file.

    The runner reconfigures logging to its own timestamped file under
    ``$HOME/.omnigent/logs/runner/`` (not its captured stdout), so read the
    newest such file to see the spec-resolution failure signature.
    """
    candidates = sorted(work.glob("**/logs/runner/*.log"), key=lambda p: p.stat().st_mtime)
    if candidates:
        return candidates[-1].read_text(errors="replace")
    all_logs = sorted(work.glob("**/*.log"), key=lambda p: p.stat().st_mtime)
    if not all_logs:
        return "(no *.log files found under the rig workdir)"
    return all_logs[-1].read_text(errors="replace")


@pytest.mark.timeout(900)
def test_agent_bundle_5xx_during_turn_setup_does_not_brick_the_turn(
    page: Page,
    spec_5xx_rig: _Rig,
    mock_llm_server_url: str,
) -> None:
    """A transient 5xx on the agent-bundle fetch must not permanently fail the turn.

    Create a ``hello_world`` session bound to a runner, open it in the web app,
    and send a message while the runner -> server agent-bundle fetch returns a
    transient 5xx. A resolver that gives up on the first 5xx aborts turn setup
    and the SPA shows the error pill with no reply; a resolver that rides it
    out completes the turn against the mock LLM.
    """
    rig = spec_5xx_rig

    # A working model reply so a retry-tolerant (fixed) runner completes the
    # turn cleanly -- the fail -> pass side of the guard.
    set_fallback_mock_llm(mock_llm_server_url, "gpt-4o-mini", "Hello! How can I help you today?")
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "Hello! How can I help you today?"}],
        key="gpt-4o-mini",
    )

    session_id = _create_hello_world_session(rig.base_url)

    # Arm before binding: the bind triggers the runner's session-init resolve.
    # If that succeeded, the turn would reuse the cached spec and never hit the
    # fault window during turn setup.
    rig.proxy.arm(session_id)

    _bind_session_to_runner(rig.base_url, session_id, rig.runner_id)

    try:
        page.goto(f"{rig.base_url}/c/{session_id}")
        composer = page.get_by_placeholder(_COMPOSER)
        expect(composer).to_be_visible(timeout=30_000)

        composer.fill("Say hello")
        page.get_by_role("button", name="Send", exact=True).click()

        error_pill = page.locator(_ERROR_PILL)
        assistant = page.locator(_ASSISTANT)

        # Wait for a definitive turn outcome: the failed status drives the error
        # pill (bug) or the mock reply lands as an assistant bubble (fixed).
        expect(error_pill.or_(assistant).first).to_be_visible(timeout=_OUTCOME_TIMEOUT_MS)

        # Prove the fault was actually exercised: a green pass with
        # injected_count == 0 would mean the 5xx never fired.
        assert rig.proxy.injected_count >= 1, (
            "the agent-bundle fetch never hit the degraded proxy path "
            f"(injected_count=0, match_count={rig.proxy.match_count}); the "
            "reproduction did not inject its fault.\nRunner log tail:\n"
            f"{_real_runner_log(rig.work)[-3000:]}"
        )

        if error_pill.count() > 0:
            headline = ""
            with contextlib.suppress(Exception):
                headline = page.locator(_ERROR_HEADLINE).first.inner_text()
            runner_log = _real_runner_log(rig.work)
            # Confirm the failure is THIS bug (spec-resolution 5xx), not incidental.
            assert (
                "spec_resolver" in runner_log
                or "Spec resolution failed" in runner_log
                or "turn setup failed" in runner_log
            ), (
                "the turn failed, but the runner log shows no spec-resolution "
                "failure signature.\nRunner log tail:\n" + runner_log[-4000:]
            )
            pytest.fail(
                "a transient 5xx on the agent-bundle fetch bricked turn setup: "
                "the resolver raised on the first non-200 with no retry, the turn "
                "aborted before harness selection, and it surfaced to the UI as "
                f"failed (error pill headline={headline!r}, injected_count="
                f"{rig.proxy.injected_count}, match_count={rig.proxy.match_count}). "
                "No assistant reply arrived."
            )

        # No runner-setup error pill and a real reply: the resolver rode out the
        # transient 5xx and the turn completed.
        expect(assistant.first).to_be_visible(timeout=_OUTCOME_TIMEOUT_MS)
        expect(error_pill).to_have_count(0)
    finally:
        with contextlib.suppress(httpx.HTTPError):
            _client.delete(f"{rig.base_url}/v1/sessions/{session_id}", timeout=10.0)

"""E2E: a transient slow session-snapshot read must not brick a cursor-native launch.

Regression test for the cursor-native terminal auto-create failure signature
``Failed to auto-create cursor terminal for <session>`` (runner logger
``omnigent.runner.app`` / ``_launch_native_terminal``): on bind, the
runner's cursor launch path
reads the session snapshot exactly once --
``_pi_native_launch_config`` in ``omnigent/runner/native/orchestration.py``
issues a single ``GET /v1/sessions/{id}`` with a hard 10s read budget and no
retry -- so one slow response (a genuine ``httpx.ReadTimeout``: a saturated
uplink, a briefly stalled server, a laptop waking from sleep) permanently
fails the whole terminal auto-create. The runner catches the resulting
``RuntimeError: Could not fetch Pi launch config for '<id>'.``, logs
``Failed to auto-create cursor terminal for <id>`` and publishes
``session.status: failed`` ("Native Cursor terminal failed to start; see the
runner log ...") with no terminal and no retry, even though the degradation
cleared seconds later. The user sees the session go failed (the SPA error
pill) instead of the cursor-agent TUI.

The journey this drives is the reported one: start a Cursor wrapper session
(the exact terminal-first spec ``omnigent cursor`` ships), open it in the web
app, and let the runner bind + auto-create the terminal while the runner's
network path to the server is briefly degraded. The degradation is injected
by a loopback TCP proxy interposed between the runner and the server that
forwards the two bind/init snapshot reads untouched and then holds ONLY the
launch-config GET for this session past the fetch's 10s read budget --
disarming itself the instant it captures that one request, so exactly one
fetch is degraded (a single transient timeout, matching the field signature:
a handful of events, no broad outage).

While the bug is live the launch dies on that one slow read and this test
FAILS with the launch-config error (session ``failed``, no terminal); once the
fetch tolerates a transient slow read (a retry, a saner budget), the terminal
comes up and the test passes. The proxy has already disarmed by the time a fix
retries, so the retry's second GET flows instantly -- the fixed path recovers
and the test transitions fail -> pass.

The rig mirrors ``test_native_pinned_model_namespace_launch.py``: a dedicated
server + runner pair with isolated ``HOME`` / ``OMNIGENT_CONFIG_HOME`` so no
ambient provider config or credential leaks in, plus the interposed proxy. No
LLM traffic is needed -- the failure under test happens before the TUI would
render anything, and on the fixed path the terminal resource registers as
soon as the pane launches, even without a usable cursor login.
"""

from __future__ import annotations

import asyncio
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
import tempfile
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

_REPO_ROOT = Path(__file__).resolve().parents[3]

# Boot budget for the spawned server + proxy + runner trio.
_HEALTH_TIMEOUT_S = 120.0
# Launch outcome budget: bind-time snapshot reads each ride out the injected
# delay before the launch-config fetch runs, plus the tmux/CLI boot on the
# fixed path.
_OUTCOME_TIMEOUT_S = 240.0
# How long the degraded path holds a session-snapshot GET. Must exceed the
# launch-config fetch's 10s read budget so that caller times out for real,
# while callers with no read timeout only see a slow server and are served
# late (they ride the stall out).
_STALL_S = 12.0

#: Number of leading ``GET /v1/sessions/{id}`` reads the bind/init path issues
#: BEFORE the launch-config fetch under test. On bind the runner reads the
#: session snapshot twice (the bind/init handler, then
#: ``_resolve_session_agent_spec_or_none``) and only then does
#: ``_auto_create_cursor_terminal`` -> ``_pi_native_launch_config`` issue the
#: launch-config fetch. These leading reads use no read budget (they ride out a
#: slow server); the launch-config fetch has the hard 10s budget under test. So
#: the proxy forwards the first two matches untouched and degrades only the
#: third -- the launch-config fetch -- so the fault lands where the bug lives
#: (not on init, which would merely hang the launch). Observed stable across
#: runs; if the init path changes its read count the ``stall_count >= 1`` guard
#: fires loudly rather than passing silently.
_INIT_SNAPSHOT_READS = 2

_ERROR_PILL = '[data-testid="error-pill"]'

# Proxy-blind client: CI forces an egress proxy via HTTP(S)_PROXY env vars
# that must not intercept loopback requests to the spawned server.
_client = httpx.Client(trust_env=False)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


#: Ambient variables that would otherwise leak into the rig: provider
#: credentials/config (the rig must resolve everything from its isolated
#: HOME / OMNIGENT_CONFIG_HOME), and runner/host identity from any outer
#: Omnigent runner this test itself runs under (a leaked
#: ``OMNIGENT_RUNNER_ZYGOTE*``/``OMNIGENT_RUNNER_ID`` makes the spawned
#: runner take the zygote-fork path and hang before coming online).
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
    # Process-identity leaks from an outer Omnigent harness: an inherited
    # process-log file or data dir points the spawned server/runner at the
    # OUTER process's (possibly unwritable) tree and crashes them at boot.
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

    Used so a request line split across socket reads is never partially
    forwarded before it can be recognized: the trailing bytes that could be
    the start of the target request are held back until the rest arrives.
    Returns 0 (forward everything) when no suffix of *buf* is a prefix of the
    target -- the common case, so ordinary traffic (including the runner's
    WebSocket tunnel frames) relays with no added latency.
    """
    m = min(len(buf), len(pat) - 1)
    for k in range(m, 0, -1):
        if buf[-k:] == pat[:k]:
            return k
    return 0


class _SnapshotDelayProxy:
    """Loopback TCP proxy (runner -> server) injecting a transient slow read.

    While armed, the exact request line ``GET /v1/sessions/<sid> HTTP/`` (the
    launch-config / snapshot fetch, and only that -- sub-paths like
    ``/v1/sessions/<sid>/agent`` do not match) is held :data:`_STALL_S`
    seconds instead of being forwarded, so its response never arrives inside
    that window. A caller whose read budget is shorter (the launch-config
    fetch's 10s) observes a genuine ``httpx.ReadTimeout`` on its own socket and
    closes the connection; a caller with no read timeout (the best-effort
    snapshot cache) rides the stall out and is served late once the held bytes
    are forwarded. All other traffic (other routes, the WebSocket tunnel that
    keeps the runner online) relays untouched.

    The degradation is TRANSIENT: the proxy disarms itself the instant it
    captures the one request it will hold, so any later request -- including a
    fix's retry -- flows instantly, while only that single captured request is
    held past its read budget. Exactly one request is degraded, mirroring a
    single transient network blip (the field signature is a handful of events,
    not a broad outage), and -- crucially -- a fix that retries the fetch
    succeeds on its second attempt, so the test transitions fail -> pass.
    """

    def __init__(self, backend_port: int) -> None:
        self._backend_port = backend_port
        self.port = _free_port()
        self._pattern: bytes | None = None
        # Let the first ``_stall_after`` matching GETs pass through untouched
        # (the bind/init snapshot reads), and only degrade the match after that
        # -- so the fault lands on the launch-config fetch, not on init.
        self._stall_after = _INIT_SNAPSHOT_READS
        self.match_count = 0
        self.stall_count = 0
        self.timed_out_count = 0
        self._started = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread = threading.Thread(target=self._run, name="snapshot-delay-proxy", daemon=True)
        self._thread.start()
        if not self._started.wait(timeout=10.0):
            raise RuntimeError("snapshot-delay proxy failed to start")

    def arm(self, session_id: str, *, stall_after: int = _INIT_SNAPSHOT_READS) -> None:
        """Degrade exact snapshot GETs for *session_id* until one times out.

        :param stall_after: Number of leading matching GETs to forward
            untouched before degrading begins; the bind/init snapshot reads
            precede the launch-config fetch and must not be stalled.
        """
        self._stall_after = stall_after
        self._pattern = f"GET /v1/sessions/{session_id} HTTP/".encode()

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
                # Once this connection is identified as the WebSocket tunnel,
                # stop scanning it: it is long-lived binary frames, never the
                # target request line, and must never incur match latency.
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
                        f"[snapshot-delay-proxy] match #{idx} "
                        f"(stall_after={self._stall_after}) at t={time.monotonic():.1f}\n"
                    )
                    sys.stderr.flush()
                    if idx <= self._stall_after:
                        # A leading bind/init snapshot read: forward untouched.
                        bwriter.write(buf)
                        await bwriter.drain()
                        buf = b""
                        continue
                    self.stall_count += 1
                    # One-shot transient blip: disarm NOW, the instant this one
                    # request (the launch-config fetch) is captured, so any
                    # RETRY -- a fix's second fetch -- or any later request
                    # flows instantly. Only THIS already-captured request stays
                    # held past the caller's read budget; the degradation has
                    # cleared for everything after it. Without this the pattern
                    # would still be armed when a fix retried and the fix would
                    # re-time-out, so the test could never go fail -> pass.
                    self._pattern = None
                    # Hold the request. Watch whether the caller hangs up (its
                    # read budget firing -> EOF) before the delay elapses.
                    try:
                        extra = await asyncio.wait_for(creader.read(65536), timeout=_STALL_S)
                    except (TimeoutError, asyncio.TimeoutError):
                        extra = None  # caller rode out the slow read
                    if extra == b"":
                        # The caller gave up mid-stall: its read timed out and
                        # it closed the connection -- drop it (already disarmed).
                        self.timed_out_count += 1
                        return
                    # Slow-server case (rode the stall out, or sent more): the
                    # held request is now forwarded and served late.
                    bwriter.write(buf)
                    if extra:
                        bwriter.write(extra)
                    await bwriter.drain()
                    buf = b""
                    continue
                # No match yet: forward everything except a trailing partial
                # that could still become the target request line.
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
class _CursorRig:
    """A dedicated server + interposed proxy + runner, with isolated home."""

    base_url: str
    runner_id: str
    proxy: _SnapshotDelayProxy
    work: Path
    server_log: Path
    runner_log: Path


@pytest.fixture
def cursor_launch_rig(
    built_spa: None,
    tmp_path_factory: pytest.TempPathFactory,
    request: pytest.FixtureRequest,
) -> Iterator[_CursorRig]:
    """Spawn an isolated server + snapshot-delay proxy + runner.

    ``RUNNER_SERVER_URL`` points the runner at the proxy, so every runner ->
    server HTTP call (including the cursor launch-config fetch) and the
    WebSocket tunnel ride through it, while the test client and the browser
    talk to the real server directly.

    :returns: The rig handle (real server base URL, runner id, proxy).
    """
    if request.config.getoption("--ui-base-url"):
        pytest.skip("cursor launch-timeout e2e requires an isolated spawned server")

    work = tmp_path_factory.mktemp("cursor_launch_timeout")
    config_home = work / "config-home"
    home_dir = work / "home"
    artifacts = work / "artifacts"
    for path in (config_home, home_dir, artifacts):
        path.mkdir(parents=True, exist_ok=True)

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    proxy = _SnapshotDelayProxy(backend_port=port)
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
        # Unbuffer subprocess stdout so the log file reflects the runner's
        # progress in real time -- a block-buffered child shows an empty log
        # while it is still alive, which hides both the launch failure
        # signature and any hang mid-launch.
        "PYTHONUNBUFFERED": "1",
    }
    server_env = {**shared_env, "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token}
    runner_env = {
        **shared_env,
        "OMNIGENT_RUNNER_ID": runner_id,
        "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
        "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
        # The runner reaches the server through the interposed proxy.
        "RUNNER_SERVER_URL": runner_server_url,
        # The runner reconfigures logging to its own process-log file (under
        # the data dir), so its captured stdout stays empty. Pin the data dir
        # to a known path inside the rig workdir so the launch-failure
        # signature is readable from a stable location.
        "OMNIGENT_LOG_TO_STDERR": "1",
        "OMNIGENT_LOG_LEVEL": "DEBUG",
        "OMNIGENT_DATA_DIR": os.environ.get(
            "CURSOR_LAUNCH_E2E_DATA_DIR", str(work / "runner-data")
        ),
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
                "cursor launch-timeout rig did not come online within "
                f"{_HEALTH_TIMEOUT_S:.0f}s.\nServer log:\n{server_log.read_text()[-3000:]}\n"
                f"Runner log:\n{runner_log.read_text()[-3000:]}"
            )

        yield _CursorRig(
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


def _create_unbound_cursor_session(base_url: str) -> str:
    """Register the cursor-native wrapper agent and create its session, unbound.

    Mirrors the conftest ``_create_native_cursor_session`` factory (the exact
    terminal-first spec ``omnigent cursor`` ships + the wrapper / terminal-first
    labels the CLI writes), but does NOT bind the runner: the journey pins the
    proxy fault before the bind-triggered terminal auto-create runs, so the
    launch-config fetch under test is the one that hits the degraded path.

    :param base_url: Real (non-proxied) server base URL.
    :returns: The new session/conversation id.
    """
    from omnigent._wrapper_labels import (
        CURSOR_NATIVE_WRAPPER_VALUE,
        UI_MODE_LABEL_KEY,
        UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY,
    )
    from omnigent.harnesses.cursor_native.main import _materialize_cursor_agent_spec

    with tempfile.TemporaryDirectory() as tmp:
        spec_path = _materialize_cursor_agent_spec(Path(tmp))
        yaml_text = spec_path.read_text()

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = yaml_text.encode()
        # Non-config.yaml arcname -> omnigent compat translator (the spec has
        # no spec_version), matching the conftest native session factories.
        info = tarfile.TarInfo("cursor-native-ui.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    labels = {
        UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY: CURSOR_NATIVE_WRAPPER_VALUE,
    }
    # Cursor keys its chat store on md5(cwd) and needs a concrete launch cwd;
    # the repo root is a valid dir on the runner. ``-f`` trusts the dir +
    # auto-approves tools so the unattended pane never blocks on a prompt (only
    # relevant on the fixed path, where the pane actually launches).
    metadata = {
        "labels": labels,
        "workspace": str(_REPO_ROOT),
        "terminal_launch_args": ["-f"],
    }
    create = _client.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps(metadata)},
        files={"bundle": ("cursor-native-ui.tar.gz", buf.getvalue(), "application/gzip")},
        timeout=30.0,
    )
    create.raise_for_status()
    return str(create.json()["session_id"])


def _real_runner_log(work: Path) -> str:
    """Text of the runner's real process-log file.

    The runner reconfigures logging to its own timestamped file under
    ``$HOME/.omnigent/logs/runner/`` (not its captured stdout/stderr), so the
    subprocess ``runner.log`` handle stays empty. Read the newest such file so
    the launch-failure signature and any hang are actually observable.
    """
    candidates = sorted(work.glob("**/logs/runner/*.log"), key=lambda p: p.stat().st_mtime)
    if candidates:
        return candidates[-1].read_text(errors="replace")
    # Fall back to any *.log under the workdir so a diagnostic run reveals
    # where the runner actually wrote (and whether it wrote at all).
    all_logs = sorted(work.glob("**/*.log"), key=lambda p: p.stat().st_mtime)
    listing = "\n".join(f"  {p} ({p.stat().st_size}B)" for p in all_logs)
    if not all_logs:
        return "(no *.log files found under the rig workdir)"
    newest = all_logs[-1]
    return f"(no logs/runner/*.log; all logs under workdir:\n{listing}\n)\n" + newest.read_text(
        errors="replace"
    )


def _session_terminal_exists(base_url: str, session_id: str) -> bool:
    """Whether a terminal resource has registered for *session_id*."""
    resources = _client.get(f"{base_url}/v1/sessions/{session_id}/resources", timeout=5.0)
    if resources.status_code != 200:
        return False
    payload = resources.json()
    rows = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return False
    return any(isinstance(row, dict) and row.get("type") == "terminal" for row in rows)


def _session_launch_error(base_url: str, session_id: str) -> str | None:
    """The session's persisted launch-failure detail, if any.

    A native-terminal start abort publishes ``session.status: failed`` whose
    error the server persists and projects into the snapshot's
    ``last_task_error`` -- the durable, user-facing failure record.

    :returns: The failure message when the launch recorded one, else ``None``.
    """
    snapshot = _client.get(f"{base_url}/v1/sessions/{session_id}", timeout=5.0)
    if snapshot.status_code != 200:
        return None
    error = snapshot.json().get("last_task_error")
    if isinstance(error, dict):
        message = str(error.get("message") or "")
        if message:
            return message
    return None


@pytest.mark.timeout(900)
def test_transient_slow_snapshot_read_does_not_brick_cursor_launch(
    page: Page,
    cursor_launch_rig: _CursorRig,
) -> None:
    """A one-off slow launch-config read must not permanently fail the terminal.

    Journey (the reported one): create a Cursor wrapper session, open it in the
    web app, and bind the runner while the runner -> server path is briefly
    degraded so the single ``GET /v1/sessions/{id}`` launch-config fetch reads
    slower than its 10s budget.

    The original bug fails the whole terminal auto-create on that one slow read
    (session ``failed``, no terminal, no retry) -- this test FAILS with the
    launch-config error while the bug is live. A fix that tolerates the
    transient slow read (retry / saner budget) brings the terminal up; the
    transient proxy has disarmed by then, so the retry succeeds and the test
    passes. Skips without the cursor CLI on PATH (the fixed path launches it).
    """
    if shutil.which("cursor-agent") is None:
        pytest.skip("cursor-agent CLI is required for the cursor-native launch e2e")

    rig = cursor_launch_rig

    # Steps 1-2: the user's Cursor session exists, unbound.
    session_id = _create_unbound_cursor_session(rig.base_url)

    # Degrade the exact launch-config fetch for THIS session (transient: it
    # disarms after the first read that times out). Set CURSOR_LAUNCH_E2E_NO_ARM=1
    # to run the no-stall baseline (does binding alone launch the terminal?).
    if not os.environ.get("CURSOR_LAUNCH_E2E_NO_ARM"):
        rig.proxy.arm(
            session_id,
            stall_after=int(
                os.environ.get("CURSOR_LAUNCH_E2E_STALL_AFTER", str(_INIT_SNAPSHOT_READS))
            ),
        )

    try:
        # Step 3: open the session in the web app, then bind the runner -- the
        # bind triggers the cursor terminal auto-create, whose launch-config
        # fetch now hits the degraded path.
        page.goto(f"{rig.base_url}/c/{session_id}")
        # The bind is synchronous: the server holds the PATCH open while the
        # runner runs the terminal auto-create. With the launch-config fetch
        # stalled, that wait outlasts a tight client timeout -- runner_id is
        # persisted before the dispatch, so a client-side timeout still leaves
        # the launch triggered. Tolerate it and poll the durable outcome below.
        with contextlib.suppress(httpx.HTTPError):
            _client.patch(
                f"{rig.base_url}/v1/sessions/{session_id}",
                json={"runner_id": rig.runner_id},
                timeout=120.0,
            )

        # Outcome: the terminal registers (launch tolerated the slow read), or
        # the launch aborts. The terminal-start abort is published as a
        # TRANSIENT ``session.status: failed`` SSE event -- the server does not
        # persist it into the snapshot's ``last_task_error`` and the session
        # settles back to ``idle`` with no terminal -- so its durable trace is
        # the runner-log signature (``Failed to auto-create cursor terminal``).
        # Detect the failure there so the loop breaks the moment the launch
        # dies (~seconds in) instead of waiting out the full outcome budget.
        launched = False
        launch_failed = False
        deadline = time.monotonic() + _OUTCOME_TIMEOUT_S
        while time.monotonic() < deadline:
            if _session_terminal_exists(rig.base_url, session_id):
                launched = True
                break
            if "Failed to auto-create cursor terminal" in _real_runner_log(rig.work):
                launch_failed = True
                break
            time.sleep(2.0)

        # Prove the fault was actually exercised: the launch-config fetch must
        # have hit the degraded path. A green pass with stall_count == 0 would
        # be a false pass (the request never went through the proxy).
        if not os.environ.get("CURSOR_LAUNCH_E2E_NO_ARM"):
            assert rig.proxy.stall_count >= 1, (
                "the launch-config fetch never traversed the degraded proxy path "
                f"(stall_count=0); the reproduction did not inject its fault.\n"
                f"Runner log tail:\n{_real_runner_log(rig.work)[-3000:]}"
            )

        if launch_failed:
            # The failure the user sees: the transient ``session.status: failed``
            # edge drives the SPA error pill. Best-effort -- the status reverts
            # to idle, so tolerate a miss and never mask the primary assertion.
            with contextlib.suppress(AssertionError):
                expect(page.locator(_ERROR_PILL).first).to_be_visible(timeout=15_000)

            runner_log = _real_runner_log(rig.work)
            assert "Could not fetch Pi launch config" in runner_log, (
                "cursor terminal auto-create failed, but not via the launch-config "
                "fetch timeout under test.\nRunner log tail:\n" + runner_log[-3000:]
            )
            # The published (transient) failure detail, for the failure message.
            launch_error = _session_launch_error(rig.base_url, session_id)
            pytest.fail(
                "cursor-native terminal auto-create was permanently bricked by a "
                "single transient slow launch-config read (stall_count="
                f"{rig.proxy.stall_count}, timed_out_count={rig.proxy.timed_out_count}"
                "): the terminal never came up and the runner logged 'Failed to "
                "auto-create cursor terminal' / 'Could not fetch Pi launch config' "
                f"with no retry. Published error: {launch_error!r}."
            )

        if not launched:
            snapshot_text = _client.get(
                f"{rig.base_url}/v1/sessions/{session_id}", timeout=5.0
            ).text[:2000]
            pytest.fail(
                "cursor-native terminal neither launched nor logged the launch-config "
                f"failure within {_OUTCOME_TIMEOUT_S:.0f}s (stall_count="
                f"{rig.proxy.stall_count}, timed_out_count={rig.proxy.timed_out_count}).\n"
                f"Session snapshot:\n{snapshot_text}\n"
                f"Runner log tail:\n{_real_runner_log(rig.work)[-6000:]}\n"
                f"Server log tail:\n{rig.server_log.read_text()[-1500:]}"
            )
    finally:
        with contextlib.suppress(httpx.HTTPError):
            _client.delete(f"{rig.base_url}/v1/sessions/{session_id}", timeout=10.0)

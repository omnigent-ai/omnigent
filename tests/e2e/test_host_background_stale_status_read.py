"""E2E regression: ``omnigent host --background`` tears down a daemon that
had already registered.

Reproduces the reported failure. ``omnigent host --background`` (and its
``omnigent start`` alias) spawns a detached host daemon and then, before
reporting success, polls a *secondary* readiness endpoint,
``GET /v1/hosts/{host_id}``, until it reports ``online`` (see
``_confirm_background_host_registered`` in ``omnigent/cli.py``). The daemon's
actual registration happens over a **different** transport: an authenticated
WebSocket tunnel. When the two diverge -- the tunnel registers fine but the
CLI's status read is stale/blocked/routed differently and keeps answering
"offline" -- the CLI hits its fixed 30s grace, declares the daemon never
registered, and force-terminates a daemon that is genuinely online, killing a
healthy host the user asked to start.

The test stands up a transparent reverse proxy in front of the e2e
``live_server``. The proxy forwards the WebSocket tunnel and every other HTTP
call straight through -- so the daemon registers on the real server for real --
but rewrites the single-host readiness read ``GET /v1/hosts/{host_id}`` to
report ``offline``, modeling the divergent/stale status read from the report.
It then runs the real ``omnigent host --server <proxy> --background`` command.
The test encodes the *correct* behavior, so it FAILS while the bug is present
and PASSES once the daemon that registered is no longer torn down:

- the daemon genuinely reaches ``online`` on the REAL server (bypassing the
  proxy, via the direct ``http_client``) -- i.e. it *did* register; then
- ``omnigent host --background`` must succeed (exit 0, no "did not register
  within 30s") and leave that healthy, registered daemon running (its pid
  stays alive and its registry record survives).

With the bug present, the CLI instead reports the 30s registration timeout and
force-terminates the online daemon, so those assertions fail.

Run with::

    .venv/bin/python -m pytest \\
        tests/e2e/test_host_background_stale_status_read.py -v
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
import httpx
import pytest
import yaml
from aiohttp import web

from tests._helpers.compat import apply_runner_env, compat_runner_cwd, runner_executable
from tests.e2e.conftest import POLL_INTERVAL_S, find_free_port

# The CLI's background-registration grace is a fixed 30s constant with no env
# override (``_BACKGROUND_HOST_REGISTRATION_GRACE_S`` in ``omnigent/cli.py``),
# so the command runs for ~30s before it gives up and tears the daemon down.
# The overall cap is generous enough for that plus daemon spawn + teardown on a
# loaded CI box.
_CLI_TIMEOUT_S = 90.0

# Exactly ``/v1/hosts/<one-segment>`` (the single-host readiness read the CLI
# polls) -- not ``/v1/hosts`` (the list) nor ``/v1/hosts/<id>/tunnel`` (the
# WebSocket registration transport, which must pass through untouched).
_HOST_STATUS_RE = re.compile(r"^/v1/hosts/[^/]+$")

# Hop-by-hop / handshake headers that must not be copied verbatim when relaying.
_SKIP_REQ_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "transfer-encoding",
        "connection",
        "keep-alive",
        "upgrade",
        "sec-websocket-key",
        "sec-websocket-version",
        "sec-websocket-extensions",
        "sec-websocket-protocol",
        "accept-encoding",
    }
)
_SKIP_RESP_HEADERS = frozenset(
    {"content-length", "transfer-encoding", "connection", "content-encoding"}
)


class _StaleStatusProxy:
    """A reverse proxy that makes the single-host readiness read go stale.

    Everything -- the WebSocket tunnel, ``/health``, ``/v1/me``, the
    ``/v1/hosts`` list, POSTs -- is forwarded transparently to *backend_url*,
    so a daemon dialing through the proxy registers on the real server exactly
    as it would directly. The one exception: a ``GET /v1/hosts/{host_id}`` that
    the backend answers ``200 {"status": "online", ...}`` is rewritten to
    ``"offline"`` before it reaches the client, modeling the divergent/stale
    status read from the report. ``stale_reads`` counts how many such reads were
    rewritten, so the test can prove it actually exercised that path.
    """

    def __init__(self, backend_url: str) -> None:
        parsed = urlparse(backend_url)
        assert parsed.hostname is not None and parsed.port is not None
        self._backend_host = parsed.hostname
        self._backend_port = parsed.port
        self.port = find_free_port()
        self.stale_reads = 0
        self._loop = asyncio.new_event_loop()
        self._session: aiohttp.ClientSession | None = None
        self._runner: web.AppRunner | None = None
        ready = threading.Event()
        self._start_error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, args=(ready,), daemon=True)
        self._thread.start()
        assert ready.wait(30), "stale-status proxy did not start within 30s"
        if self._start_error is not None:
            raise self._start_error

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _run(self, ready: threading.Event) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._start())
        except BaseException as exc:  # pragma: no cover - startup failure path
            self._start_error = exc
            ready.set()
            return
        ready.set()
        self._loop.run_forever()

    async def _start(self) -> None:
        self._session = aiohttp.ClientSession(auto_decompress=False)
        app = web.Application(client_max_size=200 * 1024 * 1024)
        app.router.add_route("*", "/{tail:.*}", self._handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", self.port)
        await site.start()

    def _fwd_headers(self, request: web.Request) -> dict[str, str]:
        return {k: v for k, v in request.headers.items() if k.lower() not in _SKIP_REQ_HEADERS}

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        # The daemon's registration transport: forward the upgrade to the
        # backend and pump both ways so it registers for real.
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await self._proxy_ws(request)

        assert self._session is not None
        target = f"http://{self._backend_host}:{self._backend_port}{request.rel_url}"
        body = await request.read()
        async with self._session.request(
            request.method,
            target,
            headers=self._fwd_headers(request),
            data=body or None,
            allow_redirects=False,
        ) as upstream:
            raw = await upstream.read()
            headers = {
                k: v for k, v in upstream.headers.items() if k.lower() not in _SKIP_RESP_HEADERS
            }
            if (
                request.method == "GET"
                and _HOST_STATUS_RE.match(request.path)
                and upstream.status == 200
                and upstream.headers.get("Content-Type", "").startswith("application/json")
            ):
                try:
                    payload = json.loads(raw)
                except ValueError:
                    payload = None
                if isinstance(payload, dict) and payload.get("status") == "online":
                    payload["status"] = "offline"
                    raw = json.dumps(payload).encode()
                    headers["Content-Type"] = "application/json"
                    self.stale_reads += 1
            return web.Response(status=upstream.status, headers=headers, body=raw)

    async def _proxy_ws(self, request: web.Request) -> web.StreamResponse:
        assert self._session is not None
        server_ws = web.WebSocketResponse(max_msg_size=0, heartbeat=None)
        await server_ws.prepare(request)
        target = f"ws://{self._backend_host}:{self._backend_port}{request.rel_url}"
        async with self._session.ws_connect(
            target,
            headers=self._fwd_headers(request),
            max_msg_size=0,
            autoping=True,
        ) as client_ws:

            async def upstream_to_downstream() -> None:
                async for msg in client_ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await server_ws.send_str(msg.data)
                    elif msg.type == aiohttp.WSMsgType.BINARY:
                        await server_ws.send_bytes(msg.data)
                    elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING):
                        break
                if not server_ws.closed:
                    await server_ws.close()

            async def downstream_to_upstream() -> None:
                async for msg in server_ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await client_ws.send_str(msg.data)
                    elif msg.type == aiohttp.WSMsgType.BINARY:
                        await client_ws.send_bytes(msg.data)
                    elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING):
                        break
                if not client_ws.closed:
                    await client_ws.close()

            await asyncio.gather(upstream_to_downstream(), downstream_to_upstream())
        return server_ws

    def close(self) -> None:
        async def _shutdown() -> None:
            if self._session is not None:
                await self._session.close()
            if self._runner is not None:
                await self._runner.cleanup()

        with contextlib.suppress(Exception):
            asyncio.run_coroutine_threadsafe(_shutdown(), self._loop).result(timeout=10)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=10)


def _pid_alive(pid: int) -> bool:
    """Return whether *pid* names a live process (signal-0 probe)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _read_daemon_record(daemons_dir: Path) -> dict[str, object] | None:
    """Return the (single) daemon registry record if it has been written."""
    if not daemons_dir.is_dir():
        return None
    records = sorted(daemons_dir.glob("*.json"))
    if not records:
        return None
    try:
        return json.loads(records[0].read_text())
    except (OSError, ValueError):
        return None


def _host_log_tail(home: Path, lines: int = 30) -> str:
    """Return the tail of the newest detached-daemon host log under *home*."""
    log_dir = home / ".omnigent" / "logs" / "host"
    logs = sorted(log_dir.glob("*.log")) if log_dir.is_dir() else []
    if not logs:
        return "(no host log found)"
    try:
        return "\n".join(logs[-1].read_text().splitlines()[-lines:])
    except OSError:
        return "(host log unreadable)"


def _host_online(client: httpx.Client, host_id: str) -> bool:
    """Whether the REAL server (bypassing the proxy) reports *host_id* online."""
    try:
        resp = client.get("/v1/hosts")
    except httpx.HTTPError:
        return False
    if resp.status_code != 200:
        return False
    return any(
        h.get("host_id") == host_id and h.get("status") == "online"
        for h in resp.json().get("hosts", [])
    )


@pytest.mark.timeout(180)
def test_host_background_tears_down_registered_daemon_on_stale_status_read(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
    mock_llm_server_url: str,
) -> None:
    """``--background`` must not tear down a daemon that already registered.

    Journey (from the report): run ``omnigent host --background`` against a
    server whose ``GET /v1/hosts/{id}`` readiness read is stale/divergent while
    the WebSocket tunnel registers fine. The daemon connects and the server
    marks the host online. Correct behavior: the CLI reports success and leaves
    the daemon running. Bug: the CLI's status probe keeps seeing "offline",
    hits its 30s grace, and force-terminates the healthy daemon -- so the
    assertions below fail until the readiness check stops relying solely on the
    divergent secondary read.

    :param live_server: Real ``omnigent server`` base URL (session fixture).
    :param http_client: HTTP client pointed straight at ``live_server`` (used
        as ground truth, bypassing the stale proxy).
    :param tmp_path: Per-test temp dir used as the daemon/CLI ``HOME``.
    :param mock_llm_server_url: Mock LLM server base URL.
    """
    proxy = _StaleStatusProxy(live_server)
    proc: subprocess.Popen[str] | None = None
    daemon_pid: int | None = None
    try:
        # Sanity: the server is reachable through the proxy, like a user URL.
        assert httpx.get(f"{proxy.url}/health", timeout=10.0).status_code == 200

        # Seed an isolated HOME with a known host id so both the spawned daemon
        # and the test can address the same host.
        host_id = uuid.uuid4().hex
        host_name = f"e2e-host-{uuid.uuid4().hex[:12]}"
        omni_dir = tmp_path / ".omnigent"
        omni_dir.mkdir(parents=True, exist_ok=True)
        (omni_dir / "config.yaml").write_text(
            yaml.safe_dump(
                {"host": {"host_id": host_id, "name": host_name}},
                default_flow_style=False,
                sort_keys=True,
            )
        )
        # Hermetic subprocess env: drop inherited ``OMNIGENT_``/``DATABRICKS_``
        # vars so the daemon reads a pristine config under our isolated HOME
        # (its registry lands under ``tmp_path/.omnigent/daemons``) instead of
        # a leaked provider config that would crash harness readiness.
        env = {
            k: v for k, v in os.environ.items() if not k.startswith(("OMNIGENT_", "DATABRICKS_"))
        }
        env["HOME"] = str(tmp_path)
        env["OMNIGENT_SKIP_ONBOARD"] = "1"
        env["OMNIGENT_NO_UPDATE_CHECK"] = "1"
        env["OPENAI_BASE_URL"] = f"{mock_llm_server_url}/v1"
        env["OPENAI_API_KEY"] = "mock-key"
        env = apply_runner_env(env)

        # Run the real user command. It blocks for the full 30s grace before
        # giving up, so drive it as a subprocess and watch the ground-truth
        # server concurrently.
        proc = subprocess.Popen(
            [
                runner_executable(),
                "-m",
                "omnigent",
                "host",
                "--server",
                proxy.url,
                "--background",
                "--non-interactive",
            ],
            env=env,
            cwd=compat_runner_cwd(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        daemons_dir = omni_dir / "daemons"
        online_seen = False
        deadline = time.monotonic() + _CLI_TIMEOUT_S
        while proc.poll() is None and time.monotonic() < deadline:
            if daemon_pid is None:
                record = _read_daemon_record(daemons_dir)
                if record is not None and isinstance(record.get("pid"), int):
                    daemon_pid = int(record["pid"])
            # Ground truth: has the daemon genuinely registered on the REAL
            # server (bypassing the stale proxy)?
            if _host_online(http_client, host_id):
                online_seen = True
            time.sleep(POLL_INTERVAL_S)

        out, err = proc.communicate(timeout=30)

        # A fast (fixed) success can exit before the concurrent poll observed
        # the online transition; confirm registration once more while the
        # daemon may still be running.
        if not online_seen:
            extra_deadline = time.monotonic() + 10.0
            while time.monotonic() < extra_deadline:
                if daemon_pid is None:
                    record = _read_daemon_record(daemons_dir)
                    if record is not None and isinstance(record.get("pid"), int):
                        daemon_pid = int(record["pid"])
                if _host_online(http_client, host_id):
                    online_seen = True
                    break
                time.sleep(POLL_INTERVAL_S)

        # Precondition (holds before and after the fix): the daemon genuinely
        # registered on the real server, so tearing it down is a bug.
        assert daemon_pid is not None, (
            "daemon record with a pid never appeared -- the background spawn "
            f"did not start a daemon. CLI stdout:\n{out}\nstderr:\n{err}"
        )
        assert online_seen, (
            "The daemon never reached 'online' on the real server, so the "
            "test did not model a genuinely *registered* daemon. "
            f"Daemon log tail:\n{_host_log_tail(tmp_path)}"
        )

        # Correct behavior: a daemon that already registered must NOT be torn
        # down. These fail while the bug is present -- the CLI reports the 30s
        # timeout and force-kills the healthy daemon. ``proxy.stale_reads``
        # (the divergent readiness reads that trigger the bug) is surfaced for
        # diagnosis, not asserted: a fix may stop polling that endpoint.
        assert proc.returncode == 0, (
            "omnigent host --background exited non-zero for a daemon that had "
            f"already registered online (divergent readiness reads="
            f"{proxy.stale_reads}). It force-tears down a healthy daemon "
            f"instead of recognizing the registration.\nstdout:\n{out}\n"
            f"stderr:\n{err}\ndaemon log tail:\n{_host_log_tail(tmp_path)}"
        )
        assert "did not register with the server within 30s" not in err, (
            "CLI declared the daemon never registered even though it was online "
            f"on the server (divergent readiness reads={proxy.stale_reads}).\n"
            f"stderr:\n{err}"
        )
        assert _pid_alive(daemon_pid), (
            f"the registered daemon (pid {daemon_pid}) was torn down by "
            "omnigent host --background despite being online on the server."
        )
        assert _read_daemon_record(daemons_dir) is not None, (
            "the registered daemon's registry record was removed -- the CLI "
            "tore it down instead of leaving it running."
        )
    finally:
        if proc is not None and proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=5)
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        if daemon_pid is not None and _pid_alive(daemon_pid):
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.kill(daemon_pid, signal.SIGKILL)
        proxy.close()

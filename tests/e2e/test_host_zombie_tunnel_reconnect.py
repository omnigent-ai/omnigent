"""A host whose tunnel peer only answers protocol pings must still reconnect.

A front door (a load balancer or proxy) can end the backend request for the
host tunnel — the server deregisters the host and marks it offline — while it
keeps the host-facing TCP/WebSocket leg open and answers WebSocket protocol
PINGs. Nothing but the server's 30 s application pings then tells the daemon
its registration is gone, so it must notice their absence and reconnect
instead of sitting in ``ws.recv()`` while the server returns 409 for it.

The test drives the real ``omnigent host`` command through a WebSocket-aware
stand-in front door (:mod:`tests.e2e._zombie_tunnel_proxy`) in front of a real
local server, severs the server-side leg, and expects the daemon to be back
online within a minute. A control case closes both legs and shows the
ordinary reconnect path.

Run with::

    .venv/bin/python -m pytest tests/e2e/test_host_zombie_tunnel_reconnect.py -v --timeout=600
"""

from __future__ import annotations

import logging
import re
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
import yaml

from omnigent.process_logging import PROCESS_LOG_FILE_ENV_VAR
from omnigent.util.tunnel_limits import (
    TUNNEL_KEEPALIVE_PING_INTERVAL_S,
    TUNNEL_KEEPALIVE_PING_TIMEOUT_S,
)
from tests._helpers.live_server import isolated_local_server, local_server_env
from tests.e2e._zombie_tunnel_proxy import ZombieTunnelProxy

_REPO_ROOT = Path(__file__).resolve().parents[2]
_logger = logging.getLogger(__name__)

# The report's acceptance bar: an otherwise healthy host is back within a minute.
RECONNECT_DEADLINE_S = 60.0
# Beyond the whole protocol keepalive budget, so a reconnect that never comes
# cannot be blamed on a ping still in flight.
WATCH_WINDOW_S = TUNNEL_KEEPALIVE_PING_INTERVAL_S + TUNNEL_KEEPALIVE_PING_TIMEOUT_S + 30.0

_DISCONNECT_RE = re.compile(r"Host tunnel disconnected")
_CONNECTED_MARK = "✓ Connected as"

pytestmark = pytest.mark.timeout(600)


@dataclass
class SpawnedHost:
    proc: subprocess.Popen[bytes]
    host_id: str
    name: str
    console_log: Path
    process_log: Path

    def console(self) -> str:
        return self.console_log.read_text(errors="replace") if self.console_log.exists() else ""

    def log(self) -> str:
        return self.process_log.read_text(errors="replace") if self.process_log.exists() else ""

    def disconnects(self) -> int:
        return len(_DISCONNECT_RE.findall(self.log()))

    def connects(self) -> int:
        return self.console().count(_CONNECTED_MARK)

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGINT)
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)


def host_env(home: Path, process_log: Path) -> dict[str, str]:
    """Environment for a host daemon and CLI isolated from this machine's state."""
    env = local_server_env({"HOME": str(home), PROCESS_LOG_FILE_ENV_VAR: str(process_log)})
    for name in list(env):
        if name.startswith(("OMNIGENT_HOST_", "OMNIGENT_RUNNER_")) or name in (
            "RUNNER_SERVER_URL",
            "OMNIGENT_REMOTE_AUTH_TOKEN",
        ):
            env.pop(name)
    return env


def spawn_host(tmp_path: Path, server_url: str) -> SpawnedHost:
    """Run the real ``omnigent host --server`` command with a fresh host identity."""
    home = tmp_path / "home"
    (home / ".omnigent").mkdir(parents=True)
    host_id = uuid.uuid4().hex
    name = f"zombie-tunnel-{host_id[:8]}"
    (home / ".omnigent" / "config.yaml").write_text(
        yaml.safe_dump({"host": {"host_id": host_id, "name": name}}, sort_keys=True)
    )
    console_log = tmp_path / "host-console.log"
    process_log = tmp_path / "host-daemon.log"
    env = host_env(home, process_log)
    with console_log.open("wb") as out:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "omnigent",
                "host",
                "--server",
                server_url,
                "--non-interactive",
                "--no-open",
            ],
            env=env,
            cwd=str(_REPO_ROOT),
            stdout=out,
            stderr=subprocess.STDOUT,
        )
    return SpawnedHost(proc, host_id, name, console_log, process_log)


def host_status(client: httpx.Client, host_id: str) -> str | None:
    response = client.get("/v1/hosts")
    response.raise_for_status()
    for row in response.json()["hosts"]:
        if row["host_id"] == host_id:
            return row["status"]
    return None


def wait_until(check: Callable[[], bool], *, timeout: float, what: str) -> float:
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        if check():
            return time.monotonic() - started
        time.sleep(0.5)
    raise AssertionError(f"timed out after {timeout:.0f}s waiting for {what}")


def wait_online(client: httpx.Client, host: SpawnedHost, timeout: float = 90.0) -> float:
    def online() -> bool:
        assert host.proc.poll() is None, (
            f"host daemon exited with {host.proc.returncode}\n{host.console()}\n{host.log()}"
        )
        return host_status(client, host.host_id) == "online"

    return wait_until(online, timeout=timeout, what=f"host {host.name} to be online")


def _describe(host: SpawnedHost, status: str | None, elapsed: float) -> str:
    alive = host.proc.poll() is None
    return (
        f"{elapsed:.0f}s after the sever: server status={status!r}, daemon alive={alive}, "
        f"'{_CONNECTED_MARK}' lines={host.connects()}, "
        f"'Host tunnel disconnected' lines={host.disconnects()}\n"
        f"--- host console ---\n{host.console()[-2000:]}\n"
        f"--- host log tail ---\n{host.log()[-3000:]}"
    )


def _server_dir(root: Path) -> Path:
    server_dir = root / "server"
    server_dir.mkdir(parents=True, exist_ok=True)
    return server_dir


def _run_sever_case(tmp_path: Path, *, zombie: bool, deadline_s: float) -> None:
    with (
        isolated_local_server(_server_dir(tmp_path)) as server_url,
        ZombieTunnelProxy(server_url) as front_door,
        httpx.Client(base_url=server_url, trust_env=False, timeout=10.0) as client,
    ):
        host = spawn_host(tmp_path, front_door.url)
        try:
            wait_online(client, host)
            front_door.wait_for_tunnel()
            wait_until(lambda: host.connects() >= 1, timeout=30, what="the ✓ Connected line")
            connects_before = host.connects()
            disconnects_before = host.disconnects()

            assert front_door.sever(zombie=zombie) == 1
            severed_at = time.monotonic()
            if zombie:
                # The server ran its disconnect path while the host heard
                # nothing: it lists the host offline and refuses host-bound
                # requests (a clean close reconnects too fast to observe this).
                wait_until(
                    lambda: host_status(client, host.host_id) == "offline",
                    timeout=30,
                    what="the server to list the host offline",
                )
                offline_probe = client.get(f"/v1/hosts/{host.host_id}/filesystem")
                _logger.info(
                    "host-bound probe after sever: GET /v1/hosts/%s/filesystem -> HTTP %s %s",
                    host.host_id,
                    offline_probe.status_code,
                    offline_probe.text[:200],
                )
                assert offline_probe.status_code == 409, offline_probe.text

            def recovered() -> bool:
                return (
                    host_status(client, host.host_id) == "online"
                    and host.connects() > connects_before
                )

            try:
                wait_until(
                    recovered,
                    timeout=max(0.0, deadline_s - (time.monotonic() - severed_at)),
                    what="the host to notice and reconnect",
                )
            except AssertionError as exc:
                status = host_status(client, host.host_id)
                cut = "host leg still answering pings" if zombie else "clean close"
                raise AssertionError(
                    "host never reconnected after its tunnel peer stopped sending "
                    f"application frames ({cut})\n"
                    f"{_describe(host, status, time.monotonic() - severed_at)}"
                ) from exc
            elapsed = time.monotonic() - severed_at
            assert elapsed <= RECONNECT_DEADLINE_S, (
                f"host reconnected only after {elapsed:.0f}s (> {RECONNECT_DEADLINE_S:.0f}s)\n"
                f"{_describe(host, 'online', elapsed)}"
            )
            assert host.disconnects() > disconnects_before, (
                f"host reconnected without logging the tunnel drop\n"
                f"{_describe(host, 'online', elapsed)}"
            )
            if zombie:
                # The recovery must come from the silence watchdog, not from the
                # front door closing the host leg and the ordinary reconnect path.
                log = host.log()
                assert "No frame from the server for" in log, _describe(host, "online", elapsed)
                assert "server went silent — prompt reconnect" in log, _describe(
                    host, "online", elapsed
                )
            assert host.proc.poll() is None, "host daemon exited instead of reconnecting"
        finally:
            host.stop()


def test_host_reconnects_when_tunnel_peer_only_answers_protocol_pings(tmp_path: Path) -> None:
    _run_sever_case(tmp_path, zombie=True, deadline_s=WATCH_WINDOW_S)


def test_host_reconnects_after_clean_tunnel_close(tmp_path: Path) -> None:
    _run_sever_case(tmp_path, zombie=False, deadline_s=RECONNECT_DEADLINE_S)

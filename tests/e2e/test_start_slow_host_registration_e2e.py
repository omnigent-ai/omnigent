"""`omnigent start` when the host tunnel needs longer than the registration wait."""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import yaml

from tests._helpers.live_server import isolated_local_server, isolated_process_env
from tests._helpers.polling import poll_until
from tests.e2e.resilience.lab.events import EventLog
from tests.e2e.resilience.lab.proxy import Fault, FaultProxy, LoopThread, host_link

_TUNNEL_STALL_S = 40.0
_START_TIMEOUT_S = 150.0
_ONLINE_AFTER_START_S = 75.0


@pytest.fixture
def stalled_tunnel_server(tmp_path: Path) -> Iterator[tuple[str, FaultProxy]]:
    with isolated_local_server(tmp_path / "server") as base_url:
        loop = LoopThread("slow-host-registration")
        loop.start()
        upstream_port = int(base_url.rsplit(":", 1)[1])
        proxy = FaultProxy(
            "host",
            ("127.0.0.1", upstream_port),
            loop=loop,
            events=EventLog(tmp_path / "network.jsonl"),
            classify=host_link,
        )
        proxy.start()
        try:
            yield base_url, proxy
        finally:
            proxy.stop()
            loop.stop()


def _clear_after_first_tunnel_attempt(
    proxy: FaultProxy, fault: Fault, stop: threading.Event
) -> None:
    while not stop.is_set() and not proxy.connections({"host.tunnel"}):
        time.sleep(0.1)
    stop.wait(_TUNNEL_STALL_S)
    fault.clear()


def _host_status(client: httpx.Client, host_id: str) -> str:
    try:
        response = client.get(f"/v1/hosts/{host_id}", timeout=3)
    except httpx.HTTPError as exc:
        return type(exc).__name__
    if response.status_code != 200:
        return f"HTTP {response.status_code}"
    return str(response.json().get("status"))


@pytest.mark.timeout(400)
def test_start_brings_host_online_when_registration_outlasts_wait(
    tmp_path: Path, stalled_tunnel_server: tuple[str, FaultProxy]
) -> None:
    base_url, proxy = stalled_tunnel_server
    home = tmp_path / "cli-home"
    config_home = home / ".omnigent"
    config_home.mkdir(parents=True)
    (config_home / "config.yaml").write_text(f"server: {proxy.url}\n")
    env = isolated_process_env(home, {"OMNIGENT_CONFIG_HOME": str(config_home)}, None)

    fault = proxy.blackhole({"host.tunnel"})
    stop_stall = threading.Event()
    stall = threading.Thread(
        target=_clear_after_first_tunnel_attempt, args=(proxy, fault, stop_stall), daemon=True
    )
    stall.start()
    try:
        start = subprocess.run(
            [sys.executable, "-m", "omnigent", "start", "--non-interactive"],
            env=env,
            cwd=str(home),
            capture_output=True,
            text=True,
            timeout=_START_TIMEOUT_S,
            check=False,
        )
        output = start.stdout + start.stderr
        host_id = (
            yaml.safe_load((config_home / "config.yaml").read_text()).get("host") or {}
        ).get("host_id")
        assert host_id, f"`omnigent start` created no host identity:\n{output}"
        assert start.returncode != 0 and "still running (pid" in output, (
            "`omnigent start` should fail while the tunnel is held and leave the daemon "
            f"running (exit {start.returncode}):\n{output}"
        )
        with httpx.Client(base_url=base_url, trust_env=False) as client:
            observed: list[str] = []

            def _online() -> bool:
                observed.append(_host_status(client, host_id))
                return observed[-1] == "online"

            try:
                poll_until(_online, timeout_s=_ONLINE_AFTER_START_S, interval_s=1.0)
            except AssertionError:
                pytest.fail(
                    f"Host never came online after `omnigent start` exited {start.returncode} "
                    f"with the tunnel held {_TUNNEL_STALL_S:.0f}s; server statuses "
                    f"{sorted(set(observed))}.\n--- omnigent start output ---\n{output}"
                )
    finally:
        stop_stall.set()
        stall.join(timeout=10)
        subprocess.run(
            [sys.executable, "-m", "omnigent", "stop", "--force"],
            env=env,
            cwd=str(home),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
            check=False,
        )

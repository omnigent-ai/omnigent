"""Owned real host processes; callers retain provider setup and fault injection."""

from __future__ import annotations

import subprocess
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import yaml

from omnigent.process_logging import PROCESS_LOG_FILE_ENV_VAR
from tests._helpers.compat import apply_runner_env, compat_runner_cwd, runner_executable
from tests._helpers.live_server import isolated_process_env, terminate_process


def _tunnel_probe_status(client: httpx.Client | ModuleType, host_id: str, base_url: str) -> int:
    """The status of a one-entry filesystem listing routed through *host_id*'s tunnel."""
    url = f"{base_url}/v1/hosts/{host_id}/filesystem"
    return client.get(url, params={"limit": 1}, timeout=10).status_code


def host_tunnel_registered(
    client: httpx.Client | ModuleType, host_id: str, *, base_url: str = ""
) -> bool:
    """Whether a host request reaches *host_id* through the server's live tunnel.

    The hosts table reports "online" before the tunnel registers, and creating a
    session on the host in that window fails with ``wrong_replica``. Pass
    *base_url* when *client* has none (e.g. the ``httpx`` module).
    """
    return _tunnel_probe_status(client, host_id, base_url) == 200


def _find_routable_host(
    client: httpx.Client | ModuleType,
    *,
    base_url: str,
    host_id: str | None,
    name: str | None,
    owner: str | None,
) -> tuple[dict[str, Any] | None, str]:
    """The first matching online host whose tunnel answers, else ``None`` and why not."""
    response = client.get(f"{base_url}/v1/hosts", timeout=10)
    if response.status_code != 200:
        return None, f"GET /v1/hosts returned {response.status_code}"
    wanted = {"host_id": host_id, "name": name, "owner": owner}
    reason = "no matching host online"
    for row in response.json().get("hosts", []):
        if row.get("status") != "online":
            continue
        if any(value is not None and row.get(key) != value for key, value in wanted.items()):
            continue
        try:
            status = _tunnel_probe_status(client, str(row["host_id"]), base_url)
        except httpx.TransportError as exc:
            # One host's dead tunnel mustn't stop a later matching host from counting.
            failure = type(exc).__name__
            reason = f"host {row['host_id']} online but its tunnel probe raised {failure}"
            continue
        if status == 200:
            return row, ""
        reason = f"host {row['host_id']} online but its tunnel probe returned {status}"
    return None, reason


def routable_host(
    client: httpx.Client | ModuleType,
    *,
    base_url: str = "",
    host_id: str | None = None,
    name: str | None = None,
    owner: str | None = None,
) -> dict[str, Any] | None:
    """Return the first online host row matching the filters whose tunnel answers.

    Rows are filtered by whichever of *host_id*, *name* and *owner* are given.
    Returns ``None`` while no match is online and registered, including when a
    request answers non-200; a transport error on the hosts list propagates.
    """
    row, _ = _find_routable_host(
        client, base_url=base_url, host_id=host_id, name=name, owner=owner
    )
    return row


def wait_for_routable_host(
    client: httpx.Client | ModuleType,
    *,
    timeout: float,
    base_url: str = "",
    host_id: str | None = None,
    name: str | None = None,
    owner: str | None = None,
    poll_interval: float = 0.5,
) -> dict[str, Any]:
    """Poll :func:`routable_host` until it returns a row, retrying transport errors."""
    deadline = time.monotonic() + timeout
    while True:  # Always poll once, so a timeout names a real failure.
        try:
            row, last = _find_routable_host(
                client, base_url=base_url, host_id=host_id, name=name, owner=owner
            )
            if row is not None:
                return row
        except httpx.TransportError as exc:
            last = f"{type(exc).__name__}: {exc}"
        if time.monotonic() >= deadline:
            break
        time.sleep(poll_interval)
    wanted = {"host_id": host_id, "name": name, "owner": owner}
    filters = ", ".join(f"{key}={value!r}" for key, value in wanted.items() if value is not None)
    raise AssertionError(f"No host ({filters or 'any'}) became routable within {timeout}s: {last}")


@dataclass
class Host:
    proc: subprocess.Popen[bytes]
    host_id: str
    home: Path
    log_path: Path

    def wait_online(
        self, client: httpx.Client, *, timeout: float = 30, poll_interval: float = 0.5
    ) -> dict[str, Any]:
        """Read this host's real tunnel registration, retaining startup diagnostics."""
        deadline = time.monotonic() + timeout
        last = "not polled"
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                break
            try:
                response = client.get("/v1/hosts", timeout=2)
                last = f"HTTP {response.status_code}"
                if response.status_code == 200:
                    for row in response.json().get("hosts", []):
                        if row.get("host_id") == self.host_id and row.get("status") == "online":
                            if host_tunnel_registered(client, self.host_id):
                                return row
                            last = "online in the hosts table; tunnel not yet registered"
            except httpx.HTTPError as exc:
                last = str(exc)
            time.sleep(poll_interval)
        raise AssertionError(
            f"Host {self.host_id} never came online ({last}; exit={self.proc.poll()}).\n"
            f"{self.log_path.read_text(errors='replace')[-4000:]}"
        )


@contextmanager
def isolated_host(
    root: Path,
    base_url: str,
    *,
    env: Mapping[str, str | None] | None = None,
    base_env: Mapping[str, str] | None = None,
    config: Mapping[str, Any] | None = None,
    host_id: str | None = None,
    name: str | None = None,
    command: Sequence[str] | None = None,
    cwd: Path | None = None,
    ready: Callable[[Host], object] | None = None,
) -> Iterator[Host]:
    """Start a compat-aware daemon with owned HOME/config/data and a unique identity.

    ``command`` can preserve a CLI entry point or install a caller's fault.
    Use a fresh root for each host; existing identity files are rejected.
    ``config`` seeds non-identity settings before startup.
    Readiness is explicit: pass a callback or call wait_online with an authenticated
    client. Cleanup also runs when the callback or the test raises.
    """
    if config is not None and "host" in config:
        raise ValueError("Set host identity through host_id and name")
    home = root / "home"
    overrides = dict(env or {})
    owned = {
        "OMNIGENT_CONFIG_HOME",
        "OMNIGENT_HOST_ID",
        "OMNIGENT_HOST_NAME",
        PROCESS_LOG_FILE_ENV_VAR,
    }
    if owned & overrides.keys():
        raise ValueError(f"Host owns these environment keys: {sorted(owned & overrides.keys())}")
    process_env = isolated_process_env(home, overrides, base_env)
    home.mkdir(parents=True, exist_ok=True)
    config_dir = home / ".omnigent"
    config_dir.mkdir(exist_ok=True)
    host_id = host_id or uuid.uuid4().hex
    name = name or f"e2e-host-{host_id[:12]}"
    with (config_dir / "config.yaml").open("x") as identity:
        yaml.safe_dump({**(config or {}), "host": {"host_id": host_id, "name": name}}, identity)
    log_path = root / "host.log"
    process_env.update(
        OMNIGENT_CONFIG_HOME=str(config_dir), OMNIGENT_HOST_ID=host_id, OMNIGENT_HOST_NAME=name
    )
    process_env[PROCESS_LOG_FILE_ENV_VAR] = str(log_path)
    argv = (
        list(command)
        if command is not None
        else [runner_executable(), "-m", "omnigent.host._daemon_entry", "--server", base_url]
    )
    # Both stdio and the child logger append; independent offsets must not overwrite.
    log_path.write_bytes(b"")
    with log_path.open("ab") as log:
        proc = subprocess.Popen(
            argv,
            env=apply_runner_env(process_env),
            cwd=cwd if cwd is not None else compat_runner_cwd(),
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            host = Host(proc, host_id, home, log_path)
            if ready is not None:
                ready(host)
            yield host
        finally:
            terminate_process(proc)

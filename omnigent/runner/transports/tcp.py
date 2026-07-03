"""TCP transport for the runner (Phase 3).

Per ``designs/RUNNER.md`` Phase 3, the runner accepts a
``--bind <host:port>`` flag for a TCP listener; the server uses
``httpx.AsyncHTTPTransport()`` against a TCP base URL.

Compared to UDS (Phase 2), the only delta is uvicorn's bind flag
and the httpx client's base URL — httpx handles TCP natively. This
module ships the TCP equivalents of :class:`RunnerSubprocess` and
``create_uds_client`` so callers have a uniform shape across phases.
"""

from __future__ import annotations

import contextlib
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass

import httpx

from omnigent.inner import _proc


def _is_tcp_listening(host: str, port: int) -> bool:
    """Connect-test on a TCP host:port. Returns True iff something accepts."""
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except (ConnectionRefusedError, OSError):
        return False


def _pick_free_port() -> int:
    """Bind 127.0.0.1:0 to let the OS pick a free port; close and return it.

    Standard "port allocation" trick — the OS guarantees the port
    is free at the moment of bind. There's a TOCTOU window before
    uvicorn re-binds it, but for tests on localhost this is fine
    (no other process is racing for the same port in the millisecond
    between our close and uvicorn's bind).
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@dataclass
class RunnerTCPSubprocess:
    """Context manager spawning uvicorn against a runner app on TCP.

    :param app_factory_path: Module-and-attribute path uvicorn imports
        for the FastAPI app, e.g.
        ``"omnigent.runner.app:create_runner_app_from_env"``.
    :param host: Bind host. Default ``"127.0.0.1"`` keeps the runner
        loopback-only, which is the safe default for in-network
        operator-deployed runners — externally-reachable runners
        belong behind a real reverse proxy (TLS, auth, etc.) that
        v1 doesn't ship.
    :param port: Bind port. ``0`` (default) asks the OS for a free
        port, which the manager assigns to ``self.port`` after start.
    :param startup_timeout_s: Max wait for the subprocess to be
        listening before raising. Default 30 s: the factory
        cold-imports ``omnigent.runner.app`` (and the whole runtime),
        which can take well over 10 s under parallel CI load.
    :param extra_env: Optional extra environment variables merged
        into the subprocess environment, e.g.
        ``{"RUNNER_SERVER_URL": "http://127.0.0.1:6767"}``.
        Values here override the inherited environment.
    """

    app_factory_path: str = "omnigent.runner.app:create_runner_app_from_env"
    host: str = "127.0.0.1"
    port: int = 0
    startup_timeout_s: float = 30.0
    extra_env: dict[str, str] | None = None
    _process: subprocess.Popen[bytes] | None = None

    def __enter__(self) -> RunnerTCPSubprocess:
        if self.port == 0:
            self.port = _pick_free_port()
        cmd = [
            sys.executable,
            "-m",
            "uvicorn",
            "--factory",
            self.app_factory_path,
            "--host",
            self.host,
            "--port",
            str(self.port),
            "--log-level",
            "warning",
        ]
        env: dict[str, str] | None = None
        if self.extra_env:
            env = {**os.environ, **self.extra_env}
        self._process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            **_proc.spawn_kwargs(),
        )
        deadline = time.time() + self.startup_timeout_s
        while time.time() < deadline:
            if self._process.poll() is not None:
                _, stderr = self._process.communicate()
                raise RuntimeError(
                    f"runner TCP subprocess exited prematurely (rc="
                    f"{self._process.returncode}); stderr: "
                    f"{stderr.decode(errors='replace')[:500]}"
                )
            if _is_tcp_listening(self.host, self.port):
                return self
            time.sleep(0.05)
        self._kill()
        raise TimeoutError(
            f"runner TCP subprocess didn't bind {self.host}:{self.port} "
            f"within {self.startup_timeout_s}s"
        )

    def __exit__(self, *exc_info: object) -> None:
        self._kill()

    @property
    def base_url(self) -> str:
        """The base URL httpx clients should use to reach this subprocess."""
        return f"http://{self.host}:{self.port}"

    def _kill(self) -> None:
        if self._process is None or self._process.poll() is not None:
            return
        _proc.terminate_tree(self._process, grace=5)
        if self._process.poll() is None:
            _proc.kill_tree(self._process)
            with contextlib.suppress(subprocess.TimeoutExpired):
                self._process.wait(timeout=2)


def create_tcp_client(
    base_url: str,
    *,
    auth_headers: dict[str, str] | None = None,
) -> httpx.AsyncClient:
    """Build an httpx AsyncClient pointed at a TCP runner.

    No special transport — httpx handles TCP natively. The factory
    exists for symmetry with :func:`create_uds_client` and the
    in-process ASGI factory, so call sites can pick a transport via
    a single switch in their config.

    :param base_url: e.g. ``"http://127.0.0.1:8080"``.
    :param auth_headers: Optional default headers (e.g.
        ``{"Authorization": "Bearer <token>"}``).  Injected into
        every request so the runner auth middleware accepts
        the call.
    """
    return httpx.AsyncClient(base_url=base_url, headers=auth_headers or {})

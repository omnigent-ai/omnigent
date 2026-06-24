"""Unix Domain Socket transport for the runner (Phase 2).

Per ``designs/RUNNER.md`` Phase 2, the runner becomes a separate
subprocess running uvicorn bound to a Unix socket; the server's
transport changes to ``httpx.AsyncHTTPTransport(uds="/tmp/...")``.

This module ships:
- ``RunnerSubprocess``: context manager that spawns uvicorn against
  a runner FastAPI app on a UDS, waits for it to be healthy, and
  tears it down cleanly.
- ``create_uds_client()``: httpx factory pointed at a UDS path.

Together they let the server speak the harness contract to a runner
process via real bytes on the wire — no in-memory shortcut.

The CLI integration that wires `omnigent run` to spawn both a
server subprocess AND a runner subprocess on a per-process UDS
(per RUNNER.md §7 Phase 2 "CLI integration") is a separate piece in
the CLI module and not implemented in this session — see RUNNER.md
§11 autonomous decisions log.
"""

from __future__ import annotations

import contextlib
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import httpx


def _is_socket_listening(socket_path: str) -> bool:
    """Connect-test on a UDS path to detect whether uvicorn is up.

    Returns True if a Unix socket exists at ``socket_path`` and
    accepts connections (i.e. uvicorn is listening). False if the
    file doesn't exist or connect fails. Used by the polling loop
    in :class:`RunnerSubprocess` to wait for the subprocess to be
    healthy before yielding control to the caller.
    """
    if not os.path.exists(socket_path):
        return False
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(0.5)
        s.connect(socket_path)
        s.close()
        return True
    except (FileNotFoundError, ConnectionRefusedError, OSError):
        return False


@dataclass
class RunnerSubprocess:
    """Context manager spawning uvicorn against a runner app on a UDS.

    Used by tests + Phase 2's `omnigent run` to ship the runner as
    a separate process while keeping the server↔runner wire local.

    :param app_factory_path: Module-and-attribute path uvicorn will
        import to get the FastAPI app, e.g.
        ``"omnigent.runner.app:create_runner_app_from_env"``. Uvicorn's
        ``--factory`` flag handles the call-the-factory pattern.
    :param socket_path: UDS path to bind. If ``None`` the manager
        picks a unique path in a tmp dir.
    :param startup_timeout_s: Max wait for the subprocess to be
        listening on the socket before raising. Default 30 s: the
        factory cold-imports ``omnigent.runner.app`` (and the whole
        runtime), which can take well over 10 s under parallel CI load.
    :param extra_env: Optional extra environment variables merged
        into the subprocess environment, e.g.
        ``{"RUNNER_SERVER_URL": "http://127.0.0.1:6767"}``.
        Values here override the inherited environment.
    """

    app_factory_path: str = "omnigent.runner.app:create_runner_app_from_env"
    socket_path: str | None = None
    startup_timeout_s: float = 30.0
    extra_env: dict[str, str] | None = None
    _process: subprocess.Popen[bytes] | None = None
    _tmp_dir: tempfile.TemporaryDirectory | None = None

    def __enter__(self) -> RunnerSubprocess:
        if self.socket_path is None:
            # Allocate a unique tmp dir + socket path.
            self._tmp_dir = tempfile.TemporaryDirectory(prefix="omnigent-runner-")
            self.socket_path = str(Path(self._tmp_dir.name) / "runner.sock")
        # Spawn uvicorn with --uds. Use the same Python the test
        # session is running so the venv's installed packages
        # (including the omnigent source) are importable.
        cmd = [
            sys.executable,
            "-m",
            "uvicorn",
            "--factory",
            self.app_factory_path,
            "--uds",
            self.socket_path,
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
            # Detach into its own session so we can SIGTERM the whole
            # group on shutdown — uvicorn spawns child workers we
            # need to clean up too.
            start_new_session=True,
            env=env,
        )
        # Poll until the socket is accepting or we time out.
        deadline = time.time() + self.startup_timeout_s
        while time.time() < deadline:
            if self._process.poll() is not None:
                # Subprocess died; capture stderr for debugging.
                _, stderr = self._process.communicate()
                raise RuntimeError(
                    f"runner subprocess exited prematurely (rc="
                    f"{self._process.returncode}); stderr: "
                    f"{stderr.decode(errors='replace')[:500]}"
                )
            if _is_socket_listening(self.socket_path):
                return self
            time.sleep(0.05)
        # Timed out — kill and report.
        self._kill()
        raise TimeoutError(
            f"runner subprocess didn't bind socket {self.socket_path} "
            f"within {self.startup_timeout_s}s"
        )

    def __exit__(self, *exc_info: object) -> None:
        self._kill()
        if self._tmp_dir is not None:
            self._tmp_dir.cleanup()

    def _kill(self) -> None:
        if self._process is None:
            return
        if self._process.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(os.getpgid(self._process.pid), signal.SIGTERM)
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(os.getpgid(self._process.pid), signal.SIGKILL)
                self._process.wait(timeout=2)


def create_uds_client(socket_path: str, *, base_url: str = "http://runner") -> httpx.AsyncClient:
    """Build an httpx client routing every request through a UDS.

    Pairs with :class:`RunnerSubprocess` on the runner side. The
    server's task workflow can use this with the same HTTP request
    shape as the WebSocket tunnel.

    :param socket_path: Filesystem path of the UDS the runner is
        listening on.
    :param base_url: Cosmetic base URL (host portion is unused for
        UDS; httpx requires a well-formed URL prefix).
    :returns: An ``httpx.AsyncClient`` pointed at the UDS.
    """
    transport = httpx.AsyncHTTPTransport(uds=socket_path)
    return httpx.AsyncClient(transport=transport, base_url=base_url)

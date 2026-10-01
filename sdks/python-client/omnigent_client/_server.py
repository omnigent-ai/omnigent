"""LocalServer — start and manage a local omnigent server."""

from __future__ import annotations

import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import time
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from ._client import OmnigentClient


def _find_free_port() -> int:
    """Find a free TCP port."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LocalServer:
    """Context manager that starts a local omnigent server.

    Usage::

        async with LocalServer(agent_path="./my-agent/") as server:
            client = server.client
            async for event in client.responses.stream(...):
                ...

    :param agent_path: Path to the agent directory or tarball.
        The server pre-registers this agent at startup.
    :param host: Bind address.
    :param port: Port number (0 = auto-assign).
    """

    def __init__(
        self,
        agent_path: str,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        self._agent_path = agent_path
        self._host = host
        self._port = port if port != 0 else _find_free_port()
        self._proc: subprocess.Popen[bytes] | None = None
        self._tmpdir: str | None = None
        self._client: OmnigentClient | None = None

    @property
    def base_url(self) -> str:
        """The server's base URL."""
        return f"http://{self._host}:{self._port}"

    @property
    def client(self) -> OmnigentClient:
        """A pre-configured client connected to this server."""
        if self._client is None:
            raise RuntimeError("Server not started — use 'async with LocalServer(...) as server:'")
        return self._client

    async def __aenter__(self) -> LocalServer:
        self._start()
        # Import here to avoid circular import at module level.
        from ._client import OmnigentClient

        self._client = OmnigentClient(base_url=self.base_url)
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._stop()
        if self._client is not None:
            await self._client.close()
            self._client = None

    def _start(self) -> None:
        """Launch the server subprocess."""
        self._tmpdir = tempfile.mkdtemp(prefix="omnigent-client-")
        db_uri = f"sqlite:///{self._tmpdir}/chat.db"
        art_loc = f"{self._tmpdir}/artifacts"

        # Resolve the omnigent project root so Alembic can find
        # its migrations directory. Walk up from the agent path or
        # from this file's location looking for omnigent/cli/commands.py.
        project_root = self._find_project_root()

        self._proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "omnigent.cli.commands",
                "server",
                "--host",
                self._host,
                "--port",
                str(self._port),
                "--database-uri",
                db_uri,
                "--artifact-location",
                art_loc,
                "--agent",
                os.path.abspath(self._agent_path),
            ],
            env={**os.environ},
            cwd=project_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        self._wait_for_ready()

    def _find_project_root(self) -> str:
        """Find the omnigent project root directory.

        Walks up from the agent path looking for a directory that
        contains ``omnigent/cli/commands.py``.
        """
        # Try from agent path first.
        candidate = pathlib.Path(self._agent_path).resolve()
        for parent in [candidate, *list(candidate.parents)]:
            if (parent / "omnigent" / "cli.py").exists():
                return str(parent)

        # Try from this file's location (inside frontends/clients/python/).
        candidate = pathlib.Path(__file__).resolve()
        for parent in candidate.parents:
            if (parent / "omnigent" / "cli.py").exists():
                return str(parent)

        # Fallback to cwd.
        return os.getcwd()

    def _wait_for_ready(self, timeout: float = 15.0) -> None:
        """Poll until the server responds."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                out = (
                    self._proc.stdout.read().decode(errors="replace") if self._proc.stdout else ""
                )
                raise RuntimeError(
                    f"Server exited with code {self._proc.returncode}.\n{out[-3000:]}"
                )
            try:
                resp = httpx.get(f"{self.base_url}/health", timeout=2.0)
                if resp.status_code == 200:
                    return
            except httpx.ConnectError:
                pass
            time.sleep(0.5)
        raise RuntimeError(f"Server did not start within {timeout}s")

    def _stop(self) -> None:
        """Stop the server subprocess."""
        if self._proc is None:
            return
        self._proc.send_signal(signal.SIGINT)
        try:
            self._proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._proc.kill()
        self._proc = None

"""An isolated real server and explicitly started runner for local regressions."""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from pathlib import Path

import httpx

from omnigent.runner.identity import token_bound_runner_id
from omnigent.testing.process_reaper import reap_leaked_omnigent_processes
from tests._helpers.live_server import find_free_port, local_server_env, terminate_process


def _process_env(home: Path, overrides: Mapping[str, str]) -> dict[str, str]:
    if {"HOME", "OMNIGENT_DATA_DIR"} & overrides.keys():
        raise ValueError("HOME and OMNIGENT_DATA_DIR are owned by the isolated stack")
    env = local_server_env({})
    # A parent runner's identity, zygote FDs and logging paths cannot belong to
    # this fresh runtime. Tests opt into their required settings explicitly.
    for name in list(env):
        if name.startswith("OMNIGENT") or name == "RUNNER_SERVER_URL":
            env.pop(name)
    env.update(
        HOME=str(home),
        OMNIGENT_DATA_DIR=str(home / ".omnigent"),
        OMNIGENT_AUTH_PROVIDER="header",
        OMNIGENT_LOCAL_SINGLE_USER="1",
        OMNIGENT_DISABLE_CATALOG_LOOKUP="1",
    )
    env.update(overrides)
    return env


class ServerRunner:
    """Own process handles; leave sessions and fault injection to the caller."""

    def __init__(
        self,
        root: Path,
        resources: ExitStack,
        *,
        server_bootstrap: str | None,
        server_env: Mapping[str, str],
        server_cwd: Path | None,
        workspace: Path | None,
        binding_token: str,
        health_timeout: float,
        poll_interval: float,
    ) -> None:
        self.root = root
        self.workspace = workspace if workspace is not None else root / "workspace"
        self.runner_home = root / "home"
        self.server_home = root / "server-home"
        for path in (self.workspace, self.runner_home, self.server_home):
            path.mkdir(parents=True, exist_ok=True)
        self.port = find_free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.database_uri = f"sqlite:///{root / 'chat.db'}"
        self.runner_id = token_bound_runner_id(binding_token)
        self._token = binding_token
        self._resources = resources
        self._client = resources.enter_context(httpx.Client(trust_env=False))
        self._health_timeout = health_timeout
        self._poll_interval = poll_interval
        self._server_bootstrap = server_bootstrap
        self._server_env = dict(server_env)
        self._server_cwd = server_cwd
        self.server: subprocess.Popen[bytes] | None = None
        self.runner: subprocess.Popen[bytes] | None = None

    def _spawn(
        self,
        name: str,
        args: list[str],
        home: Path,
        env: Mapping[str, str],
        *,
        cwd: Path | None = None,
    ) -> subprocess.Popen[bytes]:
        log = self._resources.enter_context(self.log_path(name).open("ab"))
        proc = subprocess.Popen(
            [sys.executable, *args],
            env=_process_env(home, env),
            cwd=cwd,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        self._resources.callback(terminate_process, proc)
        return proc

    def log_path(self, name: str) -> Path:
        """Return the captured output path for the server or runner process."""
        return self.root / f"{name}.log"

    def log_tail(self) -> str:
        """Include captured output and rolling process logs in startup failures."""
        paths = set(self.root.glob("*.log"))
        for home in (self.runner_home, self.server_home):
            paths.update((home / ".omnigent" / "logs").rglob("*.log"))
        tails = []
        for path in sorted(paths):
            try:
                tails.append(
                    f"{path.relative_to(self.root)}:\n{path.read_text(errors='replace')[-3000:]}"
                )
            except OSError:
                continue
        return "\n".join(tails)

    def _wait_ready(self, *, runner: bool = False) -> None:
        url = (
            f"{self.base_url}/v1/runners/{self.runner_id}/status"
            if runner
            else f"{self.base_url}/health"
        )
        deadline = time.monotonic() + self._health_timeout
        last = "not polled"
        while time.monotonic() < deadline:
            for proc in (self.server, self.runner):
                assert proc is None or proc.poll() is None, (
                    f"Process exited with code {proc.returncode} before {url} was ready.\n"
                    f"{self.log_tail()}"
                )
            try:
                if runner:
                    self._client.get(f"{self.base_url}/health", timeout=2.0).raise_for_status()
                response = self._client.get(url, timeout=2.0)
                if response.status_code == 200 and (
                    not runner or response.json().get("online") is True
                ):
                    return
                last = f"HTTP {response.status_code}: {response.text[:300]}"
            except (httpx.HTTPError, ValueError) as exc:
                last = f"{type(exc).__name__}: {exc}"
            time.sleep(self._poll_interval)
        raise AssertionError(f"{url} never became ready: {last}\n{self.log_tail()}")

    def start_server(self, *, wait_ready: bool = True) -> None:
        """Start the server, preserving its database and endpoint on restart."""
        assert self.server is None or self.server.poll() is not None
        args = ["-c", self._server_bootstrap] if self._server_bootstrap else ["-m", "omnigent.cli"]
        self.server = self._spawn(
            "server",
            [
                *args,
                "server",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
                "--database-uri",
                self.database_uri,
                "--artifact-location",
                str(self.root / "artifacts"),
            ],
            self.server_home,
            {"OMNIGENT_RUNNER_TUNNEL_TOKEN": self._token, **self._server_env},
            cwd=self._server_cwd,
        )
        if wait_ready:
            self._wait_ready()

    def restart_server(self) -> None:
        """Clear in-memory server state and await the existing runner's reconnect."""
        terminate_process(self.server)
        self.start_server()
        if self.runner is not None:
            self._wait_ready(runner=True)

    def start_runner(
        self,
        *,
        bootstrap: str | None = None,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        python_args: Sequence[str] = (),
        wait_ready: bool = True,
    ) -> None:
        """Start the runner; callers may observe startup themselves with wait_ready=False."""
        assert self.runner is None, "runner already started"
        args = ["-c", bootstrap] if bootstrap else ["-m", "omnigent.runner._entry"]
        self.runner = self._spawn(
            "runner",
            [*python_args, *args],
            self.runner_home,
            {
                "OMNIGENT_RUNNER_ID": self.runner_id,
                "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": self._token,
                "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
                "RUNNER_SERVER_URL": self.base_url,
                "OMNIGENT_RUNNER_WORKSPACE": str(self.workspace),
                **(env or {}),
            },
            cwd=cwd,
        )
        if wait_ready:
            self._wait_ready(runner=True)


def _reap(scope: Path) -> None:
    _, survivors = reap_leaked_omnigent_processes(scope)
    assert not survivors, f"Processes survived cleanup for {scope}: {survivors}"


@contextmanager
def server_runner(
    root: Path,
    *,
    server_bootstrap: str | None = None,
    server_env: Mapping[str, str] | None = None,
    server_cwd: Path | None = None,
    workspace: Path | None = None,
    binding_token: str | None = None,
    health_timeout: float = 120.0,
    poll_interval: float = 1.0,
    wait_ready: bool = True,
) -> Iterator[ServerRunner]:
    """Yield a ready server; explicitly call start_runner with scenario overrides.

    Set wait_ready=False to start both processes before waiting for the runner.
    Cleanup applies even when readiness fails. Detached Omnigent descendants
    are attributed only to this stack's directories, never the whole machine.
    """
    with ExitStack() as resources:
        stack = ServerRunner(
            root,
            resources,
            server_bootstrap=server_bootstrap,
            server_env=server_env or {},
            server_cwd=server_cwd,
            workspace=workspace,
            binding_token=binding_token or secrets.token_urlsafe(32),
            health_timeout=health_timeout,
            poll_interval=poll_interval,
        )
        # Register before process callbacks so direct children terminate first.
        for scope in (root, stack.runner_home / ".omnigent", stack.server_home / ".omnigent"):
            resources.callback(_reap, scope)
        stack.start_server(wait_ready=wait_ready)
        yield stack

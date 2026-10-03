"""Boot a real Omnigent topology whose every network link runs through a FaultProxy.

::

    browser / API user ──► proxies.client ──┐
    host daemon ──┐                          ├──► server
    runner ───────┼──────► proxies.host ─────┘
    harness hooks ┘
    Claude Code ─────────► proxies.model ──────► mock model

Harnesses reach the model through ``ANTHROPIC_BASE_URL`` when they honor it
and through ``HTTPS_PROXY`` otherwise (Claude Code managed settings can pin a
corporate gateway); the model proxy terminates those tunnels with a lab CA and
serves only model API paths from the mock, so no scenario traffic leaves the
machine.

``proxies.host`` tags each connection ``host.tunnel``, ``runner.tunnel`` or
``host.http``; ``proxies.client`` tags ``client.sse``, ``client.ws`` or
``client.http``. The lab's own probes (:attr:`Lab.observer`) bypass the
proxies so faults never blind the assertions.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import shutil
import socket
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypeVar

import httpx
import psutil
import yaml

from tests.e2e.resilience.lab.events import EventLog
from tests.e2e.resilience.lab.model import MockModel
from tests.e2e.resilience.lab.processes import (
    ManagedProcess,
    frozen,
    process_tree,
    processes_mentioning,
)
from tests.e2e.resilience.lab.proxy import (
    Fault,
    FaultProxy,
    LoopThread,
    client_link,
    fixed_link,
    host_link,
)
from tests.e2e.resilience.lab.tls import TlsInterceptor

T = TypeVar("T")

_REPO_ROOT = Path(__file__).resolve().parents[4]
_WEB_UI_DIST = _REPO_ROOT / "omnigent" / "server" / "static" / "web-ui"
_CLAUDE_MODEL = json.loads(
    (_REPO_ROOT / "tests" / "server" / "integration" / "repro_models.json").read_text()
)["claude-native"]
_POLICY_MODEL = "_policy_llm_"
_LAUNCHED_RUNNER = re.compile(r"Launched runner \S+ for workspace .*? \(pid=(\d+)\)")
_POLL_S = 0.25
_STARTUP_TIMEOUT_S = 120.0
# Ambient settings that would point lab processes at a developer's real
# server, credentials or a parent Claude Code session.
_STRIP_ENV_PREFIXES = ("OMNIGENT_", "DATABRICKS_", "ANTHROPIC_", "OPENAI_", "CLAUDE_", "CODEX_")
_STRIP_ENV = frozenset(
    {
        "RUNNER_SERVER_URL",
        "CLAUDECODE",
        "LLM_API_KEY",
        "PYTEST_ADDOPTS",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "http_proxy",
        "https_proxy",
        "ALL_PROXY",
        "all_proxy",
    }
)
# Settings host-launched runners and their harnesses also need; the daemon's
# runner allowlist drops unlisted names.
_RUNNER_PASSTHROUGH = (
    "OMNIGENT_LOCAL_SINGLE_USER",
    "OMNIGENT_DISABLE_CATALOG_LOOKUP",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
    "DISABLE_AUTOUPDATER",
    "HTTPS_PROXY",
    "NO_PROXY",
    "no_proxy",
)

LabMode = Literal["host", "runner"]
#: How a stopped server looks to clients: ``ingress`` answers 502 like a load
#: balancer (Databricks Apps); ``direct`` refuses connections like a bare port.
LabFront = Literal["ingress", "direct"]


@dataclass(frozen=True)
class LabConfig:
    """How to build a lab.

    :param mode: ``host`` runs the real ``omnigent host`` daemon, which launches
        runners on demand; ``runner`` starts one runner directly (faster, no host link).
    :param front: How the server looks while it is down; see :data:`LabFront`.
    :param root: Directory for logs, databases and workspaces. Defaults to a new
        short directory under ``/tmp`` (tmux socket paths must stay short).
    """

    mode: LabMode = "host"
    front: LabFront = "ingress"
    root: Path | None = None


@dataclass(frozen=True)
class LabProxies:
    """The three proxied links.

    :param client: Browser and API clients to the server.
    :param host: Host daemon, runner and harness hooks to the server.
    :param model: Harness to the model provider.
    """

    client: FaultProxy
    host: FaultProxy
    model: FaultProxy


def wait_for(predicate: Callable[[], T | None], *, timeout: float, what: str) -> T:
    """Poll *predicate* until it returns something other than ``None``.

    :param predicate: Called repeatedly.
    :param timeout: Seconds to wait.
    :param what: Description for the timeout message, e.g. ``"the host to register"``.
    :returns: The first non-``None`` value.
    :raises TimeoutError: When *timeout* elapses first.
    """
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value is not None:
            return value
        if time.monotonic() >= deadline:
            raise TimeoutError(f"timed out after {timeout}s waiting for {what}")
        time.sleep(_POLL_S)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _write_yaml(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(value, sort_keys=False))


class Lab:
    """A disposable server, model, and host (or runner) wired through fault proxies.

    Use as a context manager, or call :meth:`start` and :meth:`stop`.

    :param config: Topology options.
    """

    def __init__(self, config: LabConfig | None = None) -> None:
        self.config = config or LabConfig()
        root = self.config.root or Path(tempfile.mkdtemp(prefix="rlab-", dir="/tmp"))
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.logs = self.root / "logs"
        self.workspace = self.root / "workspace"
        self.events = EventLog(self.root / "events.jsonl")
        self.host_id = uuid.uuid4().hex
        self.host_name = f"resilience-lab-{self.host_id[:8]}"
        self.runner_id: str | None = None
        self._loop = LoopThread()
        self._server_port = _free_port()
        self._model_port = _free_port()
        self._tunnel_token = secrets.token_urlsafe(32)
        self._tls = TlsInterceptor(self.root / "tls")
        self._processes: list[ManagedProcess] = []
        self._server: ManagedProcess | None = None
        self._host: ManagedProcess | None = None
        self._runner: ManagedProcess | None = None
        self._proxies: LabProxies | None = None
        self._clients: list[httpx.Client] = []
        self.model: MockModel | None = None
        self.client: httpx.Client | None = None
        self.observer: httpx.Client | None = None

    # ── lifecycle ────────────────────────────────────────────────

    def __enter__(self) -> Lab:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    @property
    def proxies(self) -> LabProxies:
        """The client, host and model proxies."""
        if self._proxies is None:
            raise RuntimeError("lab not started")
        return self._proxies

    @property
    def server_url(self) -> str:
        """Direct server URL, bypassing every proxy."""
        return f"http://127.0.0.1:{self._server_port}"

    def start(self) -> Lab:
        """Boot every component and wait until the host or runner is online."""
        try:
            self._start()
        except BaseException:
            self.stop()
            raise
        return self

    def stop(self) -> None:
        """Stop every process, proxy and client; keep :attr:`root` for inspection."""
        for client in self._clients:
            client.close()
        self._clients.clear()
        if self.model is not None:
            self.model.close()
        for process in reversed(self._processes):
            process.stop(timeout=10)
        self._kill_stray_host_processes()
        if self._proxies is not None:
            for proxy in (self._proxies.client, self._proxies.host, self._proxies.model):
                with contextlib.suppress(Exception):
                    proxy.stop()
        self._loop.stop()
        self.events.emit("lab", "stopped")

    def remove(self) -> None:
        """Delete :attr:`root`; call after :meth:`stop` when the run passed."""
        shutil.rmtree(self.root, ignore_errors=True)

    def describe(self) -> str:
        """Human-readable URLs and paths for manual poking."""
        lines = [f"lab root:      {self.root}"]
        if self._proxies is not None:
            lines += [
                f"web / API:     {self._proxies.client.url}   (through proxies.client)",
                f"server direct: {self.server_url}",
                f"host link:     {self._proxies.host.url}",
                f"model link:    {self._proxies.model.url} -> {self._model_url}",
            ]
        lines += [f"events:        {self.events.path}", f"logs:          {self.logs}"]
        return "\n".join(lines)

    # ── faults and process control ───────────────────────────────

    def stop_server(self, *, graceful: bool = True) -> None:
        """Stop the server; clients see 502 (ingress) or a refused port (direct).

        :param graceful: SIGTERM (a deploy) rather than SIGKILL (a crash).
        """
        assert self._server is not None
        self.events.emit("lab", "server_down", graceful=graceful)
        self._server.stop(graceful=graceful)

    def start_server(self) -> None:
        """Start the server again on the same port and database; wait until healthy."""
        assert self._server is not None
        self._server.start()
        self._wait_server_healthy()
        self.events.emit("lab", "server_up")

    @contextlib.contextmanager
    def server_down(self, *, graceful: bool = True) -> Iterator[None]:
        """Keep the server down for the duration of the block.

        :param graceful: SIGTERM (a deploy) rather than SIGKILL (a crash).
        """
        refusals: list[Fault] = []
        if self.config.front == "direct":
            refusals = [self.proxies.client.refuse(), self.proxies.host.refuse()]
        self.stop_server(graceful=graceful)
        try:
            yield
        finally:
            self.start_server()
            for fault in refusals:
                fault.clear()

    def restart_server(self, *, downtime_s: float = 0.0, graceful: bool = True) -> None:
        """Stop the server, wait *downtime_s*, start it again.

        :param downtime_s: Seconds the server stays down, e.g. ``20.0``.
        :param graceful: SIGTERM (a deploy) rather than SIGKILL (a crash).
        """
        with self.server_down(graceful=graceful):
            time.sleep(downtime_s)

    def runner_processes(self) -> list[psutil.Process]:
        """Live runner processes (host-launched or direct)."""
        if self._runner is not None:
            return process_tree([self._runner.pid])[:1] if self._runner.running else []
        assert self._host is not None
        # Host runners are forked from a zygote and share its command line, so
        # identify them by the PIDs the daemon logs at launch.
        log = self.logs / "host-process.log"
        launched = {int(pid) for pid in _LAUNCHED_RUNNER.findall(log.read_text(errors="replace"))}
        return [proc for proc in process_tree([self._host.pid]) if proc.pid in launched]

    def kill_runner(self) -> list[int]:
        """SIGKILL every runner process, as a crash would.

        :returns: The killed PIDs.
        """
        killed = []
        for proc in self.runner_processes():
            with contextlib.suppress(psutil.NoSuchProcess):
                proc.kill()
                killed.append(proc.pid)
        self.events.emit("lab", "kill_runner", pids=killed)
        return killed

    def host_side_processes(self) -> list[psutil.Process]:
        """Every process that lives on the user's machine: daemon, runners, tmux, harnesses."""
        roots = [p.pid for p in (self._host, self._runner) if p is not None and p.running]
        tmux = [proc.pid for proc in processes_mentioning(f"{self._host_tmp}{os.sep}")]
        return process_tree([*roots, *tmux])

    @contextlib.contextmanager
    def sleep_host(self, *, wake_network_delay_s: float = 0.0) -> Iterator[None]:
        """Emulate closing a laptop lid: the network drops and host processes freeze.

        On exit the processes thaw, then the network returns after
        *wake_network_delay_s*. SIGSTOP leaves wall and monotonic clocks in step,
        so suspend detection does not fire; the keepalive path is exercised instead.

        :param wake_network_delay_s: Seconds between thaw and network return.
        """
        proxies = self.proxies
        outage = [proxies.host.blackhole(), proxies.model.blackhole()]
        self.events.emit("lab", "sleep_host")
        try:
            with frozen(self.host_side_processes(), self.events):
                yield
            if wake_network_delay_s:
                time.sleep(wake_network_delay_s)
        finally:
            for fault in outage:
                fault.clear()
            self.events.emit("lab", "wake_host")

    # ── sessions ─────────────────────────────────────────────────

    def create_claude_session(self) -> str:
        """Create a claude-native session the way the web's new-chat flow does.

        :returns: The session id, e.g. ``"conv_abc123"``.
        """
        from tests._helpers.native_session import create_native_session

        assert self.client is not None
        metadata: dict[str, Any] = {"workspace": str(self.workspace)}
        if self.config.mode == "host":
            metadata["host_id"] = self.host_id
        created = create_native_session(
            self.client, self.proxies.client.url, harness="claude", metadata=metadata
        )
        session_id = str(created["session_id"])
        if self.config.mode == "runner":
            assert self.runner_id is not None
            response = self.client.patch(
                f"/v1/sessions/{session_id}", json={"runner_id": self.runner_id}, timeout=60.0
            )
            response.raise_for_status()
        self.events.emit("lab", "session_created", session_id=session_id)
        return session_id

    def script_turn(self, marker: str, responses: list[dict[str, Any]]) -> None:
        """Queue Claude's replies for the next user message containing *marker*.

        Claude Code also sends title and summary requests that quote the user's
        message; requiring the ``Bash`` tool keeps those from consuming the queue.

        :param marker: Unique token the scenario puts in its user message.
        :param responses: Mock replies, e.g. ``[{"text": "done"}]``.
        """
        assert self.model is not None
        self.model.reply(responses, match=marker, required_tools=["Bash"])

    def send_message(self, session_id: str, text: str, *, timeout: float = 90.0) -> httpx.Response:
        """Send *text* as the user, through the client link.

        :param session_id: Target session.
        :param text: Message text.
        :param timeout: Request timeout in seconds.
        :returns: The raw response; callers decide whether a failure is expected.
        """
        assert self.client is not None
        return self.client.post(
            f"/v1/sessions/{session_id}/events",
            json={
                "type": "message",
                "data": {"role": "user", "content": [{"type": "input_text", "text": text}]},
            },
            timeout=timeout,
        )

    def snapshot(self, session_id: str) -> dict[str, Any]:
        """Read the session snapshot directly from the server."""
        assert self.observer is not None
        response = self.observer.get(f"/v1/sessions/{session_id}")
        response.raise_for_status()
        return dict(response.json())

    def items(self, session_id: str) -> list[dict[str, Any]]:
        """Read every committed conversation item directly from the server."""
        assert self.observer is not None
        response = self.observer.get(f"/v1/sessions/{session_id}/items", params={"limit": 1000})
        response.raise_for_status()
        return list(response.json().get("data", []))

    def wait_for_text(
        self, session_id: str, needle: str, *, role: str = "assistant", timeout: float = 120.0
    ) -> dict[str, Any]:
        """Wait for a committed message from *role* containing *needle*.

        :param session_id: Session to read.
        :param needle: Substring to find, e.g. a scenario marker.
        :param role: ``assistant`` or ``user``.
        :param timeout: Seconds to wait.
        :returns: The matching item.
        """

        def _find() -> dict[str, Any] | None:
            for item in self.items(session_id):
                if item.get("type") == "message" and item.get("role") == role:
                    if needle in message_text(item):
                        return item
            return None

        return wait_for(_find, timeout=timeout, what=f"{role} text {needle!r}")

    # ── startup ──────────────────────────────────────────────────

    @property
    def _model_url(self) -> str:
        return f"http://127.0.0.1:{self._model_port}"

    @property
    def _host_tmp(self) -> Path:
        return self.root / "t"

    def _client(self, base_url: str, *, timeout: float = 30.0) -> httpx.Client:
        client = httpx.Client(base_url=base_url, timeout=timeout, trust_env=False)
        self._clients.append(client)
        return client

    def _start(self) -> None:
        self.events.emit("lab", "starting", mode=self.config.mode, front=self.config.front)
        for path in (self.logs, self.workspace, self._host_tmp):
            path.mkdir(parents=True, exist_ok=True)
        (self.workspace / "README.md").write_text("# resilience lab workspace\n")
        self._loop.start()
        upstream_down = "gateway" if self.config.front == "ingress" else "reset"
        self._proxies = LabProxies(
            client=FaultProxy(
                "client",
                ("127.0.0.1", self._server_port),
                loop=self._loop,
                events=self.events,
                classify=client_link,
                upstream_down=upstream_down,
            ),
            host=FaultProxy(
                "host",
                ("127.0.0.1", self._server_port),
                loop=self._loop,
                events=self.events,
                classify=host_link,
                upstream_down=upstream_down,
            ),
            model=FaultProxy(
                "model",
                ("127.0.0.1", self._model_port),
                loop=self._loop,
                events=self.events,
                classify=fixed_link("model"),
                intercept=self._tls,
            ),
        )
        for proxy in (self._proxies.client, self._proxies.host, self._proxies.model):
            proxy.start()
        self.client = self._client(self._proxies.client.url, timeout=60.0)
        self.observer = self._client(self.server_url)
        self._start_model()
        self._start_server()
        if self.config.mode == "host":
            self._start_host()
        else:
            self._start_runner()
        self.events.emit("lab", "ready")

    def _base_env(self) -> dict[str, str]:
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in _STRIP_ENV and not key.startswith(_STRIP_ENV_PREFIXES)
        }
        pythonpath = [
            str(_REPO_ROOT),
            str(_REPO_ROOT / "sdks" / "python-client"),
            str(_REPO_ROOT / "sdks" / "ui"),
        ]
        env.update(
            {
                "PYTHONPATH": os.pathsep.join(pythonpath),
                "NO_PROXY": "127.0.0.1,localhost,::1",
                "no_proxy": "127.0.0.1,localhost,::1",
                "OMNIGENT_AUTH_PROVIDER": "header",
                "OMNIGENT_LOCAL_SINGLE_USER": "1",
                "OMNIGENT_DISABLE_CATALOG_LOOKUP": "1",
                "OMNIGENT_LOG_TO_STDERR": "1",
                "OMNIGENT_DATA_DIR": str(self.root / "data"),
                "CLAUDE_CONFIG_DIR": str(self.root / "claude-config"),
                "CODEX_HOME": str(self.root / "codex-config"),
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "DISABLE_AUTOUPDATER": "1",
                # Never read the developer's Databricks profiles.
                "DATABRICKS_CONFIG_FILE": str(self._empty_databricks_config()),
            }
        )
        return env

    def _empty_databricks_config(self) -> Path:
        path = self.root / "databrickscfg"
        if not path.exists():
            path.write_text("")
        return path

    def _spawn(self, name: str, argv: list[str], env: dict[str, str]) -> ManagedProcess:
        process = ManagedProcess(
            name,
            argv,
            env=env,
            cwd=self.root,
            log_path=self.logs / f"{name}.log",
            events=self.events,
        )
        process.start()
        self._processes.append(process)
        return process

    def _start_model(self) -> None:
        env = self._base_env()
        self._spawn(
            "model",
            [sys.executable, "-m", "tests.e2e.resilience.lab.model_server", str(self._model_port)],
            env,
        )
        self.model = MockModel(self._model_url)
        probe = self._client(self._model_url, timeout=2.0)
        wait_for(
            lambda: True if _ok(probe, "/stats") else None,
            timeout=30,
            what="the mock model server",
        )
        # Policy classification must never block a scenario on an empty queue.
        self.model.set_fallback('{"action":"allow","reason":""}', key=_POLICY_MODEL)

    def _start_server(self) -> None:
        config_home = self.root / "server-config"
        _write_yaml(
            config_home / "server.yaml",
            {
                "llm": {
                    "model": _POLICY_MODEL,
                    "connection": {"base_url": f"{self._model_url}/v1", "api_key": "mock-key"},
                }
            },
        )
        env = self._base_env()
        env["OMNIGENT_CONFIG_HOME"] = str(config_home)
        if self.config.mode == "runner":
            env["OMNIGENT_RUNNER_TUNNEL_TOKEN"] = self._tunnel_token
        if (_WEB_UI_DIST / "index.html").is_file():
            env["OMNIGENT_WEB_UI_DIST"] = str(_WEB_UI_DIST)
        argv = [
            sys.executable,
            "-m",
            "omnigent.cli",
            "server",
            "--host",
            "127.0.0.1",
            "--port",
            str(self._server_port),
            "--database-uri",
            f"sqlite:///{self.root / 'server.db'}",
            "--artifact-location",
            str(self.root / "artifacts"),
            "--config",
            str(config_home / "server.yaml"),
        ]
        self._server = self._spawn("server", argv, env)
        self._wait_server_healthy()

    def _wait_server_healthy(self) -> None:
        assert self.observer is not None and self._server is not None
        server = self._server

        def _healthy() -> bool | None:
            if not server.running:
                raise RuntimeError(f"server exited:\n{server.log_tail()}")
            return True if _ok(self.observer, "/health") else None

        wait_for(_healthy, timeout=_STARTUP_TIMEOUT_S, what="the server to become healthy")

    def _host_side_env(self) -> dict[str, str]:
        config_home = self.root / "host-config"
        config: dict[str, Any] = {
            "runner": {"idle_timeout_s": 0},
            "providers": {
                "lab-claude": {
                    "kind": "key",
                    "default": ["anthropic"],
                    "anthropic": {
                        "base_url": self.proxies.model.url,
                        "api_key": "mock-key",
                        "models": {"default": _CLAUDE_MODEL},
                    },
                }
            },
        }
        if self.config.mode == "host":
            config["host"] = {"host_id": self.host_id, "name": self.host_name}
        _write_yaml(config_home / "config.yaml", config)
        self._seed_claude_config()
        env = self._base_env()
        env.update(
            {
                "OMNIGENT_CONFIG_HOME": str(config_home),
                # Keep host inventory (skills, credentials, imports) off the
                # developer's real home directory.
                "HOME": str(self._host_home()),
                "TMPDIR": str(self._host_tmp),
                # Harness HTTPS (model calls, telemetry) goes to the model proxy.
                "HTTPS_PROXY": self.proxies.model.url,
                "NODE_EXTRA_CA_CERTS": str(self._tls.ca_path),
                "OMNIGENT_RUNNER_ENV_PASSTHROUGH": ",".join(_RUNNER_PASSTHROUGH),
                "OMNIGENT_PROCESS_LOG_FILE": str(self.logs / f"{self.config.mode}-process.log"),
            }
        )
        return env

    def _host_home(self) -> Path:
        home = self.root / "home"
        home.mkdir(parents=True, exist_ok=True)
        return home

    def _seed_claude_config(self) -> None:
        """Pre-accept Claude Code's onboarding and workspace-trust prompts."""
        claude_dir = self.root / "claude-config"
        claude_dir.mkdir(parents=True, exist_ok=True)
        trusted = {"hasTrustDialogAccepted": True, "hasCompletedProjectOnboarding": True}
        projects = {str(self.workspace): trusted, os.path.realpath(self.workspace): trusted}
        (claude_dir / ".claude.json").write_text(
            json.dumps(
                {
                    "hasCompletedOnboarding": True,
                    "theme": "dark",
                    "projects": projects,
                }
            )
        )

    def _start_host(self) -> None:
        env = self._host_side_env()
        argv = [
            sys.executable,
            "-m",
            "omnigent.host._daemon_entry",
            "--server",
            self.proxies.host.url,
        ]
        self._host = self._spawn("host", argv, env)
        assert self.observer is not None
        observer, host = self.observer, self._host

        def _online() -> bool | None:
            if not host.running:
                raise RuntimeError(f"host daemon exited:\n{host.log_tail()}")
            response = observer.get("/v1/hosts")
            if response.status_code != 200:
                return None
            for row in response.json().get("hosts", []):
                if row.get("host_id") == self.host_id and row.get("status") == "online":
                    return True
            return None

        wait_for(_online, timeout=_STARTUP_TIMEOUT_S, what="the host daemon to register")

    def _start_runner(self) -> None:
        from omnigent.runner.identity import token_bound_runner_id

        self.runner_id = token_bound_runner_id(self._tunnel_token)
        env = self._host_side_env()
        env.update(
            {
                "RUNNER_SERVER_URL": self.proxies.host.url,
                "OMNIGENT_RUNNER_ID": self.runner_id,
                "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": self._tunnel_token,
                "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
                "OMNIGENT_RUNNER_WORKSPACE": str(self.workspace),
            }
        )
        self._runner = self._spawn("runner", [sys.executable, "-m", "omnigent.runner._entry"], env)
        assert self.observer is not None
        observer, runner = self.observer, self._runner

        def _online() -> bool | None:
            if not runner.running:
                raise RuntimeError(f"runner exited:\n{runner.log_tail()}")
            response = observer.get(f"/v1/runners/{self.runner_id}/status")
            return True if response.status_code == 200 and response.json().get("online") else None

        wait_for(_online, timeout=_STARTUP_TIMEOUT_S, what="the runner to come online")

    def _kill_stray_host_processes(self) -> None:
        """Reap tmux servers and harnesses that outlived their runner."""
        for proc in processes_mentioning(f"{self._host_tmp}{os.sep}"):
            for member in process_tree([proc.pid]):
                with contextlib.suppress(psutil.NoSuchProcess):
                    member.kill()


def message_text(item: dict[str, Any]) -> str:
    """Concatenate the text blocks of a conversation message item.

    :param item: Item from ``GET /v1/sessions/{id}/items``.
    :returns: Joined text, or ``""``.
    """
    content = item.get("content")
    if not isinstance(content, list):
        return ""
    return "".join(str(block.get("text", "")) for block in content if isinstance(block, dict))


def _ok(client: httpx.Client | None, path: str) -> bool:
    if client is None:
        return False
    try:
        return client.get(path).status_code == 200
    except httpx.HTTPError:
        return False

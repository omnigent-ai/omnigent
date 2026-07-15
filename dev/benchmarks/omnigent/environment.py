"""Benchmark environment lifecycle.

:class:`BenchEnvironment` is an async context manager that stands up a real
Omnigent ``server`` with no Databricks credentials. Two modes:

- ``with_runner=False`` (default): server + SQLite DB only. Enough for the
  HTTP/API journeys, which never drive an agent turn.
- ``with_runner=True``: additionally spawns a zero-latency mock LLM and a
  sibling ``runner``, routes the server-side prompt-policy classifier at the
  mock (via ``--config``), and sets an ALLOW fallback — everything the
  full-turn journeys need.

A full env is a strict superset of the HTTP-only env, so both modes share one
class; the runner mode is gated behind the flag rather than forked into a
separate type. It mirrors the proven ``live_server`` e2e recipe
(``tests/e2e/conftest.py``) and reuses the credential-free spawn core: the
compat helpers (so subprocesses import this worktree) and
``token_bound_runner_id``.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import signal
import socket
import subprocess
import sys
import tarfile
import time
import uuid
from pathlib import Path
from typing import IO

import httpx
import yaml

from omnigent.host.identity import HOST_ID_ENV_VAR, HOST_NAME_ENV_VAR
from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN, token_bound_runner_id
from tests._helpers.compat import (
    apply_runner_env,
    apply_server_env,
    compat_runner_cwd,
    compat_server_cwd,
    runner_executable,
    server_executable,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MOCK_SERVER = _REPO_ROOT / "tests" / "server" / "integration" / "mock_llm_server.py"

_HEALTH_TIMEOUT_S = 90.0
_MOCK_TIMEOUT_S = 15.0
_POLL_INTERVAL_S = 0.2
_TURN_TIMEOUT_S = 180.0
# Budget for the host daemon (session_cold_start journey, with_host) to connect
# its tunnel and register in the hosts table after being spawned. Covers
# interpreter start + imports + the reverse-tunnel handshake.
_HOST_ONLINE_TIMEOUT_S = 60.0

# Terminal SSE events — if one arrives before any delta, the turn produced no
# streamed text (a failure for the TTFT journey).
_STREAM_TERMINAL_EVENTS = frozenset(
    {"response.completed", "response.failed", "response.cancelled"}
)
# The server persists an interrupted turn as a synthetic user message whose
# text contains this marker (see tests/e2e/test_cancel_history.py).
_CANCELLATION_MARKER = "interrupted"

# Default full-turn agent (with_runner=True). The mock ignores the model for
# routing (its "default" queue serves any request), but the key is baked into
# the spec so the harness has a concrete model to send.
_DEFAULT_MODEL = "mock-bench-brain"
_DEFAULT_HARNESS = "openai-agents"

# Server-side prompt-policy classifier queue key. In runner mode we set an
# ALLOW fallback here so a classifier call (if the agent trips one) never
# blocks or returns non-verdict text.
_POLICY_LLM_KEY = "_policy_llm_"
_POLICY_ALLOW = '{"action": "allow", "reason": ""}'


def _find_free_port() -> int:
    """Bind an ephemeral port and return it (races are tolerated by retries)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _omni_executable() -> str:
    """The ``omni`` console script beside the (compat-aware) interpreter.

    ``server_executable()`` returns the interpreter the server/runner subprocess
    should run under — ``sys.executable`` normally, or a pinned older build's
    python in cross-version compat mode. The ``omni`` console script is
    installed next to that interpreter (``[project.scripts]`` in pyproject), so
    deriving it from the same directory launches the real user-facing command
    (``omni server`` / ``omni host``) while still honoring the compat pin.
    """
    return str(Path(server_executable()).with_name("omni"))


class BenchEnvironment:
    """Async context manager owning the benchmark's server (± runner + mock).

    :param with_runner: When ``False`` (default), boot the server only — the
        v1 HTTP-journey path. When ``True``, also spawn the mock LLM and a
        runner and wire the policy classifier at the mock — the phase-2
        full-turn path.
    :param with_host: When ``True`` (implies ``with_runner``), additionally
        spawn a real ``omnigent host`` daemon. Additive over ``with_runner``:
        the boot runner still serves the warm journeys, while the daemon lets
        the ``session_cold_start`` journey create host-bound sessions that fire
        ``host.launch_runner`` and launch their OWN fresh runner — so the first
        message races the runner's boot, reproducing the true UI cold path.
    :param database_uri: SQLAlchemy URI the server boots against. ``None``
        (default) uses a fresh throwaway SQLite file in the temp dir — the
        empty-DB path. Pass a pre-seeded URI (e.g. a seeded SQLite file, or a
        ``postgresql+psycopg://…`` instance) to benchmark against a realistic
        corpus. Postgres must be the fully-qualified ``+psycopg`` form — the
        server CLI does not normalize it.
    :param harness: Harness for full-turn agents when ``with_runner`` (default
        ``openai-agents``, a base dependency needing no vendor CLI binary).
    :param model: Model string baked into registered agent specs.
    """

    def __init__(
        self,
        *,
        with_runner: bool = False,
        with_host: bool = False,
        database_uri: str | None = None,
        harness: str = _DEFAULT_HARNESS,
        model: str = _DEFAULT_MODEL,
    ) -> None:
        # with_host is additive over with_runner: the boot runner still serves
        # the warm journeys, and the host daemon additionally lets the cold-start
        # journey create host-bound sessions that launch their own runners.
        self.with_host = with_host
        self.with_runner = with_runner or with_host
        self.database_uri = database_uri
        self.harness = harness
        self.model = model
        self.base_url = ""
        self.mock_url = ""
        self.runner_id = ""
        self.host_id = ""
        self.host_workspace = ""
        self.client: httpx.AsyncClient | None = None

        self._tmp = Path("/tmp") / f"omni-bench-{uuid.uuid4().hex[:8]}"
        self._mock_proc: subprocess.Popen[bytes] | None = None
        self._server_proc: subprocess.Popen[bytes] | None = None
        self._runner_proc: subprocess.Popen[bytes] | None = None
        self._host_proc: subprocess.Popen[bytes] | None = None
        # Base env retained so the host daemon is built identically to the boot
        # runner's server-facing env (worktree source, mock LLM routing).
        self._runner_base_env: dict[str, str] = {}
        self._log_handles: list[IO[bytes]] = []
        self._agent_cache: dict[str, str] = {}

    # ── lifecycle ────────────────────────────────────────────

    async def __aenter__(self) -> BenchEnvironment:
        await asyncio.to_thread(self._start)
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=300.0,
            headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
        )
        if self.with_runner:
            # ALLOW fallback so a server-side classifier call resolves against
            # the mock (never api.openai.com) and returns a valid verdict.
            await self._mock_post(
                "/mock/set_fallback", {"key": _POLICY_LLM_KEY, "text": _POLICY_ALLOW}
            )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self.client is not None:
            await self.client.aclose()
        await asyncio.to_thread(self._stop)

    def _start(self) -> None:
        """Spawn the server (± mock + runner) and block until ready."""
        self._tmp.mkdir(mode=0o700, parents=True, exist_ok=True)
        artifact_dir = self._tmp / "artifacts"
        artifact_dir.mkdir(exist_ok=True)

        if self.with_runner:
            mock_port = _find_free_port()
            self.mock_url = f"http://127.0.0.1:{mock_port}"
            self._mock_proc = self._spawn_mock(mock_port)
            self._wait_mock_ready()

        port = _find_free_port()
        self.base_url = f"http://localhost:{port}"
        binding_token = uuid.uuid4().hex

        base_env = {**os.environ}
        if self.with_runner:
            self.runner_id = token_bound_runner_id(binding_token)
            base_env["OPENAI_API_KEY"] = "mock-key"
            # The OpenAI SDK appends /responses, so include /v1 in the base.
            base_env["OPENAI_BASE_URL"] = f"{self.mock_url}/v1"
        # Prepend the worktree so subprocesses import this branch's source.
        apply_server_env(base_env, _REPO_ROOT)
        # Retained so the host daemon (with_host) is built with the same
        # server-facing env as the boot runner.
        self._runner_base_env = base_env

        self._server_proc = self._spawn_server(port, base_env, binding_token, artifact_dir)
        if self.with_runner:
            self._runner_proc = self._spawn_runner(base_env, binding_token)
        self._wait_ready()
        # The host daemon is ADDITIVE — the boot runner above still serves the
        # warm journeys; the daemon exists so the cold-start journey can create
        # host-bound sessions that launch their OWN fresh runners on demand
        # (the race the cold path measures). The two never share a runner id.
        if self.with_host:
            self._host_proc = self._spawn_host(base_env)
            self._wait_host_online()

    def _stop(self) -> None:
        """Terminate host, runner, server, and mock; remove the temp dir."""
        # Host first: SIGTERM-ing the daemon reaps the runners IT spawned (they
        # are daemon-owned children), so it must go before the server so those
        # runners' tunnels close cleanly.
        for proc in (
            self._host_proc,
            self._runner_proc,
            self._server_proc,
            self._mock_proc,
        ):
            if proc is not None and proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
        for handle in self._log_handles:
            handle.close()
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)

    # ── spawns ───────────────────────────────────────────────

    def _log(self, name: str) -> IO[bytes]:
        handle = (self._tmp / name).open("wb")
        self._log_handles.append(handle)
        return handle

    def _spawn_mock(self, port: int) -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            [sys.executable, str(_MOCK_SERVER), str(port)],
            env={**os.environ, "PYTHONPATH": str(_REPO_ROOT)},
            stdout=self._log("mock.log"),
            stderr=subprocess.STDOUT,
        )

    def _spawn_server(
        self,
        port: int,
        base_env: dict[str, str],
        binding_token: str,
        artifact_dir: Path,
    ) -> subprocess.Popen[bytes]:
        # Pre-seeded URI when given (realistic corpus), else a throwaway SQLite
        # file in the temp dir (the empty-DB path). SQLite absolute paths need
        # four slashes; the temp path is absolute.
        db_uri = self.database_uri or f"sqlite:///{self._tmp / 'bench.db'}"
        args = [
            _omni_executable(),
            "server",
            "--port",
            str(port),
            "--database-uri",
            db_uri,
            "--artifact-location",
            str(artifact_dir),
        ]
        env = {**base_env}
        if self.with_runner:
            # Route the server-side policy-classifier LLM at the mock, mirroring
            # live_server. Without this the classifier's client defaults to
            # api.openai.com and errors. Server-only mode needs no llm config —
            # the classifier only builds under OMNIGENT_SMART_ROUTING=1.
            server_cfg = self._tmp / "server.yaml"
            server_cfg.write_text(
                yaml.safe_dump(
                    {
                        "llm": {
                            "model": _POLICY_LLM_KEY,
                            "connection": {
                                "base_url": f"{self.mock_url}/v1",
                                "api_key": "mock-key",
                            },
                        }
                    }
                )
            )
            args.extend(["--config", str(server_cfg)])
            env["OMNIGENT_RUNNER_TUNNEL_TOKEN"] = binding_token
        return subprocess.Popen(
            args,
            env=env,
            cwd=compat_server_cwd(),
            stdout=self._log("server.log"),
            stderr=subprocess.STDOUT,
        )

    def _spawn_runner(
        self, base_env: dict[str, str], binding_token: str
    ) -> subprocess.Popen[bytes]:
        # Point the runner's filesystem workspace at the temp dir so file
        # writes (e.g. read_runner_file's setup) land there and are cleaned up
        # on teardown, rather than in the launch cwd (its default).
        workspace = self._tmp / "workspace"
        workspace.mkdir(exist_ok=True)
        return self._spawn_runner_process(
            base_env,
            binding_token,
            runner_id=self.runner_id,
            workspace=workspace,
            log_name="runner.log",
        )

    def _spawn_runner_process(
        self,
        base_env: dict[str, str],
        binding_token: str,
        *,
        runner_id: str,
        workspace: Path,
        log_name: str,
    ) -> subprocess.Popen[bytes]:
        """Spawn one runner subprocess under *runner_id* + *binding_token*.

        Factored out of :meth:`_spawn_runner` so the ``session_cold_start``
        journey can spawn additional runners on demand, each under its own id,
        binding token, and workspace. The caller must pair *runner_id* with the token
        it derives from (``token_bound_runner_id(binding_token)``): the runner
        derives its managed-mint URL from the token internally, so a mismatch
        would register the tunnel under one id but mint under another (→ 401).
        """
        runner_env = apply_runner_env(
            {
                **base_env,
                "OMNIGENT_RUNNER_ID": runner_id,
                "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
                "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
                "RUNNER_SERVER_URL": self.base_url,
                "OMNIGENT_RUNNER_WORKSPACE": str(workspace),
            }
        )
        return subprocess.Popen(
            [runner_executable(), "-m", "omnigent.runner._entry"],
            env=runner_env,
            cwd=compat_runner_cwd(),
            stdout=self._log(log_name),
            stderr=subprocess.STDOUT,
        )

    def _spawn_host(self, base_env: dict[str, str]) -> subprocess.Popen[bytes]:
        """Spawn a real ``omni host`` daemon against the bench server.

        Runs the user-facing ``omni host --server`` command — the same daemon a
        developer starts by hand. Identity comes from :data:`HOST_ID_ENV_VAR` /
        :data:`HOST_NAME_ENV_VAR`: with both set, ``load_or_create_host_identity``
        returns that identity WITHOUT reading or writing any ``config.yaml``, so
        the daemon never touches the developer's real ``~/.omnigent`` (nor
        collides with a sibling bench leg). ``--non-interactive`` keeps it from
        ever launching a browser login (moot for the loopback server, which is
        not Databricks-fronted, but explicit for CI). The daemon self-registers
        over loopback (single-user ``RESERVED_USER_LOCAL`` owner, no token) and
        launches runners on demand when the server sends ``host.launch_runner``.
        """
        self.host_id = f"host_bench_{uuid.uuid4().hex[:12]}"
        workspace = self._tmp / "host-workspace"
        workspace.mkdir(exist_ok=True)
        self.host_workspace = str(workspace)
        host_env = {
            **base_env,
            HOST_ID_ENV_VAR: self.host_id,
            HOST_NAME_ENV_VAR: f"bench-host-{self.host_id[-8:]}",
        }
        return subprocess.Popen(
            [_omni_executable(), "host", "--server", self.base_url, "--non-interactive"],
            env=host_env,
            cwd=str(workspace),
            stdout=self._log("host-daemon.log"),
            stderr=subprocess.STDOUT,
        )

    # ── readiness ────────────────────────────────────────────

    def _wait_mock_ready(self) -> None:
        deadline = time.monotonic() + _MOCK_TIMEOUT_S
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"{self.mock_url}/stats", timeout=1).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        raise RuntimeError(f"mock LLM not ready within {_MOCK_TIMEOUT_S}s; logs in {self._tmp}")

    def _wait_ready(self) -> None:
        """Wait for ``/health`` (and, in runner mode, the runner online)."""
        deadline = time.monotonic() + _HEALTH_TIMEOUT_S
        while time.monotonic() < deadline:
            try:
                health = httpx.get(f"{self.base_url}/health", timeout=2)
                if health.status_code == 200 and self._runner_ready():
                    return
            except httpx.HTTPError:
                pass
            time.sleep(_POLL_INTERVAL_S)
        raise RuntimeError(f"server not ready within {_HEALTH_TIMEOUT_S}s; logs in {self._tmp}")

    def _runner_ready(self) -> bool:
        """Whether the boot runner reports online (always ``True`` server-only)."""
        if not self.with_runner:
            return True
        status = httpx.get(f"{self.base_url}/v1/runners/{self.runner_id}/status", timeout=2)
        return status.status_code == 200 and status.json().get("online") is True

    def _wait_host_online(self) -> None:
        """Block until the host daemon's row reads ``status=online``.

        Polls ``GET /v1/hosts`` (the single-user owner is ``local``) until the
        daemon we spawned has connected its tunnel and been upserted online, so
        a host-bound session-create has a live launch target.
        """
        deadline = time.monotonic() + _HOST_ONLINE_TIMEOUT_S
        while time.monotonic() < deadline:
            if self._host_proc is not None and self._host_proc.poll() is not None:
                raise RuntimeError(
                    f"host daemon exited (code {self._host_proc.returncode}) before "
                    f"coming online; logs in {self._tmp}"
                )
            try:
                resp = httpx.get(f"{self.base_url}/v1/hosts", timeout=2)
                if resp.status_code == 200:
                    for host in resp.json().get("hosts", []):
                        if host.get("host_id") == self.host_id and host.get("status") == "online":
                            return
            except httpx.HTTPError:
                # Server not yet accepting requests, or a transient read error:
                # keep polling until the deadline rather than failing the boot.
                pass
            time.sleep(_POLL_INTERVAL_S)
        raise RuntimeError(f"host {self.host_id} not online within {_HOST_ONLINE_TIMEOUT_S}s")

    # ── mock control (runner mode only) ──────────────────────

    async def _mock_post(self, path: str, body: dict[str, object]) -> None:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(f"{self.mock_url}{path}", json=body)
            resp.raise_for_status()

    async def configure_mock(
        self,
        responses: list[dict[str, object]],
        *,
        key: str = "default",
        match: str | None = None,
    ) -> None:
        """Load a keyed response queue on the mock (see e2e ``configure_mock_llm``)."""
        payload: dict[str, object] = {"key": key, "responses": responses}
        if match is not None:
            payload["match"] = match
        await self._mock_post("/mock/configure", payload)

    async def set_mock_fallback(
        self, text: str, *, key: str = "default", stream: bool = False
    ) -> None:
        """Set a reset-surviving fallback response for a mock queue *key*.

        :param stream: When ``True`` the fallback emits per-word
            ``output_text.delta`` events before completing — needed for the
            time-to-first-token journey to observe streamed deltas.
        """
        await self._mock_post("/mock/set_fallback", {"key": key, "text": text, "stream": stream})

    # ── agent + session primitives ───────────────────────────

    def _agent_bundle(self, name: str) -> bytes:
        """Build a ``spec_version: 1`` agent bundle.

        In runner mode the executor is wired at the mock LLM (auth +
        connection). Server-only, no LLM is ever called, so the bundle just
        needs to be a valid spec the server can register and bind sessions to.
        """
        executor: dict[str, object] = {
            "type": "omnigent",
            "model": self.model,
            "config": {"harness": self.harness},
        }
        config: dict[str, object] = {
            "spec_version": 1,
            "name": name,
            "prompt": "You are a helpful assistant used for performance benchmarking.",
            "executor": executor,
        }
        if self.with_runner:
            executor["auth"] = {
                "type": "api_key",
                "api_key": "mock-key",
                "base_url": f"{self.mock_url}/v1",
            }
            executor["connection"] = {"base_url": f"{self.mock_url}/v1", "api_key": "mock-key"}
            # A filesystem env so the runner can serve the resource endpoints
            # (read_runner_file). Without os_env the runner has no primary
            # environment to materialize and the filesystem proxy 404s.
            # sandbox.type=none avoids needing a bwrap binary on the host.
            config["os_env"] = {
                "type": "caller_process",
                "cwd": ".",
                "sandbox": {"type": "none"},
            }
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            payload = yaml.safe_dump(config).encode()
            info = tarfile.TarInfo("config.yaml")
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        return buf.getvalue()

    async def ensure_agent(self, name: str = "bench-agent") -> str:
        """Register the benchmark agent once, returning its name (idempotent)."""
        assert self.client is not None
        if name in self._agent_cache:
            return name
        resp = await self.client.post(
            "/v1/sessions",
            data={"metadata": "{}"},
            files={"bundle": ("agent.tar.gz", self._agent_bundle(name), "application/gzip")},
        )
        if resp.status_code not in (200, 201, 409):
            raise RuntimeError(f"agent register failed: {resp.status_code} {resp.text[:400]}")
        self._agent_cache[name] = name
        return name

    async def agent_id(self, agent_name: str) -> str:
        """Resolve a registered agent's id by name."""
        assert self.client is not None
        listing = await self.client.get(
            "/v1/sessions", params={"agent_name": agent_name, "limit": 1}
        )
        listing.raise_for_status()
        return str(listing.json()["data"][0]["agent_id"])

    async def create_session(self, agent_id: str) -> str:
        """Create an (unbound) session for *agent_id*, returning its id."""
        assert self.client is not None
        created = await self.client.post("/v1/sessions", json={"agent_id": agent_id})
        created.raise_for_status()
        return str(created.json()["id"])

    async def create_hosted_session(self, agent_id: str) -> str:
        """Create a host-bound session that fires ``host.launch_runner``.

        The inline-launch ``POST /v1/sessions`` shape the Web UI's New Chat
        wizard sends: passing ``host_id`` + ``workspace`` makes the server bind a
        runner id and dispatch a launch frame to the host daemon, then return
        immediately (~tens of ms) WITHOUT waiting for the runner to connect.
        Returned without any readiness poll on purpose — the caller's first
        message then races the runner's boot, which is the cold path we measure.

        :raises RuntimeError: If the env was not built with ``with_host=True``.
        """
        assert self.client is not None
        if not self.with_host:
            raise RuntimeError("create_hosted_session requires with_host=True")
        created = await self.client.post(
            "/v1/sessions",
            json={
                "agent_id": agent_id,
                "host_id": self.host_id,
                "host_type": "external",
                "workspace": self.host_workspace,
            },
        )
        created.raise_for_status()
        return str(created.json()["id"])

    async def seed_items(self, session_id: str, count: int) -> None:
        """Append *count* history items over HTTP, with no runner or LLM.

        Uses the ``external_conversation_item`` event, which the server
        appends "without starting or steering a task" — the runner-free path
        for giving ``load_conversation_history`` something to read back.

        Items are user messages: assistant messages require an ``agent`` field
        the server only has after a real turn, and the read path this seeds is
        role-agnostic — item count and size, not role, drive its cost.
        """
        assert self.client is not None
        for i in range(count):
            body = {
                "type": "external_conversation_item",
                "data": {
                    "item_type": "message",
                    "item_data": {
                        "role": "user",
                        "content": [{"type": "input_text", "text": f"benchmark seed item {i}"}],
                    },
                },
            }
            resp = await self.client.post(f"/v1/sessions/{session_id}/events", json=body)
            resp.raise_for_status()

    # ── runner-mode session driving (phase 2) ────────────────

    async def create_bound_session(self, agent_id: str) -> str:
        """Create a session for *agent_id* and bind it to the boot runner."""
        return await self.create_session_bound_to(agent_id, self.runner_id)

    async def create_session_bound_to(self, agent_id: str, runner_id: str) -> str:
        """Create a session for *agent_id* and bind it to *runner_id*.

        Binds a session to an already-online runner by patching its
        ``runner_id`` — used by the warm journeys via :meth:`create_bound_session`
        to pin the boot runner.
        """
        assert self.client is not None
        if not self.with_runner:
            raise RuntimeError("create_session_bound_to requires with_runner=True")
        session_id = await self.create_session(agent_id)
        bound = await self.client.patch(
            f"/v1/sessions/{session_id}", json={"runner_id": runner_id}
        )
        bound.raise_for_status()
        return session_id

    async def write_runner_file(self, session_id: str, relative_path: str, content: str) -> None:
        """Write a file into the runner's default environment over HTTP.

        The server proxies the ``PUT`` to the bound runner, which writes to its
        sandboxed filesystem — so this needs a runner. Used to plant a file the
        read journey can then fetch back.

        :raises RuntimeError: If not in runner mode.
        """
        assert self.client is not None
        if not self.with_runner:
            raise RuntimeError("write_runner_file requires with_runner=True")
        resp = await self.client.put(
            f"/v1/sessions/{session_id}/resources/environments/default/filesystem/{relative_path}",
            json={"content": content, "encoding": "utf-8"},
        )
        resp.raise_for_status()

    async def read_runner_file(self, session_id: str, relative_path: str) -> None:
        """Read a file from the runner's default environment over HTTP.

        Times the server → runner filesystem proxy (a localhost round-trip); no
        LLM is involved. Requires a runner — the server returns 502 without one.

        :raises RuntimeError: If not in runner mode.
        """
        assert self.client is not None
        if not self.with_runner:
            raise RuntimeError("read_runner_file requires with_runner=True")
        resp = await self.client.get(
            f"/v1/sessions/{session_id}/resources/environments/default/filesystem/{relative_path}",
        )
        resp.raise_for_status()

    async def drive_turn(
        self, session_id: str, text: str, *, timeout: float = _TURN_TIMEOUT_S
    ) -> None:
        """Post a user message and poll the session to a terminal state.

        :raises RuntimeError: If not in runner mode, the turn fails, or it does
            not settle within *timeout* seconds.
        """
        assert self.client is not None
        if not self.with_runner:
            raise RuntimeError("drive_turn requires with_runner=True")
        body = {
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": text}]},
        }
        posted = await self.client.post(f"/v1/sessions/{session_id}/events", json=body)
        posted.raise_for_status()

        deadline = time.monotonic() + timeout
        seen_running = False
        while time.monotonic() < deadline:
            snap = await self.client.get(f"/v1/sessions/{session_id}")
            snap.raise_for_status()
            status = snap.json().get("status")
            if status in ("running", "waiting"):
                seen_running = True
            elif status == "failed":
                raise RuntimeError(f"turn failed: {snap.json().get('last_task_error')}")
            elif status == "idle" and seen_running:
                return
            await asyncio.sleep(_POLL_INTERVAL_S)
        raise RuntimeError(f"turn did not settle within {timeout}s (session {session_id})")

    async def _wait_idle(self, session_id: str, *, timeout: float = _TURN_TIMEOUT_S) -> None:
        """Poll until the session is ``idle`` (a prior turn has settled)."""
        assert self.client is not None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snap = await self.client.get(f"/v1/sessions/{session_id}")
            snap.raise_for_status()
            if snap.json().get("status") == "idle":
                return
            await asyncio.sleep(_POLL_INTERVAL_S)
        raise RuntimeError(f"session did not reach idle within {timeout}s ({session_id})")

    async def _post_and_await_first_delta(
        self,
        session_id: str,
        text: str,
        *,
        wait_idle_first: bool,
        timeout: float = _TURN_TIMEOUT_S,
    ) -> None:
        """Imitate the UI first-token path: attach SSE, then post, then await.

        The exact sequence the web client follows for a one-shot turn:
        subscribe to ``GET …/stream``, wait for the stream's ready heartbeat (the
        first SSE line — the server yields it right after registering the
        live-tail slot, so no event can be missed), POST the message, and return
        on the first response from the model — either a
        ``response.output_text.delta`` (streamed text) or a
        ``response.output_item.done`` (a completed output item, e.g. a tool call
        for harnesses that don't stream text deltas). This measures time to *any*
        first response, not just text. A terminal event before either arrives
        means the turn produced no response at all (a failure).

        :param wait_idle_first: When ``True``, wait for the session to be ``idle``
            before subscribing so a prior turn's terminal event can't race this
            turn's response (warm-session TTFT). ``False`` for a fresh session whose
            first turn is the only one — the cold path, where the timed span must
            include runner launch + connect, so we must NOT poll it warm first.
        :raises RuntimeError: If not in runner mode, or no response / a terminal
            event arrives within *timeout*.
        """
        assert self.client is not None
        if not self.with_runner:
            raise RuntimeError("first-delta timing requires with_runner=True")

        connected = asyncio.Event()
        first_delta = asyncio.Event()
        first_response = asyncio.Event()
        outcome: dict[str, str] = {}

        async def _read_stream() -> None:
            try:
                async with self.client.stream(  # type: ignore[union-attr]
                    "GET", f"/v1/sessions/{session_id}/stream", timeout=timeout
                ) as resp:
                    # Any first line means the SSE connection is live (the server
                    # emits a ready heartbeat on connect). Signalling here lets us
                    # post the turn only once subscribed — without a blind sleep
                    # that would otherwise inflate the measured time-to-first-delta.
                    connected.set()
                    async for line in resp.aiter_lines():
                        if not line.startswith("event:"):
                            continue
                        etype = line[len("event:") :].strip()
                        if etype == "response.output_text.delta":
                            first_delta.set()
                            return
                        if etype == "response.output_item.done":
                            first_response.set()
                            return
                        if etype in _STREAM_TERMINAL_EVENTS:
                            outcome["terminal"] = etype
                            first_delta.set()
                            return
            except httpx.HTTPError as exc:
                outcome["error"] = repr(exc)
                connected.set()
                first_delta.set()

        if wait_idle_first:
            # Warm path: ensure any prior turn has settled so the fresh
            # subscription's first terminal event can't be the previous turn
            # completing (which would otherwise race ahead of this turn's delta).
            await self._wait_idle(session_id, timeout=timeout)

        reader = asyncio.create_task(_read_stream())
        try:
            # Wait until the stream is actually connected (not a fixed sleep) so
            # the measured window is post → first response, not subscription setup.
            await asyncio.wait_for(connected.wait(), timeout=timeout)
            posted = await self.client.post(
                f"/v1/sessions/{session_id}/events",
                json={
                    "type": "message",
                    "data": {"role": "user", "content": [{"type": "input_text", "text": text}]},
                },
            )
            posted.raise_for_status()
            # Return on the first response, whichever comes first: a streamed text
            # delta or a completed output item (e.g. a tool call for harnesses that
            # don't stream text).
            waiters = [
                asyncio.create_task(first_delta.wait()),
                asyncio.create_task(first_response.wait()),
            ]
            done, pending = await asyncio.wait(
                waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            if not done:
                raise RuntimeError(
                    "no output_text.delta or output_item.done within "
                    f"{timeout}s (session {session_id})"
                )
            if "error" in outcome:
                raise RuntimeError(f"stream error: {outcome['error']}")
            if "terminal" in outcome:
                raise RuntimeError(
                    f"turn reached {outcome['terminal']} before any response "
                    f"(session {session_id})"
                )
        finally:
            reader.cancel()

    async def time_to_first_delta(
        self, session_id: str, text: str, *, timeout: float = _TURN_TIMEOUT_S
    ) -> None:
        """Post a turn on a WARM session and return on the first output delta.

        Times omnigent's streaming-pipeline overhead to first token against an
        already-connected runner — with the zero-latency mock there is no model
        latency in the number. See :meth:`_post_and_await_first_delta`.
        """
        await self._post_and_await_first_delta(
            session_id, text, wait_idle_first=True, timeout=timeout
        )

    async def cold_start_first_delta(
        self, agent_id: str, text: str, *, timeout: float = _TURN_TIMEOUT_S
    ) -> None:
        """Time the full UI cold path: create → attach SSE → send → first token.

        Reproduces exactly what the Web UI does for a brand-new host-bound
        session: create the session (which fires ``host.launch_runner`` and
        returns before the runner connects), then run the standard first-token
        sequence (attach the SSE stream, wait for its ready heartbeat, POST the
        first message, await the first ``response.output_text.delta``). Because
        the runner is still booting when the message posts, the server's
        connect-grace wait is on the timed path — so the measured span captures
        the real cold-start cost the ``session_cold_start`` journey exists for:
        host launch + runner boot + reverse-tunnel connect + first-token
        pipeline. No pre-warm and no ``GET /session`` status polling — the SSE
        first-delta signal is the same one the UI renders on.

        :raises RuntimeError: If not host-backed, or no delta / a terminal event
            arrives within *timeout*.
        """
        session_id = await self.create_hosted_session(agent_id)
        await self._post_and_await_first_delta(
            session_id, text, wait_idle_first=False, timeout=timeout
        )

    async def drive_and_interrupt(
        self, session_id: str, *, timeout: float = _TURN_TIMEOUT_S
    ) -> None:
        """Drive a gated turn, interrupt it mid-flight, return when cancelled.

        The caller configures a ``block=True`` mock response first (see
        :meth:`configure_mock`), so the turn parks in ``running`` on the
        executor's LLM call. We post an ``interrupt`` once running, wait for the
        server's cancellation marker, then release the gate so the runner
        unwinds cleanly. Times the server → runner → executor cancel path.

        :raises RuntimeError: If not in runner mode, or the interrupt is not
            honored within *timeout*.
        """
        assert self.client is not None
        if not self.with_runner:
            raise RuntimeError("drive_and_interrupt requires with_runner=True")
        body = {
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": "Interrupt me."}]},
        }
        posted = await self.client.post(f"/v1/sessions/{session_id}/events", json=body)
        posted.raise_for_status()

        deadline = time.monotonic() + timeout
        interrupted = False
        try:
            while time.monotonic() < deadline:
                snap = (await self.client.get(f"/v1/sessions/{session_id}")).json()
                status = snap.get("status")
                items = snap.get("items", [])
                if status in ("running", "waiting") and not interrupted:
                    await self.client.post(
                        f"/v1/sessions/{session_id}/events", json={"type": "interrupt"}
                    )
                    interrupted = True
                if _has_cancellation_marker(items):
                    return
                if status == "idle" and interrupted:
                    if _has_cancellation_marker(items):
                        return
                    raise RuntimeError("turn settled without a cancellation marker")
                await asyncio.sleep(_POLL_INTERVAL_S)
            raise RuntimeError(f"interrupt not honored within {timeout}s (session {session_id})")
        finally:
            # Always release the gate so the blocked runner turn unwinds and
            # teardown doesn't hang, even if the interrupt path errored above.
            with contextlib.suppress(httpx.HTTPError):
                await self._mock_post("/gate/release", {})


def _has_cancellation_marker(items: list[dict[str, object]]) -> bool:
    """Whether items include the synthetic 'interrupted' user message."""
    for raw in items:
        data = raw.get("data", raw)
        if not isinstance(data, dict):
            continue
        if raw.get("type") == "message" and data.get("role") == "user":
            content = data.get("content") or []
            if isinstance(content, list) and any(
                isinstance(b, dict) and _CANCELLATION_MARKER in str(b.get("text", ""))
                for b in content
            ):
                return True
    return False

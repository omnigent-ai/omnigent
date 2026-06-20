"""E2E coverage for the Alpha sessions-API REPL lifecycle.

Migrated to use the mock LLM server. These tests run real
``omnigent`` subprocesses under pexpect and exercise the user-visible
flow: session creation, runner binding, streaming text rendering,
resume, and runner recovery. The mock LLM server provides
deterministic responses so no real Databricks credentials are required.
"""

from __future__ import annotations

import contextlib
import os
import re
import signal
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pexpect
import pytest
from omnigent_client import OmnigentClient, SessionsChat

from tests.e2e.omnigent._pexpect_harness import (
    PROMPT_READY,
    STATE_SLEEPING,
    clean_exit,
    ensure_repl_test_theme_env,
    submit_prompt,
)
from tests.e2e.omnigent.conftest import configure_mock_llm

_MODEL = "mock-session-lifecycle"
_HARNESS = "openai-agents"
_LOCAL_REMOTE_AUTH_TOKEN = "local-e2e-runner-token"
_READY_TIMEOUT = 90.0
_TURN_TIMEOUT = 240.0
_EXIT_TIMEOUT = 20.0
_POLL_PAUSE = threading.Event()


@dataclass(frozen=True)
class _LifecycleResult:
    """
    Captured identifiers from a sessions-API REPL turn.

    :param session_id: Created or resumed session id, e.g.
        ``"conv_abc123"``.
    :param runner_id: Runner id bound by the turn, e.g.
        ``"runner_abc123"``.
    """

    session_id: str
    runner_id: str


def _pid_alive(pid: int) -> bool:
    """
    Return whether a process id currently exists.

    :param pid: Process id to probe, e.g. ``12345``.
    :returns: ``True`` when the process exists.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _pause_between_external_polls(interval_s: float) -> None:
    """
    Wait briefly between bounded checks of external process state.

    :param interval_s: Pause length in seconds, e.g. ``0.25``.
    """
    _POLL_PAUSE.wait(interval_s)


def _stop_host_daemon(home: Path) -> None:
    """
    Stop the connect daemon recorded under an isolated test HOME.

    ``omnigent run --server`` leaves the daemon alive after the REPL exits
    by design. E2E tests use per-test HOME directories so they clean
    those daemon processes up explicitly.

    :param home: HOME directory used by a REPL subprocess.
    """
    pid_path = home / ".omnigent" / "host.pid"
    if not pid_path.exists():
        return
    try:
        pid = int(pid_path.read_text().splitlines()[0])
    except (IndexError, ValueError):
        pid_path.unlink(missing_ok=True)
        return
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            break
        _pause_between_external_polls(0.1)
    if _pid_alive(pid):
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
    pid_path.unlink(missing_ok=True)


@dataclass(frozen=True)
class _ServerHandle:
    """
    Live standalone Omnigent server used by ``--server`` e2e tests.

    :param base_url: Local server URL, e.g. ``"http://127.0.0.1:8123"``.
    :param proc: Server subprocess.
    :param db_path: SQLite database path.
    :param log_path: Captured server stdout/stderr log path.
    """

    base_url: str
    proc: subprocess.Popen[bytes]
    db_path: Path
    log_path: Path


def _free_port() -> int:
    """
    Reserve and release a localhost TCP port.

    :returns: A currently free port number.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _write_marker_agent(tmp_path: Path, name: str, marker: str) -> Path:
    """
    Write a minimal agent YAML that replies with a fixed marker.

    :param tmp_path: Per-test temp directory.
    :param name: Agent name, e.g. ``"session_lifecycle"``.
    :param marker: Literal response marker expected in the REPL.
    :returns: Path to the generated YAML.
    """
    yaml_path = tmp_path / f"{name}.yaml"
    yaml_path.write_text(
        f"name: {name}\n"
        "executor:\n"
        f"  harness: {_HARNESS}\n"
        f"  model: {_MODEL}\n"
        "prompt: |\n"
        f"  Reply with exactly the literal string {marker} and nothing else.\n",
    )
    return yaml_path


def _repl_env(
    base_env: dict[str, str],
    home: Path,
    mock_llm_server_url: str,
) -> dict[str, str]:
    """
    Build a subprocess environment for REPL lifecycle tests.

    :param base_env: Fixture-provided mock credentials environment.
    :param home: Isolated HOME for persistent session files.
    :param mock_llm_server_url: Mock server base URL.
    :returns: Environment dict for ``pexpect.spawn``.
    """
    env = dict(base_env)
    env["HOME"] = str(home)
    env["TERM"] = "xterm-256color"
    env["LINES"] = "40"
    env["COLUMNS"] = "120"
    env["PROMPT_TOOLKIT_NO_CPR"] = "1"
    env["OMNIGENT_SESSIONS_ADAPTER_DEBUG"] = "1"
    # Localhost test servers do not need auth, but setting the
    # remote-token env forces the CLI's --server runner path to use
    # token-bound runner ids, matching the Databricks Apps shape.
    env["OMNIGENT_REMOTE_AUTH_TOKEN"] = _LOCAL_REMOTE_AUTH_TOKEN
    # Ensure mock LLM base URL is set.
    env["OPENAI_BASE_URL"] = f"{mock_llm_server_url}/v1"
    env["OPENAI_API_KEY"] = "mock-key"
    return ensure_repl_test_theme_env(env)


def _spawn_run(
    omnigent_python: Path,
    repo_root: Path,
    yaml_path: Path,
    env: dict[str, str],
    *,
    server_url: str | None = None,
    session_id: str | None = None,
    no_session: bool = True,
) -> pexpect.spawn:
    """
    Spawn ``omnigent run`` under a real PTY.

    :param omnigent_python: Python interpreter with Omnigent installed.
    :param repo_root: Checkout root used as subprocess cwd.
    :param yaml_path: Agent YAML path.
    :param env: Subprocess environment.
    :param server_url: Optional Omnigent server URL for ``--server`` mode.
    :param session_id: Optional session id for resume.
    :param no_session: When true, pass ``--no-session``.
    :returns: A live pexpect child.
    """
    args = [
        "-m",
        "omnigent",
        "run",
        str(yaml_path),
        "--model",
        _MODEL,
        "--harness",
        _HARNESS,
    ]
    if server_url is not None:
        args.extend(["--server", server_url])
    elif no_session:
        args.append("--no-session")
    if session_id is not None:
        # Resume a prior conversation by id. The flag was renamed
        # --session -> -r/--resume in the current CLI.
        args.extend(["--resume", session_id])
    return pexpect.spawn(
        str(omnigent_python),
        args,
        env=env,
        cwd=str(repo_root),
        encoding="utf-8",
        codec_errors="replace",
        timeout=_TURN_TIMEOUT,
        dimensions=(40, 120),
    )


def _wait_ready(child: pexpect.spawn) -> None:
    """
    Wait until the REPL is ready for input.

    :param child: Live pexpect child.
    """
    child.expect([STATE_SLEEPING, PROMPT_READY], timeout=_READY_TIMEOUT)


def _session_runner_id(base_url: str, session_id: str) -> str:
    """
    Fetch the runner id currently bound to a session.

    :param base_url: Omnigent server URL.
    :param session_id: Session id.
    :returns: Bound runner id.
    :raises AssertionError: If the session is missing or unbound.
    """
    response = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=5.0)
    response.raise_for_status()
    runner_id = response.json().get("runner_id")
    if not isinstance(runner_id, str) or not runner_id:
        raise AssertionError(f"session {session_id} has no runner_id: {response.text}")
    return runner_id


def _wait_session_runner_online(
    base_url: str,
    session_id: str,
    *,
    previous_runner_id: str | None = None,
    timeout: float = 60.0,
) -> str:
    """
    Poll until a session has an online runner, optionally a new one.

    :param base_url: Omnigent server URL.
    :param session_id: Session id.
    :param previous_runner_id: Optional stale runner id that must be
        replaced before returning.
    :param timeout: Max seconds to wait.
    :returns: Online runner id.
    :raises AssertionError: If no matching online runner appears.
    """
    deadline = time.monotonic() + timeout
    last_runner_id: str | None = None
    while time.monotonic() < deadline:
        with contextlib.suppress(httpx.HTTPError, AssertionError):
            runner_id = _session_runner_id(base_url, session_id)
            last_runner_id = runner_id
            if previous_runner_id is not None and runner_id == previous_runner_id:
                _pause_between_external_polls(0.25)
                continue
            status = httpx.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=5.0)
            if status.status_code == 200 and status.json().get("online") is True:
                return runner_id
        _pause_between_external_polls(0.25)
    raise AssertionError(
        f"session {session_id} did not get an online runner within {timeout}s; "
        f"last_runner_id={last_runner_id!r}",
    )


def _newest_session_id(base_url: str, agent_name: str) -> str:
    """
    Resolve the most recent session id for *agent_name* via the API.

    The sessions-adapter debug log emits ``session created``/``resuming
    existing session`` ids, but in the ``--server``/daemon flow those
    fire once at STARTUP (before :func:`_wait_ready` returns) and never
    re-appear on a turn — so scraping them from the PTY races. The
    server's session list is the robust source of truth instead.

    :param base_url: Omnigent server URL.
    :param agent_name: Agent display name to resolve.
    :returns: The newest session id, e.g. ``"conv_..."``.
    :raises AssertionError: When no session exists for the agent.
    """
    resp = httpx.get(
        f"{base_url}/v1/sessions",
        params={"agent_name": agent_name, "limit": 1},
        timeout=5.0,
    )
    resp.raise_for_status()
    data = resp.json().get("data", [])
    if not data:
        raise AssertionError(f"no session found for agent {agent_name!r}")
    return str(data[0]["id"])


def _drive_turn(
    child: pexpect.spawn,
    marker: str,
    mock_llm_server_url: str,
    *,
    base_url: str | None = None,
    agent_name: str | None = None,
) -> _LifecycleResult:
    """
    Configure a mock response for *marker*, submit a prompt, drive one
    turn to completion, and resolve the session + runner ids.

    Two modes, by ``base_url``:

    * **Local** (``base_url is None``): the session is created *on the
      turn*, so the sessions-adapter debug markers (``POST /v1/sessions``
      / ``session created`` / ``runner bound``) render during the turn
      and the ids are parsed from the PTY.
    * **``--server``/daemon** (``base_url`` set): the session is
      created/resumed at STARTUP (before :func:`_wait_ready` returns), so
      those markers fire once at boot and never re-appear on the turn
      (#523). Sync on the assistant *marker* and read the ids from the
      server API instead — robust to that timing.

    :param child: Live REPL process.
    :param marker: Literal assistant marker expected in the PTY.
    :param mock_llm_server_url: Mock server URL for configuring queues.
    :param base_url: Omnigent server URL for the ``--server`` flow;
        ``None`` for the local flow.
    :param agent_name: Agent display name used to resolve the session
        in the ``--server`` flow (required when ``base_url`` is set).
    :returns: Captured session and runner ids.
    """
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": marker}],
        key=_MODEL,
    )
    submit_prompt(child, "respond with the configured marker")

    if base_url is not None:
        # --server/daemon flow: ids come from the API (see docstring).
        if agent_name is None:
            raise AssertionError("agent_name is required when base_url is set")
        child.expect(re.escape(marker), timeout=_TURN_TIMEOUT)
        child.expect([STATE_SLEEPING, PROMPT_READY], timeout=60)
        session_id = _newest_session_id(base_url, agent_name)
        runner_id = _wait_session_runner_online(base_url, session_id)
        return _LifecycleResult(session_id=session_id, runner_id=runner_id)

    # Local flow: parse the lifecycle markers rendered during the turn.
    lifecycle_branch = child.expect(
        [
            r"POST /v1/sessions multipart bundle",
            r"resuming existing session id='([^']+)'",
        ],
        timeout=60,
    )
    if lifecycle_branch == 0:
        child.expect(r"session created id='([^']+)'", timeout=60)
        session_id = str(child.match.group(1))
    else:
        session_id = str(child.match.group(1))
    marker_seen = False
    runner_id: str | None = None
    branch = child.expect(
        [
            rf"PATCH /v1/sessions/{re.escape(session_id)} runner_id='([^']+)'",
            r"runner bound id='([^']+)'",
            re.escape(marker),
        ],
        timeout=_TURN_TIMEOUT,
    )
    if branch == 0:
        requested_runner_id = str(child.match.group(1))
        child.expect(r"runner bound id='([^']+)'", timeout=60)
        runner_id = str(child.match.group(1))
        assert runner_id == requested_runner_id
    elif branch == 1:
        runner_id = str(child.match.group(1))
    else:
        marker_seen = True
    if runner_id is None:
        raise AssertionError("runner id was not rendered in the local turn")
    if not marker_seen:
        child.expect(re.escape(marker), timeout=_TURN_TIMEOUT)
    child.expect([STATE_SLEEPING, PROMPT_READY], timeout=60)
    return _LifecycleResult(session_id=session_id, runner_id=runner_id)


def _runner_pid_from_daemon_log(home: Path, runner_id: str) -> int:
    """
    Resolve a runner subprocess pid from the connect-daemon log.

    The daemon logs ``Launched runner <id> for workspace <ws> (pid=<N>)``
    when it spawns a runner (omnigent/host/connect.py). Reading the pid
    from that line is robust across environments — unlike walking the
    daemon's process tree, which assumes the runner is a process-tree
    descendant of the daemon. That holds locally but NOT under CI's
    container/daemon model, where the tree walk yields "No runner
    subprocess found under <pid>" (the same CI-process-model gap that
    keeps ``local_mode_launches_runner_subprocess`` quarantined).

    :param home: Isolated HOME for the REPL/daemon under test.
    :param runner_id: Runner id whose pid to resolve, e.g.
        ``"runner_token_abc123"``.
    :returns: The runner subprocess pid.
    :raises AssertionError: When the pid is not found in the daemon log.
    """
    log_dir = home / ".omnigent" / "logs" / "host-daemon"
    logs = sorted(log_dir.glob("daemon-*.log"))
    if not logs:
        raise AssertionError(f"no connect-daemon log under {log_dir}")
    text = "".join(p.read_text(errors="replace") for p in logs)
    matches = re.findall(
        rf"Launched runner {re.escape(runner_id)}\b.*?\(pid=(\d+)\)",
        text,
    )
    if not matches:
        raise AssertionError(
            f"runner {runner_id!r} launch pid not found in daemon log under {log_dir}"
        )
    return int(matches[-1])


def _wait_http_ready(base_url: str, proc: subprocess.Popen[bytes], log_path: Path) -> None:
    """
    Wait for a standalone server to answer health-like traffic.

    :param base_url: Server base URL.
    :param proc: Server subprocess.
    :param log_path: Server log path for failure diagnostics.
    :raises AssertionError: If the server does not become ready.
    """
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(
                f"server exited early with code {proc.returncode}.\n"
                f"Log:\n{log_path.read_text(errors='replace')[-4000:]}",
            )
        with contextlib.suppress(httpx.HTTPError):
            response = httpx.get(f"{base_url}/health", timeout=2.0)
            if response.status_code == 200:
                return
        _pause_between_external_polls(0.25)
    raise AssertionError(
        f"server did not become ready at {base_url}.\n"
        f"Log:\n{log_path.read_text(errors='replace')[-4000:]}",
    )


def _server_entrypoint() -> str:
    """
    Return a Python entrypoint for a remote-style Omnigent server.

    :returns: Python source passed to ``python -c``.
    """
    return """
import os
from pathlib import Path

import uvicorn

from omnigent.cli import _create_artifact_store
from omnigent.runtime import init as init_runtime
from omnigent.runtime.agent_cache import AgentCache
from omnigent.runtime.caps import RuntimeCaps
from omnigent.server.app import create_app
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_store import HostStore
db_uri = os.environ["OMNIGENT_E2E_DB_URI"]
artifact_location = Path(os.environ["OMNIGENT_E2E_ARTIFACT_LOCATION"])
port = int(os.environ["OMNIGENT_E2E_PORT"])

agent_store = SqlAlchemyAgentStore(db_uri)
file_store = SqlAlchemyFileStore(db_uri)
conversation_store = SqlAlchemyConversationStore(db_uri)
comment_store = SqlAlchemyCommentStore(db_uri)
host_store = HostStore(db_uri)
artifact_store = _create_artifact_store(str(artifact_location))
agent_cache = AgentCache(
    artifact_store=artifact_store,
    cache_dir=artifact_location / ".cache",
)
init_runtime(
    conversation_store=conversation_store,
    agent_store=agent_store,
    agent_cache=agent_cache,
    file_store=file_store,
    artifact_store=artifact_store,
    caps=RuntimeCaps(execution_timeout=7200),
)
app = create_app(
    agent_store=agent_store,
    file_store=file_store,
    conversation_store=conversation_store,
    comment_store=comment_store,
    artifact_store=artifact_store,
    agent_cache=agent_cache,
    runner_tunnel_tokens=None,
    host_store=host_store,
)
uvicorn.run(app, host="127.0.0.1", port=port)
"""


@contextlib.contextmanager
def _running_server(
    omnigent_python: Path,
    repo_root: Path,
    env: dict[str, str],
    tmp_path: Path,
) -> Iterator[_ServerHandle]:
    """
    Run a remote-style Omnigent server for ``--server`` tests.

    :param omnigent_python: Python interpreter with Omnigent installed.
    :param repo_root: Checkout root used as subprocess cwd.
    :param env: Subprocess environment.
    :param tmp_path: Per-test temp directory.
    :yields: The server handle.
    """
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    db_path = tmp_path / "server.db"
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    log_path = tmp_path / "server.log"
    server_env = {
        **env,
        "OMNIGENT_E2E_DB_URI": f"sqlite:///{db_path}",
        "OMNIGENT_E2E_ARTIFACT_LOCATION": str(artifacts),
        "OMNIGENT_E2E_PORT": str(port),
    }
    with log_path.open("wb") as log_fh:
        proc = subprocess.Popen(
            [
                str(omnigent_python),
                "-c",
                _server_entrypoint(),
            ],
            env=server_env,
            cwd=str(repo_root),
            stdout=log_fh,
            stderr=log_fh,
            start_new_session=True,
        )
    try:
        _wait_http_ready(base_url, proc, log_path)
        yield _ServerHandle(
            base_url=base_url,
            proc=proc,
            db_path=db_path,
            log_path=log_path,
        )
    finally:
        if proc.poll() is None:
            proc.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=10)
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


@contextlib.contextmanager
def _registered_runner(
    base_url: str,
    repo_root: Path,
    yaml_path: Path,
    tmp_path: Path,
    *,
    extra_env: dict[str, str] | None = None,
) -> Iterator[str]:
    """
    Register one runner against a remote-style test server.

    :param base_url: Omnigent server URL.
    :param repo_root: Workspace root exposed to runner-local tools.
    :param yaml_path: Spec path to prewarm on the runner.
    :param tmp_path: Per-test temporary directory.
    :param extra_env: Optional extra environment variables for the
        runner subprocess, e.g. mock LLM credentials.
    :yields: Registered runner id.
    """
    from omnigent.cli import _start_cli_runner_process, _stop_cli_runner_process

    runner = _start_cli_runner_process(
        server_url=base_url,
        workspace_cwd=repo_root,
        capture_logs=True,
        log_dir=tmp_path / "logs",
        prewarm_spec_path=yaml_path,
        extra_env=extra_env,
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if runner.proc.poll() is not None:
            raise AssertionError(
                f"runner exited early with code {runner.proc.returncode}",
            )
        with contextlib.suppress(httpx.HTTPError):
            response = httpx.get(
                f"{base_url}/v1/runners/{runner.runner_id}/status",
                timeout=2.0,
            )
            if response.status_code == 200 and response.json()["online"] is True:
                break
        _pause_between_external_polls(0.25)
    else:
        raise AssertionError(f"runner {runner.runner_id} did not register")

    try:
        yield runner.runner_id
    finally:
        _stop_cli_runner_process(runner.proc, grace_timeout=1.0)


def test_repl_full_session_lifecycle(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """
    REPL creates, binds, streams, and exits through ``/v1/sessions``.

    Uses the mock LLM server for deterministic marker responses.

    :param omnigent_python: Python interpreter fixture.
    :param omnigent_repo_root: Repository root fixture.
    :param mock_credentials_env: Mock-LLM credential environment fixture.
    :param mock_llm_server_url: Mock server URL.
    :param tmp_path: Per-test temp directory.
    """
    home = tmp_path / "home"
    yaml_path = _write_marker_agent(tmp_path, "repl_session_lifecycle", "SESSION_LIFECYCLE_OK")
    env = _repl_env(mock_credentials_env, home, mock_llm_server_url)
    child = _spawn_run(
        omnigent_python,
        omnigent_repo_root,
        yaml_path,
        env,
    )
    try:
        _wait_ready(child)
        result = _drive_turn(child, "SESSION_LIFECYCLE_OK", mock_llm_server_url)
        assert result.session_id.startswith("conv_")
        assert result.runner_id.startswith("runner_")

        submit_prompt(child, "/history")
        child.expect(re.escape("SESSION_LIFECYCLE_OK"), timeout=20)
        child.expect([STATE_SLEEPING, PROMPT_READY], timeout=20)

        submit_prompt(child, "/switch")
        child.expect("Switch to", timeout=20)
        child.expect(re.escape(result.session_id), timeout=20)
        child.expect("/switch <#> or <id> to resume", timeout=20)
        child.expect([STATE_SLEEPING, PROMPT_READY], timeout=20)

        submit_prompt(child, "/switch 1")
        child.expect("Resumed conversation", timeout=20)
        child.expect(re.escape("SESSION_LIFECYCLE_OK"), timeout=20)
        child.expect([STATE_SLEEPING, PROMPT_READY], timeout=20)

        clean_exit(child, timeout=_EXIT_TIMEOUT)
    finally:
        child.close(force=True)
        _stop_host_daemon(home)


def test_repl_resume_reuses_daemon_runner(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """
    ``--server --session`` resumes with the daemon-owned runner.

    Uses the mock LLM server for deterministic marker responses.

    :param omnigent_python: Python interpreter fixture.
    :param omnigent_repo_root: Repository root fixture.
    :param mock_credentials_env: Mock-LLM credential environment fixture.
    :param mock_llm_server_url: Mock server URL.
    :param tmp_path: Per-test temp directory.
    """
    first_home = tmp_path / "home-first"
    second_home = tmp_path / "home-second"
    first_env = _repl_env(mock_credentials_env, first_home, mock_llm_server_url)
    second_env = _repl_env(mock_credentials_env, second_home, mock_llm_server_url)
    yaml_path = _write_marker_agent(tmp_path, "repl_session_resume", "SESSION_RESUME_OK")
    try:
        with _running_server(
            omnigent_python,
            omnigent_repo_root,
            first_env,
            tmp_path,
        ) as server:
            first = _spawn_run(
                omnigent_python,
                omnigent_repo_root,
                yaml_path,
                first_env,
                server_url=server.base_url,
            )
            try:
                _wait_ready(first)
                first_result = _drive_turn(
                    first,
                    "SESSION_RESUME_OK",
                    mock_llm_server_url,
                    base_url=server.base_url,
                    agent_name="repl_session_resume",
                )
                clean_exit(first, timeout=_EXIT_TIMEOUT)
            finally:
                first.close(force=True)

            second = _spawn_run(
                omnigent_python,
                omnigent_repo_root,
                yaml_path,
                second_env,
                server_url=server.base_url,
                session_id=first_result.session_id,
            )
            try:
                _wait_ready(second)
                second_result = _drive_turn(
                    second,
                    "SESSION_RESUME_OK",
                    mock_llm_server_url,
                    base_url=server.base_url,
                    agent_name="repl_session_resume",
                )
                assert second_result.session_id == first_result.session_id
                assert second_result.runner_id == first_result.runner_id
                clean_exit(second, timeout=_EXIT_TIMEOUT)
            finally:
                second.close(force=True)
    finally:
        _stop_host_daemon(first_home)
        _stop_host_daemon(second_home)


def test_repl_recover_after_runner_death(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """
    ``--server`` auto-relaunches a killed daemon-owned runner.

    Uses the mock LLM server for deterministic marker responses.

    :param omnigent_python: Python interpreter fixture.
    :param omnigent_repo_root: Repository root fixture.
    :param mock_credentials_env: Mock-LLM credential environment fixture.
    :param mock_llm_server_url: Mock server URL.
    :param tmp_path: Per-test temp directory.
    """
    home = tmp_path / "home"
    env = _repl_env(mock_credentials_env, home, mock_llm_server_url)
    yaml_path = _write_marker_agent(tmp_path, "repl_session_recover", "SESSION_RECOVER_OK")
    try:
        with _running_server(omnigent_python, omnigent_repo_root, env, tmp_path) as server:
            child = _spawn_run(
                omnigent_python,
                omnigent_repo_root,
                yaml_path,
                env,
                server_url=server.base_url,
            )
            try:
                _wait_ready(child)
                first_result = _drive_turn(
                    child,
                    "SESSION_RECOVER_OK",
                    mock_llm_server_url,
                    base_url=server.base_url,
                    agent_name="repl_session_recover",
                )
                runner_pid = _runner_pid_from_daemon_log(home, first_result.runner_id)
                os.kill(runner_pid, signal.SIGKILL)

                configure_mock_llm(
                    mock_llm_server_url,
                    [{"text": "SESSION_RECOVER_OK"}],
                    key=_MODEL,
                )
                submit_prompt(child, "respond with the configured marker again")
                child.expect(re.escape("SESSION_RECOVER_OK"), timeout=_TURN_TIMEOUT)
                child.expect([STATE_SLEEPING, PROMPT_READY], timeout=60)
                recovered_runner_id = _wait_session_runner_online(
                    server.base_url,
                    first_result.session_id,
                    previous_runner_id=first_result.runner_id,
                )
                assert recovered_runner_id != first_result.runner_id
                clean_exit(child, timeout=_EXIT_TIMEOUT)
            finally:
                child.close(force=True)
    finally:
        _stop_host_daemon(home)


@pytest.mark.asyncio
async def test_repl_reasoning_effort_threads_through(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """
    Session creation with ``reasoning_effort`` completes a mock turn.

    Uses the mock LLM server to verify that create-time session metadata
    works and does not break dispatch.

    :param omnigent_python: Python interpreter fixture.
    :param omnigent_repo_root: Repository root fixture.
    :param mock_credentials_env: Mock-LLM credential environment fixture.
    :param mock_llm_server_url: Mock server URL.
    :param tmp_path: Per-test temp directory.
    """
    env = _repl_env(mock_credentials_env, tmp_path / "home", mock_llm_server_url)
    yaml_path = _write_marker_agent(
        tmp_path,
        "repl_session_reasoning_effort",
        "SESSION_REASONING_OK",
    )
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "SESSION_REASONING_OK"}],
        key=_MODEL,
    )
    with _running_server(omnigent_python, omnigent_repo_root, env, tmp_path) as server:
        from omnigent.cli import _bundle

        bundle = _bundle(yaml_path)
        with _registered_runner(
            server.base_url,
            omnigent_repo_root,
            yaml_path,
            tmp_path,
            extra_env={k: env[k] for k in ("OPENAI_BASE_URL", "OPENAI_API_KEY") if k in env},
        ) as runner_id:
            async with OmnigentClient(base_url=server.base_url) as client:
                created = await client.sessions.create(bundle, reasoning_effort="high")
                assert created.reasoning_effort == "high"
                bound = await client.sessions.bind_runner(
                    created.id,
                    runner_id=runner_id,
                )
                assert bound.runner_id == runner_id
                session_files = client.files.for_session(bound.id)
                chat = SessionsChat(
                    namespace=client.sessions,
                    files_uploader=session_files.upload,
                    files_getter=session_files.get,
                    session=bound,
                )
                result = await chat.query("respond with the configured marker")
                assert "SESSION_REASONING_OK" in result.text
                refreshed = await client.sessions.get(created.id)
                assert refreshed.reasoning_effort == "high"

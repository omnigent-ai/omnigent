"""E2E coverage for the Alpha sessions-API REPL lifecycle (mock LLM).

Migrated to mock LLM: uses canned responses for all LLM turns
so the tests are deterministic and need no Databricks credentials.

These tests run real ``omnigent`` subprocesses under pexpect. They
exercise the user-visible flow: session creation, runner binding,
streaming text rendering, resume, and runner recovery.
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
from collections.abc import Generator, Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pexpect
import pytest

from tests.e2e.conftest import configure_mock_llm, reset_mock_llm
from tests.e2e.omnigent._pexpect_harness import (
    PROMPT_READY,
    STATE_SLEEPING,
    clean_exit,
    ensure_repl_test_theme_env,
    submit_prompt,
)

_MODEL = "mock-session-lifecycle-model"
_HARNESS = "openai-agents"
_LOCAL_REMOTE_AUTH_TOKEN = "local-e2e-runner-token"
_READY_TIMEOUT = 90.0
_TURN_TIMEOUT = 120.0
_EXIT_TIMEOUT = 20.0
_RUNNER_CMD_MARKER = "omnigent.runner._entry"
_POLL_PAUSE = threading.Event()


@dataclass(frozen=True)
class _LifecycleResult:
    """Captured identifiers from a sessions-API REPL turn."""

    session_id: str
    runner_id: str


def _pid_alive(pid: int) -> bool:
    """Return whether a process id currently exists."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _pause_between_external_polls(interval_s: float) -> None:
    """Wait briefly between bounded checks of external process state."""
    _POLL_PAUSE.wait(interval_s)


def _host_daemon_pid(home: Path) -> int:
    """Return the connect daemon pid recorded under an isolated test HOME."""
    pid_path = home / ".omnigent" / "host.pid"
    try:
        first_line = pid_path.read_text().splitlines()[0]
        return int(first_line)
    except (IndexError, OSError, ValueError) as exc:
        raise AssertionError(f"Missing or invalid daemon pidfile at {pid_path}") from exc


def _stop_host_daemon(home: Path) -> None:
    """Stop the connect daemon recorded under an isolated test HOME."""
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
    """Live standalone Omnigent server."""

    base_url: str
    proc: subprocess.Popen[bytes]
    db_path: Path
    log_path: Path


def _free_port() -> int:
    """Reserve and release a localhost TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _write_marker_agent(tmp_path: Path, name: str, marker: str) -> Path:
    """Write a minimal agent YAML that replies with a fixed marker."""
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


def _repl_env(base_env: dict[str, str], home: Path) -> dict[str, str]:
    """Build a subprocess environment for REPL lifecycle tests."""
    env = dict(base_env)
    env["HOME"] = str(home)
    env["TERM"] = "xterm-256color"
    env["LINES"] = "40"
    env["COLUMNS"] = "120"
    env["PROMPT_TOOLKIT_NO_CPR"] = "1"
    env["OMNIGENT_SESSIONS_ADAPTER_DEBUG"] = "1"
    env["OMNIGENT_REMOTE_AUTH_TOKEN"] = _LOCAL_REMOTE_AUTH_TOKEN
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
    """Spawn ``omnigent run`` under a real PTY."""
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
        args.extend(["--session", session_id])
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
    """Wait until the REPL is ready for input."""
    child.expect([STATE_SLEEPING, PROMPT_READY], timeout=_READY_TIMEOUT)


def _session_runner_id(base_url: str, session_id: str) -> str:
    """Fetch the runner id currently bound to a session."""
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
    """Poll until a session has an online runner, optionally a new one."""
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


def _drive_turn(
    child: pexpect.spawn,
    marker: str,
    *,
    base_url: str | None = None,
) -> _LifecycleResult:
    """Submit one prompt and assert the sessions API lifecycle renders."""
    submit_prompt(child, "respond with the configured marker")
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
        if base_url is None:
            raise AssertionError(
                "runner id was not rendered; pass base_url for daemon-prebound sessions",
            )
        runner_id = _wait_session_runner_online(base_url, session_id)
    if not marker_seen:
        child.expect(re.escape(marker), timeout=_TURN_TIMEOUT)
    child.expect([STATE_SLEEPING, PROMPT_READY], timeout=60)
    return _LifecycleResult(session_id=session_id, runner_id=runner_id)


def _wait_session_reasoning_effort(
    base_url: str,
    session_id: str,
    expected: str | None,
) -> None:
    """Poll a session snapshot until reasoning effort matches."""
    deadline = time.monotonic() + 20
    last_body: dict[str, object] | None = None
    while time.monotonic() < deadline:
        response = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=5.0)
        if response.status_code == 200:
            body = response.json()
            if isinstance(body, dict):
                last_body = body
                if body.get("reasoning_effort") == expected:
                    return
        _pause_between_external_polls(0.25)
    raise AssertionError(
        f"session {session_id} reasoning_effort did not become {expected!r}; "
        f"last snapshot={last_body!r}",
    )


def _descendant_processes(root_pid: int) -> dict[int, tuple[int, str]]:
    """Return descendants of a process keyed by pid."""
    proc = subprocess.run(
        ["ps", "-eo", "pid=,ppid=,command="],
        capture_output=True,
        text=True,
        check=True,
    )
    all_processes: dict[int, tuple[int, str]] = {}
    children_by_parent: dict[int, list[int]] = {}
    for raw_line in proc.stdout.splitlines():
        parts = raw_line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        pid = int(parts[0])
        ppid = int(parts[1])
        command = parts[2]
        all_processes[pid] = (ppid, command)
        children_by_parent.setdefault(ppid, []).append(pid)

    descendants: dict[int, tuple[int, str]] = {}
    stack = list(children_by_parent.get(root_pid, []))
    while stack:
        pid = stack.pop()
        process = all_processes.get(pid)
        if process is None:
            continue
        descendants[pid] = process
        stack.extend(children_by_parent.get(pid, []))
    return descendants


def _find_runner_pid(root_pid: int, *, timeout: float = 15.0) -> int:
    """Find the runner subprocess below a REPL process, polling until found.

    The runner subprocess is spawned asynchronously after the REPL is ready,
    so we poll with a timeout rather than failing immediately.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        descendants = _descendant_processes(root_pid)
        for pid, (_ppid, command) in descendants.items():
            if _RUNNER_CMD_MARKER in command:
                return pid
        time.sleep(0.2)
    descendants = _descendant_processes(root_pid)
    formatted = "\n".join(
        f"{pid} <- {ppid}: {command}" for pid, (ppid, command) in sorted(descendants.items())
    )
    raise AssertionError(
        f"No runner subprocess found under {root_pid} within {timeout}s.\n{formatted}"
    )


def _wait_http_ready(base_url: str, proc: subprocess.Popen[bytes], log_path: Path) -> None:
    """Wait for a standalone server to answer health-like traffic."""
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
    """Return a Python entrypoint for a remote-style Omnigent server."""
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
db_uri = os.environ["OMNIGENT_E2E_DB_URI"]
artifact_location = Path(os.environ["OMNIGENT_E2E_ARTIFACT_LOCATION"])
port = int(os.environ["OMNIGENT_E2E_PORT"])

agent_store = SqlAlchemyAgentStore(db_uri)
file_store = SqlAlchemyFileStore(db_uri)
conversation_store = SqlAlchemyConversationStore(db_uri)
comment_store = SqlAlchemyCommentStore(db_uri)
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
)
uvicorn.run(app, host="127.0.0.1", port=port)
"""


@contextlib.contextmanager
def _running_server(
    omnigent_python: Path,
    repo_root: Path,
    env: dict[str, str],
    tmp_path: Path,
) -> Generator[_ServerHandle]:
    """Run a remote-style Omnigent server for ``--server`` tests."""
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


def test_repl_local_mode_launches_runner_subprocess(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """Local ``omnigent run`` starts a REPL session with a bound runner.

    Verifies that a full turn completes and the session/runner IDs are
    assigned. The subprocess tree check was removed because CI process
    isolation prevents reliable detection of grandchild processes.
    """
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "LOCAL_RUNNER_SUBPROCESS_OK"}],
        key=_MODEL,
    )

    yaml_path = _write_marker_agent(
        tmp_path,
        "repl_local_runner_subprocess",
        "LOCAL_RUNNER_SUBPROCESS_OK",
    )
    child = _spawn_run(
        omnigent_python,
        omnigent_repo_root,
        yaml_path,
        _repl_env(mock_credentials_env, tmp_path / "home"),
    )
    try:
        _wait_ready(child)

        result = _drive_turn(child, "LOCAL_RUNNER_SUBPROCESS_OK")
        assert result.session_id.startswith("conv_")
        assert result.runner_id.startswith("runner_")

        clean_exit(child, timeout=_EXIT_TIMEOUT)
    finally:
        child.close(force=True)


@contextlib.contextmanager
def _registered_runner(
    base_url: str,
    repo_root: Path,
    yaml_path: Path,
    tmp_path: Path,
) -> Iterator[str]:
    """
    Register one runner against a remote-style test server.

    Used by direct SDK e2e coverage where there is no REPL process to
    launch the runner.

    :param base_url: Omnigent server URL, e.g. ``"http://127.0.0.1:8123"``.
    :param repo_root: Workspace root exposed to runner-local tools.
    :param yaml_path: Spec path to prewarm on the runner.
    :param tmp_path: Per-test temporary directory used for captured
        runner logs.
    :yields: Registered runner id.
    """
    from omnigent.cli import _start_cli_runner_process, _stop_cli_runner_process

    runner = _start_cli_runner_process(
        server_url=base_url,
        workspace_cwd=repo_root,
        capture_logs=True,
        log_dir=tmp_path / "logs",
        prewarm_spec_path=yaml_path,
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
        # Runner registration arrives over the websocket tunnel from
        # another process, so the test polls the public status API.
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
    """REPL creates, binds, streams, and exits through ``/v1/sessions``."""
    reset_mock_llm(mock_llm_server_url)
    # Three turns: initial turn, /history (no LLM), /switch recall
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "SESSION_LIFECYCLE_OK"}],
        key=_MODEL,
    )

    home = tmp_path / "home"
    yaml_path = _write_marker_agent(tmp_path, "repl_session_lifecycle", "SESSION_LIFECYCLE_OK")
    env = _repl_env(mock_credentials_env, home)
    child = _spawn_run(
        omnigent_python,
        omnigent_repo_root,
        yaml_path,
        env,
    )
    try:
        _wait_ready(child)
        result = _drive_turn(child, "SESSION_LIFECYCLE_OK")
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
    """``--server --session`` resumes with the daemon-owned runner."""
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [
            {"text": "SESSION_RESUME_OK"},
            {"text": "SESSION_RESUME_OK"},
        ],
        key=_MODEL,
    )

    first_home = tmp_path / "home-first"
    second_home = tmp_path / "home-second"
    first_env = _repl_env(mock_credentials_env, first_home)
    second_env = _repl_env(mock_credentials_env, second_home)
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
                    base_url=server.base_url,
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
                    base_url=server.base_url,
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
    """``--server`` auto-relaunches a killed daemon-owned runner."""
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [
            {"text": "SESSION_RECOVER_OK"},
            {"text": "SESSION_RECOVER_OK"},
        ],
        key=_MODEL,
    )

    home = tmp_path / "home"
    env = _repl_env(mock_credentials_env, home)
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
                    base_url=server.base_url,
                )
                runner_pid = _find_runner_pid(_host_daemon_pid(home))
                os.kill(runner_pid, signal.SIGKILL)

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


def test_repl_effort_command_persists_session_metadata(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """``/effort`` writes create-time and PATCH-time session metadata."""
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "SESSION_EFFORT_OK"}],
        key=_MODEL,
    )

    home = tmp_path / "home"
    env = _repl_env(mock_credentials_env, home)
    yaml_path = _write_marker_agent(tmp_path, "repl_session_effort", "SESSION_EFFORT_OK")
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

                submit_prompt(child, "/effort high")
                child.expect("reasoning effort set to high", timeout=20)
                child.expect([STATE_SLEEPING, PROMPT_READY], timeout=20)

                result = _drive_turn(child, "SESSION_EFFORT_OK", base_url=server.base_url)
                _wait_session_reasoning_effort(server.base_url, result.session_id, "high")

                submit_prompt(child, "/effort medium")
                child.expect("reasoning effort set to medium", timeout=20)
                child.expect([STATE_SLEEPING, PROMPT_READY], timeout=20)
                _wait_session_reasoning_effort(server.base_url, result.session_id, "medium")

                submit_prompt(child, "/effort default")
                child.expect("reasoning effort reset to agent default", timeout=20)
                child.expect([STATE_SLEEPING, PROMPT_READY], timeout=20)
                _wait_session_reasoning_effort(server.base_url, result.session_id, None)

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
    """Session creation with ``reasoning_effort`` completes a real turn."""
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "SESSION_REASONING_OK"}],
        key=_MODEL,
    )

    env = _repl_env(mock_credentials_env, tmp_path / "home")
    yaml_path = _write_marker_agent(
        tmp_path,
        "repl_session_reasoning_effort",
        "SESSION_REASONING_OK",
    )
    with _running_server(omnigent_python, omnigent_repo_root, env, tmp_path) as server:
        from omnigent.cli import _bundle, _start_cli_runner_process, _stop_cli_runner_process

        bundle = _bundle(yaml_path)

        runner = _start_cli_runner_process(
            server_url=server.base_url,
            workspace_cwd=omnigent_repo_root,
            capture_logs=True,
            log_dir=tmp_path / "logs",
            prewarm_spec_path=yaml_path,
            extra_env={k: env[k] for k in ("OPENAI_BASE_URL", "OPENAI_API_KEY") if k in env},
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if runner.proc.poll() is not None:
                raise AssertionError(
                    f"runner exited early with code {runner.proc.returncode}",
                )
            with contextlib.suppress(httpx.HTTPError):
                response = httpx.get(
                    f"{server.base_url}/v1/runners/{runner.runner_id}/status",
                    timeout=2.0,
                )
                if response.status_code == 200 and response.json()["online"] is True:
                    break
            _pause_between_external_polls(0.25)
        else:
            raise AssertionError(f"runner {runner.runner_id} did not register")

        try:
            from omnigent_client import OmnigentClient, SessionsChat

            async with OmnigentClient(base_url=server.base_url) as client:
                created = await client.sessions.create(bundle, reasoning_effort="high")
                assert created.reasoning_effort == "high"
                bound = await client.sessions.bind_runner(
                    created.id,
                    runner_id=runner.runner_id,
                )
                assert bound.runner_id == runner.runner_id
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
        finally:
            _stop_cli_runner_process(runner.proc, grace_timeout=1.0)

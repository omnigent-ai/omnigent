"""E2E regression: a hard runner exit + delete must not leak the codex tree."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx
import psutil
import pytest
import yaml

from omnigent.harnesses.codex_native.main import _find_codex_cli
from omnigent.native.native_coding_agents import CODEX_NATIVE_AGENT_NAME
from omnigent.process_logging import PROCESS_LOG_FILE_ENV_VAR
from tests._helpers.compat import apply_runner_env, compat_runner_cwd, runner_executable
from tests.e2e.conftest import POLL_INTERVAL_S, configure_mock_llm
from tests.e2e.test_host_e2e import (
    _pid_alive,
    _runner_pid_from_daemon_log,
    _wait_for_host_online,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]

_HARD_EXIT_SETTLE_S = 25.0
_REAP_AFTER_DELETE_S = 150.0


@dataclass
class _SpawnedDaemon:
    proc: subprocess.Popen[bytes]
    host_id: str
    daemon_log: Path


def _spawn_host_daemon(
    *, tmp_path: Path, live_server: str, mock_llm_server_url: str
) -> _SpawnedDaemon:
    """Spawn an isolated ``omnigent host`` daemon for this test."""
    omni_dir = tmp_path / ".omnigent"
    omni_dir.mkdir(parents=True, exist_ok=True)
    host_id = uuid.uuid4().hex
    host_name = f"e2e-host-{uuid.uuid4().hex[:12]}"
    (omni_dir / "config.yaml").write_text(
        yaml.safe_dump({"host": {"host_id": host_id, "name": host_name}}, sort_keys=True)
    )
    daemon_log = tmp_path / "host-daemon.log"
    pythonpath = os.pathsep.join(
        [
            str(_REPO_ROOT),
            str(_REPO_ROOT / "sdks" / "python-client"),
            str(_REPO_ROOT / "sdks" / "ui"),
            os.environ.get("PYTHONPATH", ""),
        ]
    )
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
        "OPENAI_API_KEY": "mock-key",
        "PYTHONPATH": pythonpath,
        PROCESS_LOG_FILE_ENV_VAR: str(daemon_log),
    }
    with open(daemon_log, "w") as log_fh:
        proc = subprocess.Popen(
            [runner_executable(), "-m", "omnigent.host._daemon_entry", "--server", live_server],
            env=apply_runner_env(env),
            cwd=compat_runner_cwd(),
            stdout=log_fh,
            stderr=log_fh,
        )
    return _SpawnedDaemon(proc=proc, host_id=host_id, daemon_log=daemon_log)


def _seed_fake_codex_auth(home_dir: Path) -> None:
    """Give the runner a codex ``auth.json`` so the app-server boots."""
    codex_home = home_dir / ".codex"
    codex_home.mkdir(parents=True, exist_ok=True)
    (codex_home / "auth.json").write_text(
        json.dumps({"OPENAI_API_KEY": "mock-key"}), encoding="utf-8"
    )


def _state_dir_for(home_dir: Path, session_id: str) -> Path:
    """The session's codex-native state dir under the runner's HOME."""
    bare = session_id.removeprefix("conv_")
    digest = hashlib.sha256(bare.encode("utf-8")).hexdigest()[:32]
    return home_dir / ".omnigent" / "codex-native" / digest


def _codex_tree_pids(state_dir: Path) -> dict[int, str]:
    """Live processes whose argv names *state_dir* -- the session's tree."""
    needle = str(state_dir)
    found: dict[int, str] = {}
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmdline = proc.info["cmdline"] or []
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        joined = " ".join(cmdline)
        if needle in joined:
            found[proc.info["pid"]] = joined
    return found


def _kill_pids(pids: list[int]) -> None:
    """Best-effort SIGKILL a set of pids and their children."""
    for pid in pids:
        with contextlib.suppress(psutil.NoSuchProcess):
            proc = psutil.Process(pid)
            for child in proc.children(recursive=True):
                with contextlib.suppress(psutil.NoSuchProcess):
                    child.kill()
            proc.kill()


def _codex_native_agent_id(client: httpx.Client) -> str:
    """Return the durable id of the auto-registered ``codex-native-ui``."""
    resp = client.get("/v1/agents")
    resp.raise_for_status()
    for agent in resp.json()["data"]:
        if agent["name"] == CODEX_NATIVE_AGENT_NAME:
            return str(agent["id"])
    raise AssertionError(f"{CODEX_NATIVE_AGENT_NAME!r} not registered on the server")


@pytest.mark.skipif(
    _find_codex_cli() is None or shutil.which("tmux") is None,
    reason="needs the codex CLI (harness-resolved) and tmux for the native TUI",
)
@pytest.mark.timeout(600)
def test_hard_runner_exit_and_delete_reap_codex_app_server(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
    mock_llm_server_url: str,
) -> None:
    """A hard runner exit + session delete must not orphan the codex tree."""
    configure_mock_llm(mock_llm_server_url, [{"text": "CODEX_OK"}])
    _seed_fake_codex_auth(tmp_path)

    workspace = tmp_path / "project"
    workspace.mkdir()

    daemon = _spawn_host_daemon(
        tmp_path=tmp_path,
        live_server=live_server,
        mock_llm_server_url=mock_llm_server_url,
    )
    session_id: str | None = None
    state_dir: Path | None = None
    try:
        _wait_for_host_online(http_client, daemon.host_id, timeout=45.0)
        agent_id = _codex_native_agent_id(http_client)

        create = http_client.post(
            "/v1/sessions",
            json={
                "agent_id": agent_id,
                "host_id": daemon.host_id,
                "workspace": str(workspace),
            },
            timeout=90.0,
        )
        create.raise_for_status()
        session_id = create.json()["id"]
        state_dir = _state_dir_for(tmp_path, session_id)

        http_client.post(
            f"/v1/sessions/{session_id}/events",
            json={
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "boot the app-server"}],
                },
            },
            timeout=120.0,
        )

        # Wait for the runner's codex app-server tree to come up for this session.
        deadline = time.monotonic() + 180.0
        tree: dict[int, str] = {}
        while time.monotonic() < deadline:
            tree = _codex_tree_pids(state_dir)
            if tree:
                break
            time.sleep(POLL_INTERVAL_S)
        assert tree, (
            f"codex app-server never came up for session {session_id} "
            f"(state dir {state_dir}) -- cannot exercise the hard-exit journey"
        )

        runner_pid = _runner_pid_from_daemon_log(daemon.daemon_log)
        assert runner_pid is not None, "host daemon never logged a runner launch"
        assert _pid_alive(runner_pid), f"runner (pid={runner_pid}) died before the hard exit"

        os.kill(runner_pid, signal.SIGKILL)

        # Let the daemon reap the dead runner and the tree settle as an orphan.
        settle = time.monotonic() + _HARD_EXIT_SETTLE_S
        while time.monotonic() < settle and _pid_alive(runner_pid):
            time.sleep(POLL_INTERVAL_S)
        time.sleep(3.0)

        http_client.delete(f"/v1/sessions/{session_id}", timeout=60.0).raise_for_status()

        reap_deadline = time.monotonic() + _REAP_AFTER_DELETE_S
        remaining = _codex_tree_pids(state_dir)
        while time.monotonic() < reap_deadline:
            remaining = _codex_tree_pids(state_dir)
            if not remaining:
                break
            time.sleep(1.0)

        assert not remaining, (
            f"the codex app-server tree for session {session_id} is still "
            f"running {_REAP_AFTER_DELETE_S:.0f}s after a hard runner exit and "
            f"DELETE. Leaked pids -> argv: {remaining}"
        )
    finally:
        if state_dir is not None:
            _kill_pids(list(_codex_tree_pids(state_dir)))
        if session_id is not None:
            with contextlib.suppress(httpx.HTTPError):
                http_client.delete(f"/v1/sessions/{session_id}", timeout=30.0)
        if daemon.proc.poll() is None:
            daemon.proc.send_signal(signal.SIGTERM)
            try:
                daemon.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                daemon.proc.kill()
                daemon.proc.wait()

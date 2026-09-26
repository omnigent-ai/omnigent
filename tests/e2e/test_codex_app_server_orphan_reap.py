"""Regression e2e: codex app-server leaks on unclean runner death."""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path

import httpx
import pytest

from omnigent.entities.session_resources import terminal_resource_id
from omnigent.native.native_coding_agents import CODEX_NATIVE_AGENT_NAME
from omnigent.process_logging import PROCESS_LOG_FILE_ENV_VAR
from tests._helpers.compat import apply_runner_env, compat_runner_cwd, runner_executable
from tests.e2e.helpers import POLL_INTERVAL_S

_TAG_ARG_PREFIX = "omnigent_crash_teardown_tag="

_ORPHAN_REAP_GRACE_S = 150.0


def _spawn_host_daemon(
    *, tmp_path: Path, live_server: str
) -> tuple[subprocess.Popen[bytes], Path]:
    """Spawn an ``omnigent host`` daemon whose log captures runner PIDs."""
    repo_root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    sdk_roots = [repo_root, repo_root / "sdks" / "python-client", repo_root / "sdks" / "ui"]
    env["PYTHONPATH"] = os.pathsep.join(
        [str(p) for p in sdk_roots] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
    )
    daemon_log = tmp_path / "host-daemon.log"
    env[PROCESS_LOG_FILE_ENV_VAR] = str(daemon_log)
    with open(daemon_log, "w") as log_fh:
        proc = subprocess.Popen(
            [
                runner_executable(),
                "-m",
                "omnigent.host._daemon_entry",
                "--server",
                live_server,
            ],
            env=apply_runner_env(env),
            cwd=compat_runner_cwd(),
            stdout=subprocess.DEVNULL,
            stderr=log_fh,
        )
    return proc, daemon_log


def _online_host_id(client: httpx.Client, timeout: float = 30.0) -> str:
    """Poll ``GET /v1/hosts`` until at least one host is online."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get("/v1/hosts")
        if resp.status_code == 200:
            online = [h for h in resp.json().get("hosts", []) if h["status"] == "online"]
            if online:
                return str(online[0]["host_id"])
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(f"No host came online within {timeout}s")


def _codex_native_agent_id(client: httpx.Client) -> str:
    """Return the durable id of the auto-registered ``codex-native-ui``."""
    resp = client.get("/v1/agents")
    resp.raise_for_status()
    for agent in resp.json()["data"]:
        if agent["name"] == CODEX_NATIVE_AGENT_NAME:
            return str(agent["id"])
    raise AssertionError(f"{CODEX_NATIVE_AGENT_NAME!r} not registered on the server")


def _runner_pid_from_daemon_log(log_path: Path) -> int | None:
    """Parse the launched runner's PID from the host daemon's log."""
    if not log_path.exists():
        return None
    match = re.search(
        r"Launched runner \S+ for workspace .*? \(pid=(\d+)\)",
        log_path.read_text(),
    )
    return int(match.group(1)) if match else None


def _pid_alive(pid: int) -> bool:
    """Return whether a process id is currently alive (zombies excluded)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return stat.rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


def _find_codex_app_server_pids(workspace: Path) -> list[int]:
    """Find THIS session's live ``codex app-server`` process(es)."""
    resolved_workspace = workspace.resolve()
    tagged: list[int] = []
    untagged: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (
                (entry / "cmdline")
                .read_bytes()
                .replace(b"\x00", b" ")
                .decode("utf-8", errors="replace")
            )
            cwd = Path(os.readlink(entry / "cwd")).resolve()
        except OSError:
            continue
        if "app-server" not in cmdline or cwd != resolved_workspace:
            continue
        if _TAG_ARG_PREFIX in cmdline:
            tagged.append(int(entry.name))
        else:
            untagged.append(int(entry.name))
    return tagged or untagged


def _live_group_member_pids(pgid: int) -> list[int]:
    """Find live processes whose process group is *pgid* (zombies excluded)."""
    members: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
        except OSError:
            continue
        rest = stat.rsplit(")", 1)[1].split()
        if len(rest) < 3 or rest[0] == "Z":
            continue
        if int(rest[2]) == pgid:
            members.append(int(entry.name))
    return members


def _poll_for_terminal_resource(
    client: httpx.Client,
    *,
    session_id: str,
    resource_id: str,
    timeout: float,
) -> None:
    """Poll session resources until the codex terminal is registered."""
    deadline = time.monotonic() + timeout
    last_seen: list[object] = []
    while time.monotonic() < deadline:
        resp = client.get(f"/v1/sessions/{session_id}/resources")
        if resp.status_code == 200:
            data = resp.json().get("data", [])
            last_seen = [r.get("id") for r in data]
            for resource in data:
                if resource.get("id") == resource_id and resource.get("type") == "terminal":
                    return
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(
        f"Terminal resource {resource_id!r} never appeared for session "
        f"{session_id} within {timeout}s; saw {last_seen!r}."
    )


@pytest.mark.skipif(
    os.environ.get("OMNIGENT_E2E_CODEX_NATIVE") != "1" or shutil.which("codex") is None,
    reason=(
        "codex app-server orphan e2e needs `codex` on PATH and OMNIGENT_E2E_CODEX_NATIVE=1 to run"
    ),
)
@pytest.mark.timeout(600)
def test_unclean_runner_death_reaps_codex_app_server(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
) -> None:
    """An uncleanly-dead runner must not leave its codex app-server behind."""
    daemon, daemon_log = _spawn_host_daemon(tmp_path=tmp_path, live_server=live_server)
    app_server_pids: list[int] = []
    try:
        host_id = _online_host_id(http_client, timeout=60.0)
        agent_id = _codex_native_agent_id(http_client)

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        create = http_client.post(
            "/v1/sessions",
            json={"agent_id": agent_id, "host_id": host_id, "workspace": str(workspace)},
            timeout=60.0,
        )
        create.raise_for_status()
        session_id = create.json()["id"]

        _poll_for_terminal_resource(
            http_client,
            session_id=session_id,
            resource_id=terminal_resource_id("codex", "main"),
            timeout=120.0,
        )

        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            app_server_pids = _find_codex_app_server_pids(workspace)
            if app_server_pids:
                break
            time.sleep(POLL_INTERVAL_S)
        assert app_server_pids, (
            "No tagged `codex app-server` process appeared for the created "
            "codex-native session; cannot exercise the orphan scenario."
        )

        tree_pids = set(app_server_pids)
        for pid in app_server_pids:
            with contextlib.suppress(OSError):
                tree_pids.update(_live_group_member_pids(os.getpgid(pid)))

        runner_pid = _runner_pid_from_daemon_log(daemon_log)
        assert runner_pid is not None, (
            f"Could not parse the runner PID from the host daemon log at {daemon_log}"
        )

        # Unclean runner death: SIGKILL skips _stop_pm and every teardown path.
        os.kill(runner_pid, signal.SIGKILL)
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline and _pid_alive(runner_pid):
            time.sleep(POLL_INTERVAL_S)
        assert not _pid_alive(runner_pid), f"Runner {runner_pid} survived SIGKILL"

        deadline = time.monotonic() + _ORPHAN_REAP_GRACE_S
        while time.monotonic() < deadline:
            leaked = [pid for pid in tree_pids if _pid_alive(pid)]
            if not leaked:
                break
            time.sleep(1.0)
        leaked = [pid for pid in tree_pids if _pid_alive(pid)]
        assert not leaked, (
            f"codex app-server process(es) {leaked} are still alive "
            f"{_ORPHAN_REAP_GRACE_S:.0f}s after the runner (pid={runner_pid}) "
            "was SIGKILLed. The unclean runner death leaked the app-server "
            "and its MCP bridge children: nothing reaps them "
            "until the NEXT runner launch runs crash reconciliation."
        )
    finally:
        # Clean up anything the bug (or a partial run) left behind.
        for pid in app_server_pids:
            if _pid_alive(pid):
                try:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                except OSError:
                    with contextlib.suppress(OSError):
                        os.kill(pid, signal.SIGKILL)
        daemon.terminate()
        try:
            daemon.wait(timeout=10)
        except subprocess.TimeoutExpired:
            daemon.kill()
            daemon.wait(timeout=10)

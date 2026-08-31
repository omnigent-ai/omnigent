"""Regression e2e: codex app-server leaks on unclean runner death.

Journey: connect an omnigent host -> create a codex-native session bound to
that host (the runner spawns a ``codex app-server`` subprocess in its own
process group, plus its serve-mcp bridge child) -> the runner dies uncleanly
(SIGKILL, so its graceful ``_stop_pm`` teardown never runs) -> the app-server
and its MCP bridge children survive, re-parented away from the dead runner,
and are never reaped while the host stays up (crash reconciliation only runs
on the NEXT runner launch).

The regression assertion: after an unclean runner death, the session's codex
app-server process (group) must be cleaned up within a grace period. Before a
fix this test FAILS (the orphan lingers); after a fix it passes.

Run (opt-in, needs ``codex`` on PATH)::

    OMNIGENT_E2E_CODEX_NATIVE=1 \
    .venv/bin/python -m pytest tests/e2e/test_codex_app_server_orphan_reap.py -v
"""

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
from omnigent.native_coding_agents import CODEX_NATIVE_AGENT_NAME
from omnigent.process_logging import PROCESS_LOG_FILE_ENV_VAR
from tests._helpers.compat import apply_runner_env, compat_runner_cwd, runner_executable
from tests.e2e.helpers import POLL_INTERVAL_S

# The inert argv marker CodexNativeAppServer embeds in the app-server command
# line for crash-safe reconciliation (see codex_native_process_registry).
_TAG_ARG_PREFIX = "omnigent_crash_teardown_tag="

# How long the orphan check waits for the app-server to be cleaned up after
# the unclean runner death. The host's ownerless sweep runs on a 60s cadence
# (first pass one interval after boot), so the grace must cover a full cycle
# plus classification; the leak itself persists indefinitely, so a genuine
# regression still fails deterministically.
_ORPHAN_REAP_GRACE_S = 150.0


def _spawn_host_daemon(
    *, tmp_path: Path, live_server: str
) -> tuple[subprocess.Popen[bytes], Path]:
    """
    Spawn an ``omnigent host`` daemon whose log captures runner PIDs.

    :param tmp_path: Per-test temp dir for the daemon log.
    :param live_server: Test server URL.
    :returns: The spawned daemon subprocess handle and its log path.
    """
    repo_root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{repo_root}{os.pathsep}{env.get('PYTHONPATH', '')}"
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
    """
    Poll ``GET /v1/hosts`` until at least one host is online.

    :param client: HTTP client pointed at the test server.
    :param timeout: Max seconds to wait.
    :returns: The online host's ``host_id``.
    :raises AssertionError: If no host comes online within *timeout*.
    """
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
    """
    Return the durable id of the auto-registered ``codex-native-ui``.

    :param client: HTTP client pointed at the test server.
    :returns: The ``"ag_..."`` id for ``codex-native-ui``.
    :raises AssertionError: If the server did not auto-register it.
    """
    resp = client.get("/v1/agents")
    resp.raise_for_status()
    for agent in resp.json()["data"]:
        if agent["name"] == CODEX_NATIVE_AGENT_NAME:
            return str(agent["id"])
    raise AssertionError(f"{CODEX_NATIVE_AGENT_NAME!r} not registered on the server")


def _runner_pid_from_daemon_log(log_path: Path) -> int | None:
    """
    Parse the launched runner's PID from the host daemon's log.

    :param log_path: Path to the captured daemon stderr log.
    :returns: The runner subprocess PID, or ``None`` if not present yet.
    """
    if not log_path.exists():
        return None
    match = re.search(
        r"Launched runner \S+ for workspace .*? \(pid=(\d+)\)",
        log_path.read_text(),
    )
    return int(match.group(1)) if match else None


def _pid_alive(pid: int) -> bool:
    """
    Return whether a process id is currently alive (zombies excluded).

    :param pid: Process id to probe.
    :returns: ``True`` if the process exists and is not a zombie.
    """
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
    """
    Find THIS session's live ``codex app-server`` process(es).

    Every host-spawned codex-native app-server embeds a unique
    ``omnigent_crash_teardown_tag=...`` marker in its command line and runs
    with the session workspace as its cwd. Matching both keys the scan to
    exactly this test's session, so a parallel opt-in run (which uses its
    own tmp workspace) can never be picked up — or killed — by this test.

    :param workspace: The session workspace the app-server was launched in.
    :returns: PIDs of this session's live tagged app-server processes.
    """
    resolved_workspace = workspace.resolve()
    pids: list[int] = []
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
        if _TAG_ARG_PREFIX in cmdline and "app-server" in cmdline and cwd == resolved_workspace:
            pids.append(int(entry.name))
    return pids


def _live_group_member_pids(pgid: int) -> list[int]:
    """
    Find live processes whose process group is *pgid* (zombies excluded).

    The app-server leads its own group (spawned with ``start_new_session``),
    and its MCP bridge children live in that group — so the group roster is
    the full tree the sweep must reap.

    :param pgid: Process group id to scan for.
    :returns: PIDs of live members of the group.
    """
    members: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
        except OSError:
            continue
        # Field 5 (after the comm field, which may contain spaces) is the pgid;
        # field 3 is the state.
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
    """
    Poll session resources until the codex terminal is registered.

    :param client: HTTP client pointed at the test server.
    :param session_id: Session/conversation id.
    :param resource_id: Expected terminal resource id.
    :param timeout: Max seconds to wait.
    :raises AssertionError: If the resource never appears within *timeout*.
    """
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
    """An uncleanly-dead runner must not leave its codex app-server behind.

    Journey: host online -> codex-native session created on that host (the
    runner spawns a tagged ``codex app-server`` in its own process group) ->
    the runner is SIGKILLed (unclean death: no ``_stop_pm``, no per-session
    teardown) -> the app-server (and its MCP bridge children) must be cleaned
    up within a grace period.

    Before the fix the app-server survives indefinitely (re-parented away
    from the dead runner; crash reconciliation only runs on the NEXT runner
    launch, which never happens while the session stays dead), so this test
    fails on the leak. After a fix it passes.
    """
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

        # The auto-create registers the Codex TUI terminal only after the
        # app-server is up, so this doubles as the app-server-ready wait.
        _poll_for_terminal_resource(
            http_client,
            session_id=session_id,
            resource_id=terminal_resource_id("codex", "main"),
            timeout=120.0,
        )

        # Identify this session's app-server by tag + workspace cwd, so a
        # parallel run's app-server can never be matched (or killed) here.
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

        # The whole tree the sweep must reap: the app-server's process group
        # also holds its MCP bridge children (`serve-mcp`), so snapshot the
        # group roster to assert THEY are cleaned up too, not just the leader.
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

        # Regression assertion: the app-server AND its group (the MCP bridge
        # children) must not outlive the runner's unclean death for long.
        # Before the fix they linger indefinitely.
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

"""E2E regression: idle runner terminals must not keep fork+exec'ing tmux.

Journey, all real processes: a host daemon connects to the live server, a
runner is launched on that host for a session, the agent opens three ``bash``
terminals with ``sys_terminal_launch`` in one turn, then everything sits idle.
The host daemon's PATH starts with a logging ``tmux`` shim (forwarded to the
runner through the host->runner env allowlist), so every tmux fork+exec the
runner performs appends one ledger line. The assertion is the runner's tmux
spawn rate per idle terminal over a fixed window.

    .venv/bin/python -m pytest tests/e2e/test_terminal_idle_watcher_fork_storm.py -v
"""

from __future__ import annotations

import contextlib
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import uuid
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from tests.e2e.conftest import (
    configure_mock_llm,
    lookup_agent_id,
    poll_session_until_terminal,
    register_inline_agent,
    reset_mock_llm,
    send_user_message_to_session,
)
from tests.e2e.test_host_e2e import _spawn_host_daemon, _SpawnedHostDaemon, _wait_for_host_online

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux") or shutil.which("tmux") is None,
    reason="idle-watcher fork-rate e2e needs Linux /proc accounting and tmux on PATH",
)

TERMINAL_SESSION_KEYS = ("s0", "s1", "s2")
RUNNER_ONLINE_TIMEOUT_S = 60.0
TURN_TIMEOUT_S = 180.0
IDLE_SETTLE_S = 2.0
IDLE_WINDOW_S = 12.0
# A watcher that backs off once a pane is quiet stays far below this; a fixed
# 1 s tick that spawns tmux twice measures ~2 per second per terminal.
MAX_IDLE_SPAWNS_PER_S_PER_TERMINAL = 0.6

_RUNNER_PID_RE = re.compile(
    r"Launched runner \S+ for workspace .*? \(pid=(\d+)\)|runner pid=(\d+)"
)


@dataclass(frozen=True)
class TmuxSpawn:
    """One ``tmux`` fork+exec recorded by the shim."""

    at: float
    args: tuple[str, ...]

    @property
    def socket(self) -> str | None:
        """The ``-S <socket>`` that identifies the terminal, if present."""
        for index, token in enumerate(self.args[:-1]):
            if token == "-S":
                return self.args[index + 1]
        return None

    @property
    def subcommand(self) -> str:
        """The tmux subcommand (``capture-pane``, ``list-panes``, ...)."""
        skip_next = False
        for token in self.args:
            if skip_next:
                skip_next = False
                continue
            if token in ("-S", "-f", "-L"):
                skip_next = True
                continue
            if not token.startswith("-"):
                return token
        return "?"


@dataclass(frozen=True)
class IdleSpawnSample:
    """tmux spawns the runner made for the session's terminals while idle."""

    window_s: float
    spawns: tuple[TmuxSpawn, ...]
    runner_cpu_s: float | None

    @property
    def per_socket(self) -> Counter[str]:
        return Counter(spawn.socket or "?" for spawn in self.spawns)

    @property
    def per_subcommand(self) -> Counter[str]:
        return Counter(spawn.subcommand for spawn in self.spawns)

    def rate_per_terminal(self, terminal_count: int) -> float:
        return len(self.spawns) / self.window_s / terminal_count


@dataclass
class IdleTerminalsJourney:
    """Handles for a session whose runner owns idle terminals."""

    daemon: _SpawnedHostDaemon
    session_id: str
    runner_id: str
    runner_pid: int | None
    ledger: Path
    terminals: dict[str, str]

    @property
    def sockets(self) -> tuple[str, ...]:
        return tuple(self.terminals.values())

    def close(self) -> None:
        self.daemon.proc.send_signal(signal.SIGTERM)
        try:
            self.daemon.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.daemon.proc.kill()
            self.daemon.proc.wait()
        for socket in self.sockets:
            with contextlib.suppress(Exception):
                subprocess.run(
                    ["tmux", "-S", socket, "kill-server"],
                    capture_output=True,
                    timeout=5,
                    check=False,
                )


def write_tmux_shim(bin_dir: Path, ledger: Path) -> None:
    """Install a ``tmux`` shim in *bin_dir* that logs each invocation to *ledger*."""
    real_tmux = shutil.which("tmux")
    assert real_tmux is not None
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "tmux"
    shim.write_text(
        "#!/bin/sh\n"
        f'printf \'%s %s\\n\' "$(date +%s.%N)" "$*" >> {shlex.quote(str(ledger))}\n'
        f'exec {shlex.quote(real_tmux)} "$@"\n'
    )
    shim.chmod(0o755)


def read_ledger(ledger: Path) -> list[TmuxSpawn]:
    if not ledger.exists():
        return []
    spawns: list[TmuxSpawn] = []
    for line in ledger.read_text().splitlines():
        stamp, _, rest = line.partition(" ")
        try:
            at = float(stamp)
        except ValueError:
            continue
        spawns.append(TmuxSpawn(at=at, args=tuple(rest.split())))
    return spawns


@contextlib.contextmanager
def _path_prefixed(bin_dir: Path) -> Iterator[None]:
    original = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{bin_dir}{os.pathsep}{original}"
    try:
        yield
    finally:
        os.environ["PATH"] = original


def _runner_pid(daemon_log: Path) -> int | None:
    if not daemon_log.exists():
        return None
    match = _RUNNER_PID_RE.search(daemon_log.read_text())
    if match is None:
        return None
    return int(match.group(1) or match.group(2))


def _cpu_seconds(pid: int) -> float | None:
    if not sys.platform.startswith("linux"):
        return None
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    fields = stat[stat.rindex(")") + 2 :].split()
    return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")


def _wait_runner_online(client: httpx.Client, runner_id: str) -> None:
    deadline = time.monotonic() + RUNNER_ONLINE_TIMEOUT_S
    while time.monotonic() < deadline:
        resp = client.get(f"/v1/runners/{runner_id}/status")
        if resp.status_code == 200 and resp.json().get("online") is True:
            return
        time.sleep(0.5)
    raise AssertionError(f"runner {runner_id} never came online on the host")


def _live_terminals(client: httpx.Client, session_id: str) -> dict[str, str]:
    """Map each live terminal resource name (``bash:s0``) to its tmux socket path."""
    resp = client.get(f"/v1/sessions/{session_id}/resources")
    resp.raise_for_status()
    return {
        str(resource["name"]): str(resource["metadata"]["tmux_socket"])
        for resource in resp.json()["data"]
        if resource.get("type") == "terminal" and resource["metadata"].get("running", True)
    }


def open_idle_terminals(
    client: httpx.Client,
    *,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path: Path,
    spawn_daemon: Callable[..., _SpawnedHostDaemon] = _spawn_host_daemon,
    shim: bool = True,
) -> IdleTerminalsJourney:
    """Bring a host online, launch a runner on it, and have the agent open three terminals.

    Returns once the terminal-opening turn has completed, i.e. the session is
    idle with the three agent-opened terminals (plus any the runner adds) live.
    With ``shim`` the host daemon's PATH starts with the logging tmux shim; a
    caller counting spawns another way (e.g. strace around ``spawn_daemon``)
    passes ``shim=False`` to keep the real PATH walk observable.
    """
    ledger = tmp_path / "tmux-spawns.log"
    shim_bin = tmp_path / "shim-bin"
    if shim:
        write_tmux_shim(shim_bin, ledger)
    with _path_prefixed(shim_bin) if shim else contextlib.nullcontext():
        daemon = spawn_daemon(
            tmp_path=tmp_path,
            live_server=live_server,
            mock_llm_server_url=mock_llm_server_url,
        )
    try:
        _wait_for_host_online(client, daemon.host_id, timeout=30.0)

        model = f"mock-idle-terminals-{uuid.uuid4().hex[:6]}"
        agent_name = register_inline_agent(
            client,
            name=f"idle-terminals-{uuid.uuid4().hex[:6]}",
            harness="openai-agents",
            model=model,
            profile="",
            prompt="You open bash terminals with sys_terminal_launch when asked.",
            mock_llm_base_url=f"{mock_llm_server_url}/v1",
            extra_config={
                "terminals": {
                    "bash": {
                        "command": "bash",
                        "os_env": {"type": "caller_process", "sandbox": {"type": "none"}},
                    }
                },
                "os_env": {"type": "caller_process", "cwd": ".", "sandbox": {"type": "none"}},
            },
        )
        resp = client.post("/v1/sessions", json={"agent_id": lookup_agent_id(client, agent_name)})
        resp.raise_for_status()
        session_id = str(resp.json()["id"])

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        launch = client.post(
            f"/v1/hosts/{daemon.host_id}/runners",
            json={"session_id": session_id, "workspace": str(workspace)},
            timeout=60.0,
        )
        assert launch.status_code == 200, (
            f"runner launch failed: {launch.status_code} {launch.text}"
        )
        runner_id = str(launch.json()["runner_id"])
        _wait_runner_online(client, runner_id)
        client.patch(
            f"/v1/sessions/{session_id}", json={"runner_id": runner_id}
        ).raise_for_status()

        reset_mock_llm(mock_llm_server_url)
        configure_mock_llm(
            mock_llm_server_url,
            [
                *(
                    {
                        "tool_calls": [
                            {
                                "call_id": f"call_launch_{key}",
                                "name": "sys_terminal_launch",
                                "arguments": f'{{"terminal": "bash", "session": "{key}"}}',
                            }
                        ]
                    }
                    for key in TERMINAL_SESSION_KEYS
                ),
                {"text": "Three bash terminals are open."},
            ],
            key=model,
        )
        response_id = send_user_message_to_session(
            client,
            session_id=session_id,
            content=f"Open {len(TERMINAL_SESSION_KEYS)} bash terminals and leave them idle.",
        )
        body = poll_session_until_terminal(
            client, session_id=session_id, response_id=response_id, timeout=TURN_TIMEOUT_S
        )
        assert body["status"] == "completed", f"terminal-opening turn failed: {body.get('error')}"
        terminals = _live_terminals(client, session_id)
        opened = {f"bash:{key}" for key in TERMINAL_SESSION_KEYS}
        assert opened <= terminals.keys(), f"agent-opened terminals missing: {terminals}"
    except BaseException:
        daemon.proc.kill()
        daemon.proc.wait()
        raise
    return IdleTerminalsJourney(
        daemon=daemon,
        session_id=session_id,
        runner_id=runner_id,
        runner_pid=_runner_pid(daemon.daemon_log),
        ledger=ledger,
        terminals=terminals,
    )


def sample_idle_spawns(
    journey: IdleTerminalsJourney, window_s: float = IDLE_WINDOW_S
) -> IdleSpawnSample:
    """Leave everything untouched for *window_s* and collect the runner's tmux spawns."""
    time.sleep(IDLE_SETTLE_S)
    cpu_before = _cpu_seconds(journey.runner_pid) if journey.runner_pid else None
    start = time.time()
    time.sleep(window_s)
    end = time.time()
    cpu_after = _cpu_seconds(journey.runner_pid) if journey.runner_pid else None
    spawns = tuple(
        spawn
        for spawn in read_ledger(journey.ledger)
        if start <= spawn.at <= end and spawn.socket in journey.sockets
    )
    cpu = None if cpu_before is None or cpu_after is None else cpu_after - cpu_before
    return IdleSpawnSample(window_s=end - start, spawns=spawns, runner_cpu_s=cpu)


def test_idle_terminals_do_not_fork_storm_tmux(
    live_server: str,
    http_client: httpx.Client,
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """Three idle bash terminals must not keep the runner spawning tmux every tick."""
    journey = open_idle_terminals(
        http_client,
        live_server=live_server,
        mock_llm_server_url=mock_llm_server_url,
        tmp_path=tmp_path,
    )
    try:
        sample = sample_idle_spawns(journey)
    finally:
        journey.close()

    assert sample.spawns, (
        "no tmux spawns recorded; shim/ledger instrumentation failed and the "
        "fork-rate assertion would otherwise pass vacuously"
    )
    rate = sample.rate_per_terminal(len(journey.terminals))
    socket_names = {socket: name for name, socket in journey.terminals.items()}
    per_terminal = {socket_names.get(s, s): n for s, n in sample.per_socket.items()}
    assert rate < MAX_IDLE_SPAWNS_PER_S_PER_TERMINAL, (
        f"runner fork+exec'd tmux {len(sample.spawns)} times in {sample.window_s:.1f}s while "
        f"{len(journey.terminals)} terminals sat idle ({rate:.2f}/s per terminal; limit "
        f"{MAX_IDLE_SPAWNS_PER_S_PER_TERMINAL}/s). per terminal: {per_terminal}; "
        f"per subcommand: {dict(sample.per_subcommand)}; runner cpu seconds: {sample.runner_cpu_s}"
    )

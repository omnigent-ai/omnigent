"""End-to-end guard: idle terminals must not spawn tmux subprocesses continuously."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest
import yaml

from omnigent.process_logging import PROCESS_LOG_FILE_ENV_VAR
from tests._helpers.compat import apply_runner_env, compat_runner_cwd, runner_executable
from tests.e2e.conftest import (
    configure_mock_llm,
    lookup_agent_id,
    poll_session_until_terminal,
    register_inline_agent,
    reset_mock_llm,
    send_user_message_to_session,
)
from tests.e2e.test_host_e2e import _wait_for_host_online

pytestmark = [
    pytest.mark.skipif(
        not sys.platform.startswith("linux"),
        reason="POSIX shell shim + /proc semantics; idle-watcher storm is a POSIX runner bug",
    ),
    pytest.mark.skipif(
        shutil.which("tmux") is None,
        reason="tmux not installed; terminal idle-watcher tests need tmux on PATH",
    ),
]

# Three watchers separate fixed polling from scheduler noise.
_N_TERMINALS = 3

# Fully-idle window sampled after the turn completes. Long enough to cover
# ~12 watcher wakeups of the 1s poll cadence per terminal.
_IDLE_WINDOW_S = 12.0

# Settle past the idle threshold and exclude launch-time tmux calls.
_POST_TURN_SETTLE_S = 12.0

# Reject both two-probe (~2/s) and folded fixed-rate (~1/s) watchers.
_MAX_PROBE_SPAWNS_PER_S_PER_TERMINAL = 0.6

# Worktree root (tests/e2e/<file> → parents[2]); forwarded to the runner so
# it imports this checkout's omnigent (see the daemon env comment below).
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _worktree_pythonpath() -> str:
    """PYTHONPATH for the host daemon (and thus its runners)."""
    entries = [str(_REPO_ROOT)]
    for entry in os.environ.get("PYTHONPATH", "").split(os.pathsep):
        if entry:
            entries.append(os.path.abspath(entry))
    return os.pathsep.join(entries)


def _write_tmux_shim(shim_dir: Path, log_path: Path) -> None:
    """Write a transparent ``tmux`` shim that logs every spawn."""
    real_tmux = shutil.which("tmux")
    assert real_tmux is not None  # guarded by pytestmark
    shim = shim_dir / "tmux"
    shim.write_text(
        "#!/bin/sh\n"
        f'printf \'%s %s\\n\' "$(date +%s.%N)" "$*" >> "{log_path}"\n'
        f'exec "{real_tmux}" "$@"\n'
    )
    shim.chmod(0o755)


def _probe_spawns_in_window(log_path: Path, start: float, end: float) -> list[str]:
    """Return watcher-probe tmux spawn records inside ``[start, end]``."""
    if not log_path.exists():
        return []
    matches: list[str] = []
    for line in log_path.read_text().splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        try:
            ts = float(parts[0])
        except ValueError:
            continue
        if start <= ts <= end and ("capture-pane" in parts[1] or "list-panes" in parts[1]):
            matches.append(line)
    return matches


def test_idle_terminals_do_not_spawn_tmux_continuously(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
    mock_llm_server_url: str,
) -> None:
    """A runner whose terminals are all idle must not sustain tmux fork+execs."""
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    spawn_log = tmp_path / "tmux_spawns.log"
    _write_tmux_shim(shim_dir, spawn_log)
    omni_dir = tmp_path / ".omnigent"
    omni_dir.mkdir(parents=True)
    host_id = uuid.uuid4().hex
    (omni_dir / "config.yaml").write_text(
        yaml.safe_dump(
            {"host": {"host_id": host_id, "name": f"e2e-fork-storm-{uuid.uuid4().hex[:12]}"}},
            default_flow_style=False,
            sort_keys=True,
        )
    )
    daemon_log = tmp_path / "host-daemon.log"
    daemon_base_env = os.environ.copy()
    # Ignore an operator's local kill switch when measuring this revision.
    daemon_base_env.pop("OMNIGENT_TERMINAL_IDLE_POLL_BACKOFF", None)
    env = apply_runner_env(
        {
            **daemon_base_env,
            "HOME": str(tmp_path),
            "PATH": f"{shim_dir}{os.pathsep}{os.environ['PATH']}",
            # The daemon forwards this checkout to runners launched from session cwd.
            "PYTHONPATH": _worktree_pythonpath(),
            PROCESS_LOG_FILE_ENV_VAR: str(daemon_log),
        }
    )
    with open(daemon_log, "w") as log_fh:
        daemon = subprocess.Popen(
            [runner_executable(), "-m", "omnigent.host._daemon_entry", "--server", live_server],
            env=env,
            cwd=compat_runner_cwd(),
            stdout=subprocess.DEVNULL,
            stderr=log_fh,
        )
    try:
        _wait_for_host_online(http_client, host_id, timeout=60.0)
        model = f"mock-fork-storm-{uuid.uuid4().hex[:6]}"
        reset_mock_llm(mock_llm_server_url)
        agent_name = register_inline_agent(
            http_client,
            name=f"fork-storm-{uuid.uuid4().hex[:6]}",
            harness="openai-agents",
            model=model,
            profile="",
            prompt=(
                "You are a terminal test assistant. Use sys_terminal_launch "
                "to open shell terminals when asked."
            ),
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
        agent_id = lookup_agent_id(http_client, agent_name)
        resp = http_client.post("/v1/sessions", json={"agent_id": agent_id})
        resp.raise_for_status()
        session_id = resp.json()["id"]

        launch_resp = http_client.post(
            f"/v1/hosts/{host_id}/runners",
            json={"session_id": session_id, "workspace": str(tmp_path)},
            timeout=60.0,
        )
        assert launch_resp.status_code == 200, (
            f"Runner launch failed: {launch_resp.status_code} {launch_resp.text}"
        )
        runner_id = launch_resp.json()["runner_id"]

        deadline = time.monotonic() + 60.0
        runner_online = False
        while time.monotonic() < deadline:
            status_resp = http_client.get(f"/v1/runners/{runner_id}/status")
            if status_resp.status_code == 200 and status_resp.json().get("online") is True:
                runner_online = True
                break
            time.sleep(0.5)
        assert runner_online, f"Runner {runner_id} never came online after launch"

        http_client.patch(
            f"/v1/sessions/{session_id}",
            json={"runner_id": runner_id},
        ).raise_for_status()
        launch_steps = [
            {
                "tool_calls": [
                    {
                        "call_id": f"call_launch_{i}",
                        "name": "sys_terminal_launch",
                        "arguments": f'{{"terminal": "bash", "session": "s{i}"}}',
                    }
                ],
            }
            for i in range(_N_TERMINALS)
        ]
        configure_mock_llm(
            mock_llm_server_url,
            [*launch_steps, {"text": f"Launched {_N_TERMINALS} terminals."}],
            key=model,
        )
        response_id = send_user_message_to_session(
            http_client,
            session_id=session_id,
            content=f"Open {_N_TERMINALS} bash terminals and confirm.",
        )
        result = poll_session_until_terminal(
            http_client,
            session_id=session_id,
            response_id=response_id,
            timeout=180,
        )
        assert result["status"] == "completed", (
            f"Terminal-launch turn failed: status={result['status']!r}, "
            f"error={result.get('error')!r}"
        )

        # The terminals must actually have been created through the shim,
        # otherwise a zero spawn count would vacuously pass.
        launches = [line for line in spawn_log.read_text().splitlines() if "new-session" in line]
        assert len(launches) >= _N_TERMINALS, (
            f"Expected >= {_N_TERMINALS} tmux new-session spawns through the shim, "
            f"saw {len(launches)}. The runner did not launch its terminals via the "
            f"shimmed PATH; spawn log:\n{spawn_log.read_text()[-2000:]}"
        )
        time.sleep(_POST_TURN_SETTLE_S)
        window_start = time.time()
        time.sleep(_IDLE_WINDOW_S)
        window_end = time.time()

        probes = _probe_spawns_in_window(spawn_log, window_start, window_end)
        per_terminal_rate = len(probes) / _IDLE_WINDOW_S / _N_TERMINALS
        limit = _MAX_PROBE_SPAWNS_PER_S_PER_TERMINAL
        assert per_terminal_rate < limit, (
            f"Idle terminals sustained {len(probes)} tmux watcher-probe fork+execs "
            f"(capture-pane/list-panes) over a {_IDLE_WINDOW_S:.0f}s fully-idle window "
            f"with {_N_TERMINALS} live terminals — {per_terminal_rate:.2f} spawns/s per "
            f"terminal (limit {limit}). The per-terminal idle watcher fork+execs tmux "
            f"on every poll tick with no quiescence backoff, so runner system time "
            f"scales with live-terminal count even when nothing is running. "
            f"First probes:\n" + "\n".join(probes[:6])
        )
    finally:
        daemon.send_signal(signal.SIGTERM)
        try:
            daemon.wait(timeout=10)
        except subprocess.TimeoutExpired:
            daemon.kill()
            daemon.wait(timeout=10)

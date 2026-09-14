"""End-to-end guard: idle terminals must not spawn tmux subprocesses continuously.

Native/tool-terminal idle-activity detection runs one watcher thread per
live terminal (``TerminalInstance._idle_watch_loop_threaded``). On the bug,
every poll tick performs TWO blocking ``subprocess.run`` fork+execs of tmux
— ``capture-pane`` (pane diff) plus ``list-panes`` (pane-dead probe) — at a
fixed cadence with no idle backoff and no teardown, so a runner with N live
terminals sustains ``2N`` tmux fork+execs per second **while completely
idle**. Under sub-agent fan-out (tens of terminals) this dominates runner
system time; ``fork()`` cost scales with the runner's RSS and thread count.

This drives the real user journey: connect a real host daemon, launch a
real runner on it, have the agent (mock LLM) launch three terminals via
``sys_terminal_launch``, let the turn complete so everything is idle, and
count how many tmux subprocesses the runner spawns during a fully-idle
window. The count is observed via a transparent ``tmux`` shim prepended to
the daemon's PATH (inherited by the runner): the shim appends a timestamped
argv line to a log, then ``exec``s the real tmux, so behavior is unchanged
while every spawn is recorded::

    .venv/bin/python -m pytest tests/e2e/test_terminal_idle_watcher_fork_storm.py -v

Measured on the bug (pre-backoff main): 3 idle terminals sustain ~2.0
watcher-tick tmux spawns per second per terminal (~72 in the 12s window).
The threshold allows up to 0.6/s/terminal so an adaptive-backoff or
pipe-pane/control-mode fix passes with margin while both the fixed-rate
two-calls-per-tick bug (~2.0/s) and a folded-but-unbacked-off poller
(~1.0/s) fail specifically.
"""

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

# Number of terminals the agent launches. The spawn stream is per-terminal
# (one watcher thread each), so a few terminals make the fixed-rate polling
# unambiguous against scheduler noise without needing real fan-out scale.
_N_TERMINALS = 3

# Fully-idle window sampled after the turn completes. Long enough to cover
# ~12 watcher wakeups of the 1s poll cadence per terminal.
_IDLE_WINDOW_S = 12.0

# Post-turn settle before sampling starts. Longer than the watcher's 10s
# idle threshold so the idle edge has fired for every terminal before the
# window opens — the window then samples pure steady-state cadence, not the
# pre-edge base-rate ramp. Also ages launch-time tmux calls (new-session,
# set-option, initial reads) out of the window.
_POST_TURN_SETTLE_S = 12.0

# Max watcher-probe tmux spawns per second per idle terminal. The bug
# sustains 2.0 (capture-pane + list-panes every 1s tick, no backoff); merely
# folding the two calls into one still measures ~1.0. A quiescence-aware
# implementation (post-idle backoff, pipe-pane, or control mode) measures
# well under 0.6 once the idle edge has fired, so this threshold rejects a
# fixed-rate poller even with the probe folded. Counts only
# capture-pane/list-panes argv lines, so unrelated tmux use cannot inflate it.
_MAX_PROBE_SPAWNS_PER_S_PER_TERMINAL = 0.6

# Worktree root (tests/e2e/<file> → parents[2]); forwarded to the runner so
# it imports this checkout's omnigent (see the daemon env comment below).
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _worktree_pythonpath() -> str:
    """PYTHONPATH for the host daemon (and thus its runners).

    Prepends the worktree root and absolutizes any existing entries —
    a relative entry (e.g. ``sdks/ui``) resolves against the *runner's*
    cwd (the session workspace), not the repo, and would silently drop.

    :returns: An ``os.pathsep``-joined PYTHONPATH string.
    """
    entries = [str(_REPO_ROOT)]
    for entry in os.environ.get("PYTHONPATH", "").split(os.pathsep):
        if entry:
            entries.append(os.path.abspath(entry))
    return os.pathsep.join(entries)


def _write_tmux_shim(shim_dir: Path, log_path: Path) -> None:
    """Write a transparent ``tmux`` shim that logs every spawn.

    The shim appends ``<epoch-seconds> <argv>`` to *log_path* and execs the
    real tmux binary, so tmux behavior is byte-identical while every
    fork+exec the runner performs becomes a countable log line.

    :param shim_dir: Directory to place the shim in (prepended to PATH).
    :param log_path: File the shim appends spawn records to.
    """
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
    """Return watcher-probe tmux spawn records inside ``[start, end]``.

    Only ``capture-pane`` / ``list-panes`` invocations count — those are the
    idle watcher's per-tick probes. Launch/teardown commands (new-session,
    set-option, kill-server, ...) are excluded so the assertion isolates the
    steady-state polling cost.

    :param log_path: The shim's spawn log.
    :param start: Window start (epoch seconds).
    :param end: Window end (epoch seconds).
    :returns: The matching raw log lines.
    """
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
    """A runner whose terminals are all idle must not sustain tmux fork+execs.

    Connect a host daemon whose PATH resolves tmux through a counting shim,
    launch a runner on it, drive one turn in which the agent launches
    ``_N_TERMINALS`` bash terminals, wait for the turn to complete, then
    count tmux watcher-probe spawns over a fully-idle window. On the bug the
    idle watchers spawn ~2 tmux subprocesses per second per terminal
    indefinitely; the assertion fails on that fixed-rate storm.
    """
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    spawn_log = tmp_path / "tmux_spawns.log"
    _write_tmux_shim(shim_dir, spawn_log)

    # ── Host daemon with the shim on PATH (runner inherits PATH) ──
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
    # The watcher's backoff kill switch must not leak in from the operator's
    # shell or CI — an ambient =0 would pin every watcher at base rate and
    # flip this test's verdict for reasons unrelated to the tree under test.
    daemon_base_env.pop("OMNIGENT_TERMINAL_IDLE_POLL_BACKOFF", None)
    env = apply_runner_env(
        {
            **daemon_base_env,
            "HOME": str(tmp_path),
            "PATH": f"{shim_dir}{os.pathsep}{os.environ['PATH']}",
            # The daemon forwards PYTHONPATH to host-spawned runners (it is
            # allowlisted in _RUNNER_ENV_ALLOWLIST), and those runners start
            # with ``python -P`` from the workspace cwd — so the worktree
            # must be on PYTHONPATH for the runner to import this checkout
            # (mirroring the live_server fixture's server env). Existing
            # entries are absolutized because the runner's cwd differs.
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

        # ── Agent with a plain bash terminal, wired to the mock LLM ──
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

        # ── Session bound to a runner launched ON the host ──
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

        # ── One turn: the agent launches N terminals, then replies ──
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

        # ── Fully-idle window: nothing runs, terminals just sit there ──
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

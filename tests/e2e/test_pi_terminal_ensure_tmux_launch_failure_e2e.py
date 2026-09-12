"""Pi terminal ensure fails -- ``tmux launch failed (rc=1): `` (empty reason).

The journey
-----------
A user launches a pi-native session (``omnigent pi``) on a host whose ``tmux``
passes the version preflight (``tmux -V`` answers a supported version) but
fails when the runner launches the Pi terminal (``new-session`` exits 1). The
Pi terminal never starts: the CLI dies with ``Pi terminal ensure failed
(500): Native Pi terminal failed to start; see the runner log ...`` and the
runner log records the tracked telemetry signature::

    ERROR ... runner.app ... | Pi terminal ensure failed for session=<id>
    ...
    RuntimeError: tmux launch failed (rc=1):

Note the EMPTY reason after ``rc=1):``. ``TerminalInstance.launch``
(``omnigent/inner/terminal.py``) pipes tmux stdout to ``DEVNULL`` and surfaces
only stderr in the raised ``RuntimeError``, so a tmux that fails while writing
its diagnostic to stdout -- or that fails silently -- leaves the operator (and
the KPI pipeline ingesting the runner log) with no way to tell WHY tmux
failed. The durable contract is exactly this: preserve a useful structured
error reason.

The fail -> pass contract
-------------------------
This test drives the REAL CLI journey (``omnigent pi --server ""`` under a
PTY: auto-spawned server + host daemon + runner) with a ``tmux`` first on
``PATH`` that answers ``-V`` normally and fails everything else with rc=1,
emitting a distinctive diagnostic on stdout. It waits for the reported
user-visible failure (the CLI's ``Pi terminal ensure failed`` error -- the
reproduction gate), then asserts the durable contract: the runner log's
surfaced failure must PRESERVE the diagnostic the failing tmux emitted.

On the current build the diagnostic is discarded (stdout -> DEVNULL) and the
log shows the unactionable ``tmux launch failed (rc=1): `` -- the assertion
FAILS, reproducing the reported behavior. It passes once the launch captures the
failing tmux's output (or otherwise preserves a useful reason) in the error it
raises.

Usage::

    python -m pytest tests/e2e/test_pi_terminal_ensure_tmux_launch_failure_e2e.py -v
"""

from __future__ import annotations

import contextlib
import os
import pty
import re
import select
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from tests.e2e._harness_probes import cli_unavailable_reason
from tests.e2e.helpers import POLL_INTERVAL_S

# tests/e2e/<this file> -> parents[2] is the worktree root; threaded onto the
# CLI + runner subprocess PYTHONPATH so they import THIS worktree's code.
_REPO_ROOT = Path(__file__).resolve().parents[2]

# The diagnostic the failing tmux emits (on stdout, which the launcher
# currently discards). The fix must surface this text in the runner log's
# failure so the KPI signature stops being an unactionable empty reason.
_TMUX_DIAG_MARKER = "failing-tmux-shim-diagnostic: new-session refused by shim"

# Version answered to ``tmux -V`` -- at/above the managed-terminal floor so the
# runner's ``_require_supported_tmux`` preflight passes and the failure lands
# where the reported failure lands: the launch itself.
_TMUX_VERSION_LINE = "tmux 3.4"

# Wide PTY so the CLI's error line is not wrapped/truncated.
_PTY_ROWS = 50
_PTY_COLS = 220

# Budget for the full CLI journey: server auto-spawn + daemon + runner online
# + the terminal-ensure failure. Generous for a loaded CI box.
_JOURNEY_TIMEOUT_S = 240

# Env vars that, leaked from this (possibly omnigent-hosted) process into the
# CLI subprocess, would misroute the auto-spawned server/daemon/runner.
_STALE_ENV_VARS = (
    "DATABRICKS_TOKEN",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "OMNIGENT_RUNNER_ID",
    "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN",
    "RUNNER_SERVER_URL",
    "OMNIGENT_RUNNER_WORKSPACE",
    "TMUX",
    # HOME is replaced with a per-test dir (below), so HOME-derived XDG
    # overrides must not leak in and point tools back at the real home.
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
    "XDG_CACHE_HOME",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "http_proxy",
    "https_proxy",
)

# Strip ANSI escape sequences (CSI, OSC, and keypad-mode toggles) so the CLI
# output can be matched as plain text.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[=>]")


def _make_failing_tmux(bin_dir: Path) -> Path:
    """Write a ``tmux`` that passes the version preflight but fails to launch.

    ``-V`` (anywhere in argv) answers a supported version, so both the CLI's
    dependency preflight and the runner's ``_require_supported_tmux`` accept
    it. Every other invocation -- the runner's ``new-session`` launch -- exits
    1 after printing its diagnostic to STDOUT, modelling the observed failure
    mode where ``tmux launch failed (rc=1): `` carries no stderr text.

    :param bin_dir: Directory to create the shim in (prepended to ``PATH``).
    :returns: The shim path.
    """
    shim = bin_dir / "tmux"
    shim.write_text(
        "#!/bin/sh\n"
        'for a in "$@"; do\n'
        f'  if [ "$a" = "-V" ]; then echo "{_TMUX_VERSION_LINE}"; exit 0; fi\n'
        "done\n"
        f'echo "{_TMUX_DIAG_MARKER}"\n'
        "exit 1\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return shim


def _journey_env(
    shim_dir: Path, config_home: Path, data_dir: Path, home_dir: Path
) -> dict[str, str]:
    """Build the isolated env for the ``omnigent pi`` journey subprocess."""
    env = dict(os.environ)
    for stale in _STALE_ENV_VARS:
        env.pop(stale, None)
    env["PATH"] = f"{shim_dir}{os.pathsep}{env['PATH']}"
    env["OMNIGENT_CONFIG_HOME"] = str(config_home)
    env["OMNIGENT_DATA_DIR"] = str(data_dir)
    # HOME-derived state (e.g. the pi-native bridge root under ~/.omnigent)
    # must land in the per-test dir: the real HOME may be read-only under
    # process isolation, and writing there would leak state between runs.
    env["HOME"] = str(home_dir)
    env["PYTHONPATH"] = f"{_REPO_ROOT}{os.pathsep}{env.get('PYTHONPATH', '')}"
    env["TERM"] = "xterm-256color"
    env["LINES"] = str(_PTY_ROWS)
    env["COLUMNS"] = str(_PTY_COLS)
    env["OMNIGENT_NO_UPDATE_CHECK"] = "1"
    env["OMNIGENT_SKIP_ONBOARD"] = "1"
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env["no_proxy"] = "127.0.0.1,localhost"
    return env


def _runner_log_text(data_dir: Path) -> str:
    """Concatenate every runner log the journey produced (empty if none)."""
    log_dir = data_dir / "logs" / "runner"
    if not log_dir.is_dir():
        return ""
    return "\n".join(p.read_text(errors="replace") for p in sorted(log_dir.glob("*.log")))


@pytest.mark.skipif(
    (_PI_REASON := cli_unavailable_reason("pi")) is not None,
    reason=(
        f"pi terminal-ensure journey requires a runnable 'pi' CLI; {_PI_REASON}. "
        "Install/fix Pi to run this test."
    ),
)
@pytest.mark.timeout(_JOURNEY_TIMEOUT_S + 120)
def test_pi_terminal_ensure_tmux_launch_failure_preserves_diagnostic() -> None:
    """A failing-tmux Pi launch must surface WHY tmux failed, not an empty reason.

    Journey: ``omnigent pi`` on a host whose tmux fails at launch
    -> the Pi terminal ensure fails (CLI errors, runner logs ``Pi terminal
    ensure failed for session=...`` with ``RuntimeError: tmux launch failed
    (rc=1): ``). Durable contract: the runner log must preserve the failing
    tmux's own diagnostic. Fails on the current build (stdout is piped to
    DEVNULL, the reason is empty); passes once the launch error carries the
    captured output.
    """
    work = Path(tempfile.mkdtemp(prefix="pi-tmux-launch-"))
    shim_dir = work / "bin"
    shim_dir.mkdir()
    _make_failing_tmux(shim_dir)
    config_home = work / "config"
    config_home.mkdir()
    data_dir = work / "data"
    home_dir = work / "home"
    home_dir.mkdir()

    env = _journey_env(shim_dir, config_home, data_dir, home_dir)
    omnigent = Path(sys.executable).parent / "omnigent"
    assert omnigent.is_file(), f"omnigent console script not found at {omnigent}"

    pid, fd = pty.fork()
    if pid == 0:
        try:
            os.execve(str(omnigent), [str(omnigent), "pi", "--server", ""], env)
        except OSError:
            os._exit(127)

    buf: list[bytes] = []
    lock = threading.Lock()
    stop = threading.Event()

    def _drain() -> None:
        while not stop.is_set():
            try:
                ready, _, _ = select.select([fd], [], [], 0.5)
            except OSError:
                break
            if not ready:
                continue
            try:
                data = os.read(fd, 4096)
            except OSError:
                break
            if not data:
                break
            with lock:
                buf.append(data)

    def _output() -> str:
        with lock:
            return _ANSI_RE.sub("", b"".join(buf).decode("utf-8", "replace"))

    threading.Thread(target=_drain, name=f"pi-pty-drain-{pid}", daemon=True).start()

    try:
        # Reproduction gate: the reported user-visible failure. The CLI must
        # die on the terminal ensure (NOT attach a working Pi terminal).
        deadline = time.monotonic() + _JOURNEY_TIMEOUT_S
        while time.monotonic() < deadline:
            if "Pi terminal ensure failed" in _output():
                break
            time.sleep(POLL_INTERVAL_S)
        cli_output = _output()
        assert "Pi terminal ensure failed" in cli_output, (
            "The journey did not reach the reported failure: `omnigent pi` "
            "with a launch-failing tmux never printed 'Pi terminal ensure "
            f"failed' within {_JOURNEY_TIMEOUT_S}s. CLI output tail:\n"
            f"{cli_output[-2500:]}"
        )

        # The runner log is where the failure reason must land (the client
        # payload deliberately carries only a generic message + log pointer,
        # and the KPI pipeline ingests these log records).
        deadline = time.monotonic() + 30
        log_text = _runner_log_text(data_dir)
        while time.monotonic() < deadline and "tmux launch failed" not in log_text:
            time.sleep(POLL_INTERVAL_S)
            log_text = _runner_log_text(data_dir)
        assert "Pi terminal ensure failed for session=" in log_text, (
            "Runner log never recorded the ensure failure signature "
            "('Pi terminal ensure failed for session='). Log text tail:\n"
            f"{log_text[-2500:]}"
        )
        assert "tmux launch failed" in log_text, (
            f"Runner log never recorded the tmux launch failure. Log tail:\n{log_text[-2500:]}"
        )

        # THE regression contract: the surfaced failure must
        # preserve the diagnostic the failing tmux emitted. On the buggy
        # build the log shows only 'RuntimeError: tmux launch failed (rc=1): '
        # (empty reason -- tmux stdout is piped to DEVNULL), so this FAILS.
        failure_lines = [line for line in log_text.splitlines() if "tmux launch failed" in line]
        assert _TMUX_DIAG_MARKER in log_text, (
            "The Pi terminal ensure failure dropped the failing tmux's own "
            f"diagnostic ({_TMUX_DIAG_MARKER!r}): the runner log's launch "
            "failure carries an EMPTY reason, so the operator (and the KPI "
            "pipeline reading this log) cannot tell why tmux failed. "
            "TerminalInstance.launch (omnigent/inner/terminal.py) pipes tmux "
            "stdout to DEVNULL and surfaces only stderr; the fix must "
            "preserve a useful error reason (e.g. capture stdout too). "
            f"Logged launch-failure lines: {failure_lines!r}"
        )
    finally:
        stop.set()
        # Tear down the whole tree: the CLI's process group, then the
        # auto-spawned managed server + local daemon + runner.
        with contextlib.suppress(ProcessLookupError, OSError):
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        with contextlib.suppress(ProcessLookupError, OSError):
            os.kill(pid, signal.SIGKILL)
        with contextlib.suppress(Exception):
            os.waitpid(pid, 0)
        with contextlib.suppress(OSError):
            os.close(fd)
        with contextlib.suppress(Exception):
            subprocess.run(
                [str(omnigent), "server", "stop"],
                env=env,
                capture_output=True,
                timeout=60,
            )

"""End-to-end guard: the harness readiness loop must not burn CPU on slow PATHs.

While a tunnel session is established, ``_harness_readiness_loop`` wakes
every ``HARNESS_READINESS_REFRESH_INTERVAL_S`` (5s) and runs
``_unavailable_harness_became_ready``, which calls
``harness_is_configured`` for every currently-unavailable harness. Each
call re-runs ``shutil.which`` PATH scans from scratch (twice, via
``resolve_cli_binary`` and ``_installer_only_availability``). With
several harnesses not installed, every quick probe performs full PATH
misses; on a machine where each stat is expensive (long PATH, Windows
endpoint-security filter drivers) the probe consumes a large fraction of
the 5s interval and the daemon burns up to a full core while completely
idle.

This drives the real user journey: start a real server, connect a real
host daemon (``omnigent.host._daemon_entry --server <url>``) whose
environment carries an artificially long PATH (thousands of empty
directories — the portable stand-in for "each PATH stat is expensive"),
let the tunnel establish, and measure the daemon's CPU consumption over
a ~35s window in which no sessions are dispatched. A functionally idle
daemon must not consume a meaningful fraction of a core::

    .venv/bin/python -m pytest tests/e2e/test_host_idle_readiness_cpu.py -v

Measured on the bug (2026-08-28 main): ~0.19 cores sustained with 3000
extra PATH entries; an identical daemon with a normal PATH measures
~0.005 cores. The threshold sits well between the two so the assertion
fails specifically on the readiness-loop rescan cost, not on scheduler
noise.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import httpx
import pytest
import yaml

from omnigent.process_logging import PROCESS_LOG_FILE_ENV_VAR
from tests._helpers.compat import apply_runner_env, compat_runner_cwd, runner_executable
from tests.e2e.test_host_e2e import _wait_for_host_online

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="reads /proc/<pid>/stat for per-process CPU accounting",
)

# Number of empty directories prepended to the daemon's PATH. Each
# shutil.which() miss walks all of them, making one readiness probe pass
# cost ~1s — the portable equivalent of the reported 95-entry Windows
# PATH inspected by endpoint-security filter drivers. Sized (with short
# names under a short /tmp prefix) to keep the PATH env string well
# under exec's per-string size limit, which can be tighter in sandboxes.
_PATH_DIR_COUNT = 3000

# Post-connect idle window we sample. Covers ~7 quick-probe wakeups of the
# 5s readiness cadence, so the buggy rescan cost dominates the sample.
_MEASURE_WINDOW_S = 35.0

# Max cores an idle, connected daemon may consume. The bug measures
# ~0.2 cores under this PATH; a daemon that caches binary resolutions
# (or otherwise avoids rescanning PATH every 5s) measures ~0.005 cores,
# with the 60s full refresh adding at most ~0.03. 0.07 sits between the
# two with ~3x margin on each side.
_MAX_IDLE_CORES = 0.07

# Seconds after the tunnel establishes before sampling starts, so
# connect-time work (initial capability probe, registration) settles.
_POST_CONNECT_SETTLE_S = 5.0


def _cpu_seconds(pid: int) -> float:
    """Return cumulative user+system CPU seconds for *pid* from /proc.

    :param pid: Process id of the daemon under measurement.
    :returns: utime+stime in seconds.
    """
    stat = Path(f"/proc/{pid}/stat").read_text()
    # comm may contain spaces/parens; fields resume after the last ')'.
    fields = stat[stat.rindex(")") + 2 :].split()
    utime_ticks = int(fields[11])  # field 14 overall
    stime_ticks = int(fields[12])  # field 15 overall
    return (utime_ticks + stime_ticks) / os.sysconf("SC_CLK_TCK")


def _make_expensive_path() -> tuple[str, str]:
    """Build a PATH whose misses are expensive to scan.

    :returns: ``(path_value, root_dir)`` — the PATH string to give the
        daemon (thousands of empty dirs prepended to the real PATH) and
        the root directory to remove on teardown.
    """
    root = tempfile.mkdtemp(prefix="p", dir="/tmp")
    dirs = []
    for i in range(_PATH_DIR_COUNT):
        d = os.path.join(root, str(i))
        os.mkdir(d)
        dirs.append(d)
    return os.pathsep.join([*dirs, os.environ["PATH"]]), root


def test_host_daemon_idle_cpu_with_expensive_path(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
) -> None:
    """A connected-but-idle host daemon must not burn CPU rescanning PATH.

    Connect a real host daemon whose PATH makes each
    ``shutil.which`` miss expensive, dispatch nothing to it, and assert
    its CPU consumption over an idle window stays far below the ~1 core
    the 5s readiness quick-probe rescan burns on the bug.
    """
    inflated_path, path_root = _make_expensive_path()

    omni_dir = tmp_path / ".omnigent"
    omni_dir.mkdir(parents=True)
    host_id = uuid.uuid4().hex
    (omni_dir / "config.yaml").write_text(
        yaml.safe_dump(
            {"host": {"host_id": host_id, "name": f"e2e-host-idle-cpu-{uuid.uuid4().hex[:12]}"}},
            default_flow_style=False,
            sort_keys=True,
        )
    )
    daemon_log = tmp_path / "host-daemon.log"
    env = apply_runner_env(
        {
            **os.environ,
            "HOME": str(tmp_path),
            "PATH": inflated_path,
            PROCESS_LOG_FILE_ENV_VAR: str(daemon_log),
        }
    )
    with open(daemon_log, "w") as log_fh:
        proc = subprocess.Popen(
            [runner_executable(), "-m", "omnigent.host._daemon_entry", "--server", live_server],
            env=env,
            cwd=compat_runner_cwd(),
            stdout=subprocess.DEVNULL,
            stderr=log_fh,
        )
    try:
        # Startup itself probes harnesses over the slow PATH, so give the
        # online wait headroom beyond the default 30s.
        _wait_for_host_online(http_client, host_id, timeout=90.0)
        time.sleep(_POST_CONNECT_SETTLE_S)

        cpu_start = _cpu_seconds(proc.pid)
        wall_start = time.monotonic()
        time.sleep(_MEASURE_WINDOW_S)
        cpu_end = _cpu_seconds(proc.pid)
        wall_end = time.monotonic()

        assert proc.poll() is None, "host daemon died during the idle measurement window"
        cores = (cpu_end - cpu_start) / (wall_end - wall_start)
        assert cores < _MAX_IDLE_CORES, (
            f"connected-but-idle host daemon consumed {cores:.3f} cores over "
            f"{wall_end - wall_start:.0f}s (limit {_MAX_IDLE_CORES}). The harness "
            "readiness quick probe is rescanning PATH via shutil.which for every "
            "unavailable harness on each 5s wakeup."
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
        shutil.rmtree(path_root, ignore_errors=True)

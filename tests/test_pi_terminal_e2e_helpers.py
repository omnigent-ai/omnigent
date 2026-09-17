"""Regression coverage for process-title changes and logs from earlier attempts."""

import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from tests.e2e.test_pi_main_terminal_tmux_disappears_e2e import (
    _TMUX_UNAVAILABLE_RE,
    _find_pi_process,
    _scan_home_logs_for,
)


@pytest.mark.timeout(15)
def test_finds_pi_after_node_replaces_its_command_line(tmp_path: Path) -> None:
    """A fully booted Pi remains identifiable by its inherited bridge directory."""
    node = shutil.which("node")
    if node is None or not Path("/proc").is_dir():
        pytest.skip("requires Node and Linux procfs")
    marker = f"pi-native/{uuid.uuid4().hex}"
    script = 'process.title = "pi"; process.stdout.write("ready\\n"); setInterval(() => {}, 1000);'
    proc = subprocess.Popen(
        [node, "-e", script],
        env={**os.environ, "OMNIGENT_PI_NATIVE_BRIDGE_DIR": str(tmp_path / marker)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline() == b"ready\n"
        found = _find_pi_process(marker)
        assert found is not None
        assert found[0] == proc.pid
        assert _find_pi_process(marker + "-other") is None
    finally:
        proc.kill()
        proc.communicate(timeout=5)


def test_failure_scan_ignores_other_sessions_but_keeps_current_errors(tmp_path: Path) -> None:
    """A retry cannot inherit a cleanup failure from an earlier session."""
    log_dir = tmp_path / ".omnigent" / "logs" / "runner"
    log_dir.mkdir(parents=True)
    signature = "tmux unavailable after 3 consecutive probes for terminal pi:main"
    (log_dir / "runner-old-session-20260101-000000.log").write_text(signature)
    current = log_dir / "runner-current-session-20260101-000001.log"
    current.write_text("terminal ready\n")

    assert _scan_home_logs_for(tmp_path, "current-session", _TMUX_UNAVAILABLE_RE) is None
    current.write_text(signature)
    assert _scan_home_logs_for(tmp_path, "current-session", _TMUX_UNAVAILABLE_RE) == signature

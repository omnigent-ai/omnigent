"""Shared startup failures retain diagnostics and reap the real child process."""

import subprocess
from pathlib import Path

import pytest

from tests._helpers.live_server import isolated_local_server


def test_startup_exit_reports_log_and_reaps_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    children = []
    real_popen = subprocess.Popen

    def record_child(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr("tests._helpers.live_server.subprocess.Popen", record_child)
    with pytest.raises(AssertionError) as error:
        with isolated_local_server(
            tmp_path,
            bootstrap="print('startup-marker', flush=True); raise SystemExit(23)",
            health_timeout=30,
            poll_interval=0.02,
        ):
            pytest.fail("a failed child must not be yielded as a healthy server")
    assert "Server exited with code 23" in str(error.value)
    assert "startup-marker" in str(error.value)
    assert len(children) == 1 and children[0].returncode == 23

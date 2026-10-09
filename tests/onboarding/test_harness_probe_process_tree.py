"""Readiness probes must not leave a CLI's descendants running after a timeout."""

from __future__ import annotations

import os
import signal
import time
from contextlib import suppress
from pathlib import Path

import pytest

from omnigent.inner._proc import process_alive
from omnigent.onboarding import harness_install as hi

_PROBE_TIMEOUT_S = 1.0
_TEARDOWN_GRACE_S = 3.0


def _install_hanging_cli(tmp_path: Path, name: str, child_pid_file: Path) -> None:
    # Fork a long-lived child, publish its pid, then hang past the probe timeout.
    lines = ["#!/bin/sh", "sleep 300 &", f"echo $! > '{child_pid_file}'", "exec sleep 300"]
    cli = tmp_path / name
    cli.write_text("\n".join(lines) + "\n", encoding="utf-8")
    cli.chmod(0o755)


def _read_child_pid(child_pid_file: Path) -> int:
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        text = child_pid_file.read_text(encoding="utf-8") if child_pid_file.exists() else ""
        if text.strip():
            return int(text)
        time.sleep(0.05)
    raise AssertionError("fake CLI never forked its child")


@pytest.mark.skipif(os.name == "nt", reason="requires a POSIX shell")
@pytest.mark.parametrize(
    ("probe", "key", "binary"),
    [
        pytest.param("harness_cli_installed", hi.HERMES_KEY, "hermes", id="version-probe"),
        pytest.param("harness_cli_logged_in", hi.CURSOR_KEY, "cursor-agent", id="login-probe"),
    ],
)
def test_timed_out_readiness_probe_leaves_no_cli_descendants(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: str,
    key: str,
    binary: str,
) -> None:
    child_pid_file = tmp_path / "child.pid"
    _install_hanging_cli(tmp_path, binary, child_pid_file)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")

    child_pid: int | None = None
    try:
        assert getattr(hi, probe)(key, timeout=_PROBE_TIMEOUT_S) is False
        child_pid = _read_child_pid(child_pid_file)
        deadline = time.monotonic() + _TEARDOWN_GRACE_S
        while process_alive(child_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not process_alive(child_pid), (
            f"{binary}'s child (pid {child_pid}) outlived the timed-out {probe} probe"
        )
    finally:
        if child_pid is not None:
            with suppress(ProcessLookupError, PermissionError):
                os.kill(child_pid, signal.SIGKILL)

"""Readiness probes must stop everything the CLI started when they time out."""

from __future__ import annotations

import os
import signal
import time
from contextlib import suppress
from pathlib import Path

import pytest

from omnigent.inner._proc import process_alive
from omnigent.onboarding import harness_install as hi


@pytest.mark.skipif(os.name == "nt", reason="requires a POSIX shell script CLI")
@pytest.mark.parametrize("probe", ["harness_cli_installed", "harness_cli_logged_in"])
def test_timed_out_probe_stops_what_the_cli_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, probe: str
) -> None:
    # A CLI that starts a helper (like an update-check fetch) and then hangs.
    pid_file = tmp_path / "helper.pid"
    cli = tmp_path / "claude"
    cli.write_text(f"#!/bin/sh\nsleep 60 &\necho $! > {pid_file}\nexec sleep 60\n")
    cli.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")

    assert getattr(hi, probe)("anthropic", timeout=1.0) is False

    helper = int(pid_file.read_text())
    try:
        deadline = time.monotonic() + 5
        while process_alive(helper) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not process_alive(helper)
    finally:
        with suppress(ProcessLookupError):
            os.kill(helper, signal.SIGKILL)

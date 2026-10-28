"""Tests for running a Windows npm or pnpm ``.cmd`` shim as its node script."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from omnigent.inner import node_cmd_shim
from tests._helpers.polling import poll_until

_PNPM_SHIM_TEXT = '@SETLOCAL\r\n"%~dp0\\node.exe"  "%~dp0\\..\\pkg\\cli.js" %*\r\n'


def _node_cmd_shim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    text: str,
    *,
    stdout: bytes | None = None,
    failure: Exception | None = None,
) -> list[dict]:
    """Write a ``pi.CMD`` shim with its node and script, and record each probe run."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "pi.CMD").write_text(text)
    (bin_dir / "node.exe").write_text("")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "cli.js").write_text("")
    probes: list[dict] = []
    output = json.dumps([str(bin_dir / "node.exe"), str(tmp_path / "pkg" / "cli.js")])
    reported = f"noise\r\n{node_cmd_shim._PROBE_SENTINEL}{output}\r\n".encode()

    def _popen(argv: list[str], **kwargs: object) -> SimpleNamespace:
        probe = {"argv": argv, **kwargs}
        probes.append(probe)
        if isinstance(failure, OSError):
            raise failure

        def _communicate(timeout: float) -> tuple[bytes, None]:
            probe["timeout"] = timeout
            if failure is not None:
                raise failure
            return stdout or reported, None

        return SimpleNamespace(
            pid=None, returncode=None, kill=lambda: None, communicate=_communicate
        )

    monkeypatch.setattr(node_cmd_shim, "IS_WINDOWS", True)
    monkeypatch.setattr(node_cmd_shim.subprocess, "Popen", _popen)
    return probes


def test_node_cmd_shim_argv_returns_the_node_and_script_the_shim_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Node reports its own executable and script from a probe the shim loads."""
    probes = _node_cmd_shim(tmp_path, monkeypatch, _PNPM_SHIM_TEXT)
    monkeypatch.setenv("NODE_OPTIONS", "--max-old-space-size=4096")
    shim = tmp_path / "bin" / "pi.CMD"

    argv = node_cmd_shim.node_cmd_shim_argv(str(shim))

    assert argv == [str(tmp_path / "bin" / "node.exe"), str(tmp_path / "pkg" / "cli.js")]
    (probe,) = probes
    assert probe["argv"] == [str(shim.resolve())]
    assert probe["stdin"] == subprocess.DEVNULL
    assert probe["timeout"] > 0
    probe_script = Path(node_cmd_shim.__file__).parent / "node_cmd_shim_probe.cjs"
    assert probe_script.is_file()
    node_options = probe["env"]["NODE_OPTIONS"]
    assert node_options.startswith("--max-old-space-size=4096 ")
    assert f'--require "{probe_script.as_posix()}"' in node_options


def test_node_cmd_shim_argv_probes_each_shim_version_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probes = _node_cmd_shim(tmp_path, monkeypatch, _PNPM_SHIM_TEXT)
    shim = tmp_path / "bin" / "pi.CMD"

    first = node_cmd_shim.node_cmd_shim_argv(str(shim))
    assert node_cmd_shim.node_cmd_shim_argv(str(shim)) == first
    assert len(probes) == 1

    shim.write_text(_PNPM_SHIM_TEXT + "\r\n")
    assert node_cmd_shim.node_cmd_shim_argv(str(shim)) == first
    assert len(probes) == 2


@pytest.mark.parametrize(
    ("case", "shim_runs"),
    [
        ("not_windows", False),
        ("not_cmd", False),
        ("missing_shim", False),
        ("names_no_node", False),
        ("runs_another_program_from_node_modules", False),
        ("probe_fails_to_start", True),
        ("probe_times_out", True),
        ("no_sentinel", True),
        ("malformed_output", True),
        ("missing_script", True),
    ],
)
def test_node_cmd_shim_argv_returns_none_when_the_shim_is_not_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str, shim_runs: bool
) -> None:
    """Callers keep the original program, and a shim that does not run node never runs."""
    text = {
        "names_no_node": "@echo off\r\nmytool %*\r\n",
        "runs_another_program_from_node_modules": (
            '@SETLOCAL\r\npython "%~dp0\\node_modules\\pkg\\tool.py" %*\r\n'
        ),
    }.get(case, _PNPM_SHIM_TEXT)
    stdout = {
        "no_sentinel": b'["node.exe", "cli.js"]\r\n',
        "malformed_output": f"{node_cmd_shim._PROBE_SENTINEL}[1]\r\n".encode(),
    }.get(case)
    failure = {
        "probe_fails_to_start": OSError("not a valid Win32 application"),
        "probe_times_out": subprocess.TimeoutExpired("pi.CMD", 5),
    }.get(case)
    probes = _node_cmd_shim(tmp_path, monkeypatch, text, stdout=stdout, failure=failure)
    path = str(tmp_path / "bin" / "pi.CMD")
    if case == "not_windows":
        monkeypatch.setattr(node_cmd_shim, "IS_WINDOWS", False)
    elif case == "not_cmd":
        path = str(tmp_path / "bin" / "node.exe")
    elif case == "missing_shim":
        path = str(tmp_path / "absent" / "pi.cmd")
    elif case == "missing_script":
        (tmp_path / "pkg" / "cli.js").unlink()

    assert node_cmd_shim.node_cmd_shim_argv(path) is None
    assert bool(probes) is shim_runs


@pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe runs .cmd shims only on Windows")
def test_node_cmd_shim_argv_ends_a_timed_out_probe_and_the_child_holding_its_pipe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = tmp_path / "child.pid"
    child = (
        "import os, pathlib, sys, time; "
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)"
    )
    shim = tmp_path / "pi.cmd"
    shim.write_text(f'@echo off\r\nrem node\r\n"{sys.executable}" -c "{child}" "{pid_file}"\r\n')
    monkeypatch.setattr(node_cmd_shim, "_PROBE_TIMEOUT_S", 2.0)

    started = time.monotonic()
    argv = node_cmd_shim.node_cmd_shim_argv(str(shim))
    elapsed = time.monotonic() - started

    assert argv is None
    assert elapsed < 15.0
    child_pid = int(pid_file.read_text())
    poll_until(lambda: not psutil.pid_exists(child_pid), timeout_s=5.0, interval_s=0.1)

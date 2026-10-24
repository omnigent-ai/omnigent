"""``omnigent copilot`` must be a recognized harness launcher: the real console
script, spawned under a pseudo-TTY, answers ``copilot --help`` without a server
or credential and lists ``copilot`` under ``Harnesses`` in ``--help``. On an
install without the optional ``copilot`` extra it must report the missing SDK
before booting a REPL whose first message can only fail."""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import re
import signal
import time
from pathlib import Path

import pytest

from omnigent.onboarding import secrets as secret_store
from tests.e2e._native_resume_helpers import cli_env, omnigent_console_script

pexpect = pytest.importorskip("pexpect")

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_EXIT_TIMEOUT_S = 90.0
# Booting `omnigent copilot` starts the local server + host daemon first (~20 s).
# The budgets below keep the whole test, teardown included, under CI's 180 s
# per-test timeout even when the launch regresses.
_BOOT_TIMEOUT_S = 75.0
_FIRST_TURN_TIMEOUT_S = 40.0
_QUIT_TIMEOUT_S = 15.0
_STOP_GRACE_S = 10.0

_REPL_READY_RE = r"state: sleeping|❯ "
_COPILOT_EXTRA_RE = r"omnigent\[copilot\]|github-copilot-sdk"
_MISSING_SDK_ERROR_RE = r"requires the 'github-copilot-sdk' package"

# Makes `copilot` unimportable in every Python process the launcher spawns, like an
# install without the `copilot` extra, while the test venv itself keeps the SDK.
_SDK_BLOCKER = """\
import sys
from importlib.abc import MetaPathFinder


class _NoCopilotSdk(MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "copilot" or fullname.startswith("copilot."):
            raise ModuleNotFoundError(f"No module named {fullname!r}", name=fullname)
        return None


sys.meta_path.insert(0, _NoCopilotSdk())
"""


def _run_cli(*args: str) -> tuple[str, int | None]:
    env = cli_env()
    env["NO_COLOR"] = "1"
    child = pexpect.spawn(
        str(omnigent_console_script()),
        list(args),
        env=env,
        encoding="utf-8",
        codec_errors="replace",
        timeout=_EXIT_TIMEOUT_S,
        dimensions=(50, 200),
    )
    try:
        child.expect(pexpect.EOF)
        output = child.before
    finally:
        child.close(force=True)
    return _ANSI_RE.sub("", output), child.exitstatus


def test_copilot_command_prints_its_own_usage() -> None:
    output, exit_code = _run_cli("copilot", "--help")

    assert "No such command 'copilot'" not in output, output
    assert exit_code == 0, output
    assert re.search(r"Usage: omnigent copilot\b", output), output


def test_help_lists_copilot_under_harnesses() -> None:
    # Extras-gated harnesses (cursor) are hidden from the roster until their SDK
    # is importable; only assert the row when the Copilot SDK is installed.
    if importlib.util.find_spec("copilot") is None:
        pytest.skip("github-copilot-sdk is not installed")

    output, exit_code = _run_cli("--help")

    assert exit_code == 0, output
    assert "Harnesses:" in output, output
    harness_section = output.split("Harnesses:", 1)[1].split("Commands:", 1)[0]
    assert re.search(r"^\s+copilot\s", harness_section, re.MULTILINE), output


def _copilot_launch_env(root: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Env for ``omnigent copilot`` with a token from setup but no copilot SDK.

    Isolated ``HOME``/config/data dirs so the launch spawns its own local server and
    host daemon and records their PIDs where only this test looks; the token is
    persisted the way pasting it into ``omnigent setup`` does (secret store +
    ``keychain:copilot`` reference), with the file backend so a developer's real
    keychain is never written.
    """
    home = root / "home"
    config_home = home / ".omnigent"
    config_home.mkdir(parents=True)
    blocker = root / "no-copilot-sdk"
    blocker.mkdir()
    (blocker / "sitecustomize.py").write_text(_SDK_BLOCKER, encoding="utf-8")

    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("OMNIGENT_DISABLE_KEYRING", "1")
    secret_store.store_secret("copilot", "github_pat_11AAAAAAA0FAKEFAKEFAKE_e2e")
    (config_home / "config.yaml").write_text(
        "copilot:\n  github_token_ref: keychain:copilot\ntui:\n  theme: light\n",
        encoding="utf-8",
    )

    env = cli_env()
    for ambient_token in ("GH_TOKEN", "GITHUB_TOKEN", "COPILOT_GITHUB_TOKEN"):
        env.pop(ambient_token, None)
    env.update(
        HOME=str(home),
        OMNIGENT_CONFIG_HOME=str(config_home),
        OMNIGENT_DATA_DIR=str(config_home),
        OMNIGENT_DISABLE_KEYRING="1",
        NO_COLOR="1",
        PYTHONPATH=f"{blocker}{os.pathsep}{env['PYTHONPATH']}",
    )
    return env


def _recorded_pids(data_dir: Path) -> list[int]:
    """PIDs the launch recorded under its own data dir: host daemon registry
    records first, then the local server pidfile (PID and port lines)."""
    pids: list[int] = []
    for record in sorted((data_dir / "daemons").glob("*.json")):
        try:
            pid = json.loads(record.read_text(encoding="utf-8")).get("pid")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(pid, int):
            pids.append(pid)
    pidfile = data_dir / "local_server.pid"
    if pidfile.is_file():
        first = pidfile.read_text(encoding="utf-8").split()[:1]
        if first and first[0].isdigit():
            pids.append(int(first[0]))
    return pids


def _terminate(pid: int) -> None:
    """SIGTERM a recorded process, escalating to SIGKILL after a grace period.

    A PID that is gone, or reused by a process we may not signal, is not ours.
    """
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.monotonic() + _STOP_GRACE_S
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            return
        time.sleep(0.2)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGKILL)


def _shutdown(child: pexpect.spawn, data_dir: Path) -> None:
    try:
        if child.isalive():
            child.send("/quit\r")
            child.expect(pexpect.EOF, timeout=_QUIT_TIMEOUT_S)
    except (pexpect.EOF, pexpect.TIMEOUT, OSError):
        # OSError: the child exited between isalive() and the pty write.
        pass
    finally:
        child.close(force=True)
    # Stop only the detached daemon and server this launch recorded under its own
    # data dir (the daemon reaps its runner); `omnigent stop` also sweeps the
    # canonical port and could hit a server the test does not own.
    for pid in _recorded_pids(data_dir):
        _terminate(pid)


def test_copilot_without_sdk_reports_the_missing_extra_before_booting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _copilot_launch_env(tmp_path, monkeypatch)
    data_dir = Path(env["OMNIGENT_DATA_DIR"])
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    child = pexpect.spawn(
        str(omnigent_console_script()),
        ["copilot"],
        env=env,
        cwd=str(workspace),
        encoding="utf-8",
        codec_errors="replace",
        timeout=_BOOT_TIMEOUT_S,
        dimensions=(40, 160),
    )
    try:
        outcome = child.expect(
            [_COPILOT_EXTRA_RE, _REPL_READY_RE, pexpect.EOF], timeout=_BOOT_TIMEOUT_S
        )
        startup = _ANSI_RE.sub("", child.before or "")
        if outcome == 0:
            # Named the extra: the launcher must now exit instead of booting the REPL.
            ended = child.expect([pexpect.EOF, _REPL_READY_RE], timeout=_FIRST_TURN_TIMEOUT_S)
            rest = _ANSI_RE.sub("", child.before or "")
            assert ended == 0, (
                f"`omnigent copilot` named the missing copilot extra but booted its REPL anyway:\n"
                f"{rest[-3000:]}"
            )
            child.close()
            assert child.exitstatus not in (None, 0), (
                f"`omnigent copilot` named the missing copilot extra but exited with "
                f"{child.exitstatus}:\n{(startup + rest)[-3000:]}"
            )
            assert _recorded_pids(data_dir) == [], (
                "`omnigent copilot` named the missing copilot extra but still started a "
                f"server or host daemon: {_recorded_pids(data_dir)}"
            )
            return
        assert outcome != 2, (
            "`omnigent copilot` exited without naming the missing copilot extra:\n"
            f"{startup[-3000:]}"
        )

        child.send("hello")
        child.send("\r")
        first_turn = child.expect(
            [_MISSING_SDK_ERROR_RE, pexpect.EOF, pexpect.TIMEOUT], timeout=_FIRST_TURN_TIMEOUT_S
        )
        matched = child.after if isinstance(child.after, str) else ""
        turn_output = _ANSI_RE.sub("", (child.before or "") + matched)
        detail = (
            "the first turn then failed with the missing-SDK error"
            if first_turn == 0
            else "the first turn produced no missing-SDK error before the timeout"
        )
        pytest.fail(
            "`omnigent copilot` booted its REPL although the copilot SDK is not installed; "
            f"{detail}:\n{turn_output[-3000:]}"
        )
    finally:
        _shutdown(child, data_dir)

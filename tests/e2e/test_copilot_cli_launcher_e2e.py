"""``omnigent copilot`` must be a recognized harness launcher: the real console
script, spawned under a pseudo-TTY, answers ``copilot --help`` without a server
or credential and lists ``copilot`` under ``Harnesses`` in ``--help``."""

from __future__ import annotations

import importlib.util
import re

import pytest

from tests.e2e._native_resume_helpers import cli_env, omnigent_console_script

pexpect = pytest.importorskip("pexpect")

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_EXIT_TIMEOUT_S = 90.0


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

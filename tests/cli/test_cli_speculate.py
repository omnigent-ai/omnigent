"""In-process ``omnigent speculate`` checks the subprocess e2e cannot patch or time."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from omnigent.cli_speculate import speculate_command

_ARGS = ["fix", "--replica", "true", "--verify", "true"]


def test_platform_without_process_groups_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replicas are stopped by process group, which Windows lacks, so the command refuses."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delattr(os, "killpg")

    result = CliRunner().invoke(speculate_command, _ARGS)

    assert (result.exit_code, result.stdout) == (2, "")
    assert result.stderr == "Error: omnigent speculate needs a POSIX system (Linux or macOS)\n"

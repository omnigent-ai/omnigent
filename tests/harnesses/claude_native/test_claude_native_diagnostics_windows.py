"""Windows file-opening checks of the Claude-native debug capture."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

from omnigent.harnesses.claude_native import bridge, diagnostics
from omnigent.process_logging import HARNESS_STDERR_ENABLED_ENV_VAR

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="Windows opens diagnostic files by path"
)


@pytest.fixture
def capture_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> Path:
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path)
    monkeypatch.setattr(bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setenv(HARNESS_STDERR_ENABLED_ENV_VAR, "1")
    caplog.set_level(logging.INFO, logger=diagnostics.__name__)
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    args = diagnostics.augment_claude_debug_args([], bridge_dir)
    assert args[0] == "--debug-file"
    return Path(args[1])


def _exported_texts(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.attributes["text"]
        for record in caplog.records
        if getattr(record, "event_name", None) == "harness_diagnostic_output"
    ]


def _poll_and_close(bridge_dir: Path) -> diagnostics.ClaudeDebugLogFollower:
    follower = diagnostics.ClaudeDebugLogFollower(bridge_dir)
    follower.poll("conv_test")
    follower.close("conv_test")
    return follower


def test_bridge_directory_junction_is_never_read(
    capture_file: Path, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    capture_file.write_text("redirected failure\n")
    bridge_dir = capture_file.parent
    elsewhere = bridge_dir.rename(tmp_path / "elsewhere")
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(bridge_dir), str(elsewhere)],
        check=True,
        capture_output=True,
    )

    follower = _poll_and_close(bridge_dir)

    assert _exported_texts(caplog) == []
    assert follower.health_snapshot()["diagnostic_capture_state"] == "read_error"


@pytest.mark.parametrize("substituted", [False, True])
def test_only_the_checked_entry_is_read(
    capture_file: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    substituted: bool,
) -> None:
    capture_file.write_text("final failure\n")
    unrelated = capture_file.with_name("unrelated.log")
    unrelated.write_text("unrelated private contents\n")
    open_by_path = diagnostics._open_by_path

    def substitute_then_open(path: Path) -> int:
        if not substituted or path != capture_file:
            return open_by_path(path)
        checked = path.with_name("checked.log")
        os.replace(path, checked)
        os.replace(unrelated, path)
        try:
            return open_by_path(path)
        finally:
            os.replace(path, unrelated)
            os.replace(checked, path)

    monkeypatch.setattr(diagnostics, "_open_by_path", substitute_then_open)

    _poll_and_close(capture_file.parent)

    assert _exported_texts(caplog) == ([] if substituted else ["final failure"])

"""Tests for native Kiro CLI orchestration."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from click import ClickException

from omnigent._wrapper_labels import KIRO_NATIVE_WRAPPER_VALUE, WRAPPER_LABEL_KEY
from omnigent.kiro_native import (
    PreparedKiroTerminal,
    _attach_terminal_resource,
    _launched_kiro_terminal_from_payload,
    _materialize_kiro_agent_spec,
    build_kiro_launch,
)


def test_materialize_kiro_agent_spec_uses_native_identity(tmp_path: Path) -> None:
    """The generated wrapper spec targets ``kiro-native`` and terminal-first labels."""
    path = _materialize_kiro_agent_spec(tmp_path, model="auto")

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert raw["name"] == "kiro-native-ui"
    assert raw["executor"] == {"harness": "kiro-native", "model": "auto"}
    assert raw["spawn"] is True


def test_materialized_kiro_agent_spec_passes_current_validator(tmp_path: Path) -> None:
    """``omnigent kiro`` must not be rejected as an unknown harness at upload."""
    from omnigent.spec._omnigent_compat import load_omnigent_yaml

    path = _materialize_kiro_agent_spec(tmp_path, model=None)

    spec = load_omnigent_yaml(path)

    assert spec.executor.config["harness"] == "kiro-native"


def test_launched_kiro_terminal_decodes_tmux_metadata() -> None:
    """Runner terminal metadata is converted into attach details."""
    terminal = _launched_kiro_terminal_from_payload(
        {
            "id": "terminal_kiro_main",
            "metadata": {
                "tmux_socket": "/tmp/kiro.sock",
                "tmux_target": "main",
            },
        }
    )

    assert terminal.terminal_id == "terminal_kiro_main"
    assert terminal.tmux_socket == Path("/tmp/kiro.sock")
    assert terminal.tmux_target == "main"


def test_build_kiro_launch_includes_resume_id() -> None:
    """Cold resume launches Kiro against the captured native session id."""
    launch = build_kiro_launch(
        ["--effort", "high"],
        resume_id="kiro-session-123",
        env={},
        which=lambda _cmd: "/usr/bin/kiro-cli",
    )

    assert launch.argv == [
        "/usr/bin/kiro-cli",
        "chat",
        "--tui",
        "--resume-id",
        "kiro-session-123",
        "--effort",
        "high",
    ]


@pytest.mark.asyncio
async def test_attach_terminal_resource_requires_tmux_metadata() -> None:
    """A runner response without tmux attach metadata fails clearly."""
    prepared = PreparedKiroTerminal(
        session_id="conv_abc",
        terminal_id="terminal_kiro_main",
        tmux_socket=None,
        tmux_target=None,
        reattached=False,
    )

    with pytest.raises(ClickException, match="Runner-owned Kiro terminal"):
        await _attach_terminal_resource(prepared)


def test_session_labels_use_kiro_wrapper_value() -> None:
    """Kiro wrapper sessions stamp the centralized wrapper label."""
    from omnigent.kiro_native import _SESSION_LABELS

    assert _SESSION_LABELS[WRAPPER_LABEL_KEY] == KIRO_NATIVE_WRAPPER_VALUE


def test_live_kiro_cli_binary_reports_version_when_installed() -> None:
    """Skippable smoke test for the native Kiro binary expected by the harness."""
    binary = shutil.which("kiro-cli")
    if binary is None:
        pytest.skip("kiro-cli is not installed on PATH")

    result = subprocess.run(
        [binary, "--version"],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == 0
    assert "kiro-cli" in result.stdout.lower()

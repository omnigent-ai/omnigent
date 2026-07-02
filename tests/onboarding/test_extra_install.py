"""Tests for ``omnigent/onboarding/extra_install.py``."""

from __future__ import annotations

import pytest

from omnigent.onboarding import extra_install
from omnigent.onboarding.extra_install import (
    _is_uv_tool_install,
    extra_install_command,
    extra_install_display,
)

# -- _is_uv_tool_install() --------------------------------------------------


@pytest.mark.parametrize(
    "prefix, expected",
    [
        # Default Linux/macOS layout
        ("/home/user/.local/share/uv/tools/omnigent/bin/python", True),
        # Windows layout (forward-slash normalized)
        ("C:/Users/user/AppData/Local/uv/tools/omnigent/Scripts/python", True),
        # Regular virtualenv — not a uv tool install
        ("/home/user/repos/omnigent/.venv", False),
        # System Python
        ("/usr", False),
        # pipx venv (should NOT be detected as uv tool)
        ("/home/user/.local/pipx/venvs/omnigent/bin/python", False),
    ],
    ids=["linux-uv-tool", "windows-uv-tool", "venv", "system", "pipx"],
)
def test_is_uv_tool_install(monkeypatch: pytest.MonkeyPatch, prefix: str, expected: bool) -> None:
    monkeypatch.setattr(extra_install.sys, "prefix", prefix)
    assert _is_uv_tool_install() is expected


# -- extra_install_command() -------------------------------------------------


def test_extra_install_command_uv_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inside a uv tool venv, produces the ``uv tool install --with`` argv."""
    monkeypatch.setattr(extra_install, "_is_uv_tool_install", lambda: True)
    cmd = extra_install_command("cursor")
    assert cmd == [
        "uv",
        "tool",
        "install",
        "--with",
        "omnigent[cursor]",
        "omnigent",
        "--force",
    ]


def test_extra_install_command_uv_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """With uv on PATH (non-tool), produces ``uv pip install``."""
    monkeypatch.setattr(extra_install, "_is_uv_tool_install", lambda: False)
    monkeypatch.setattr(extra_install.shutil, "which", lambda name: "/usr/bin/uv")
    cmd = extra_install_command("antigravity")
    assert cmd == ["uv", "pip", "install", "omnigent[antigravity]"]


def test_extra_install_command_pip_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without uv, falls back to this interpreter's pip."""
    monkeypatch.setattr(extra_install, "_is_uv_tool_install", lambda: False)
    monkeypatch.setattr(extra_install.shutil, "which", lambda name: None)
    cmd = extra_install_command("copilot")
    assert cmd == [
        extra_install.sys.executable,
        "-m",
        "pip",
        "install",
        "omnigent[copilot]",
    ]


# -- extra_install_display() -------------------------------------------------


def test_extra_install_display_matches_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The display string is a shell-safe rendering of the command argv."""
    monkeypatch.setattr(extra_install, "_is_uv_tool_install", lambda: False)
    monkeypatch.setattr(extra_install.shutil, "which", lambda name: "/usr/bin/uv")
    display = extra_install_display("cursor")
    assert "omnigent[cursor]" in display
    assert display.startswith("uv")

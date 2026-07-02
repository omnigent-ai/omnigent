"""Tests for ``omnigent/onboarding/extra_install.py``."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import tomllib

from omnigent.onboarding import extra_install
from omnigent.onboarding.antigravity_auth import ANTIGRAVITY_EXTRA
from omnigent.onboarding.copilot_auth import COPILOT_EXTRA
from omnigent.onboarding.cursor_auth import CURSOR_EXTRA
from omnigent.onboarding.extra_install import (
    _installed_vcs_url,
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


# -- _installed_vcs_url() ----------------------------------------------------


def test_installed_vcs_url_git_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """Surfaces the ``vcs_url`` recorded for a git-source install."""
    url = "git+https://github.com/omnigent-ai/omnigent.git"
    monkeypatch.setattr(
        "omnigent.update_check._read_installed_wheel_info",
        lambda: SimpleNamespace(vcs_url=url),
    )
    assert _installed_vcs_url() == url


@pytest.mark.parametrize(
    "info",
    [SimpleNamespace(vcs_url=None), None],
    ids=["registry-install", "not-installed"],
)
def test_installed_vcs_url_none(monkeypatch: pytest.MonkeyPatch, info: object) -> None:
    """Returns ``None`` for registry installs and when the dist is absent."""
    monkeypatch.setattr("omnigent.update_check._read_installed_wheel_info", lambda: info)
    assert _installed_vcs_url() is None


# -- extra_install_command() -------------------------------------------------


def test_extra_install_command_uv_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    """A registry uv tool venv produces the ``uv tool install --with`` argv."""
    monkeypatch.setattr(extra_install, "_is_uv_tool_install", lambda: True)
    monkeypatch.setattr(extra_install, "_installed_vcs_url", lambda: None)
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


def test_extra_install_command_uv_tool_git_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """A git-source uv tool install reinstalls from that source, not PyPI."""
    url = "git+https://github.com/omnigent-ai/omnigent.git"
    monkeypatch.setattr(extra_install, "_is_uv_tool_install", lambda: True)
    monkeypatch.setattr(extra_install, "_installed_vcs_url", lambda: url)
    cmd = extra_install_command("cursor")
    assert cmd == ["uv", "tool", "install", "--force", f"omnigent[cursor] @ {url}"]


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


# -- extra names stay in sync with pyproject packaging -----------------------


@pytest.mark.parametrize(
    "extra",
    [CURSOR_EXTRA, ANTIGRAVITY_EXTRA, COPILOT_EXTRA],
    ids=["cursor", "antigravity", "copilot"],
)
def test_harness_extra_is_a_real_pyproject_extra(extra: str) -> None:
    """Each harness ``*_EXTRA`` must name a real ``optional-dependencies`` key.

    The install command interpolates the constant into ``omnigent[<extra>]``; a
    typo or a rename in ``pyproject.toml`` would silently produce a command that
    installs a nonexistent extra. This ties the code's name to the packaging.
    """
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    if not pyproject.is_file():
        # Installed wheel with no source tree — nothing to check.
        return
    extras = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"][
        "optional-dependencies"
    ]
    assert extra in extras, (
        f"{extra!r} is not a declared optional-dependencies extra in "
        f"pyproject.toml (have: {sorted(extras)})"
    )

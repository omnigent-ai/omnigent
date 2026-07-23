"""Regression tests for user-facing Omnigent launchers."""

from __future__ import annotations

from pathlib import Path

import tomllib

ROOT = Path(__file__).resolve().parents[1]


def test_installed_package_exposes_omnigent_console_script() -> None:
    """A standard install must put ``omnigent`` on PATH."""
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    scripts = data["project"]["scripts"]
    assert scripts["omnigent"] == "omnigent.cli:main"
    assert scripts["omni"] == scripts["omnigent"]


def test_windows_batch_launcher_uses_same_module_entrypoint() -> None:
    """The repo Windows wrapper must dispatch through ``omnigent.__main__``."""
    launcher = ROOT / "scripts" / "omnigent.bat"

    text = launcher.read_text(encoding="utf-8").lower()
    assert "python -m omnigent %*" in text
    assert "omnigent.cli" not in text

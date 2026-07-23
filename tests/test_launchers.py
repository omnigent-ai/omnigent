"""Regression tests for user-facing Omnigent launcher packaging."""

from __future__ import annotations

from pathlib import Path

import tomllib

ROOT = Path(__file__).resolve().parents[1]


def test_installed_package_exposes_omnigent_console_script() -> None:
    """A standard install must put ``omnigent`` on PATH.

    On Windows, this same ``[project.scripts]`` entry creates the launcher
    executable in the environment's Scripts directory.
    """
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    scripts = data["project"]["scripts"]
    assert scripts["omnigent"] == "omnigent.cli:main"
    assert scripts["omni"] == scripts["omnigent"]

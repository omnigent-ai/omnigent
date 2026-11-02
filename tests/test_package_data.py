"""Pin the package-data globs for runtime assets that the wheel must ship.

Source-tree tests see these files even when the wheel omits them, so a
missing glob fails only for installed users. These tests expand the globs
from ``pyproject.toml`` against the files on disk.
"""

from __future__ import annotations

from pathlib import Path

import tomllib

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _shipped_files(package: str) -> set[Path]:
    """Return the files that *package*'s ``package-data`` globs match."""
    pyproject = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text())
    patterns = pyproject["tool"]["setuptools"]["package-data"].get(package, [])
    package_dir = _REPO_ROOT.joinpath(*package.split("."))
    return {path for pattern in patterns for path in package_dir.glob(pattern) if path.is_file()}


def test_framework_skill_files_ship_in_the_wheel() -> None:
    """Every file under the framework skills dir is declared as package data."""
    skills_dir = _REPO_ROOT / "omnigent" / "onboarding" / "agent" / "skills"
    on_disk = {path for path in skills_dir.rglob("*") if path.is_file()}
    assert on_disk, f"no framework skill files under {skills_dir}"

    missing = on_disk - _shipped_files("omnigent.onboarding")

    assert not missing, sorted(str(path.relative_to(_REPO_ROOT)) for path in missing)

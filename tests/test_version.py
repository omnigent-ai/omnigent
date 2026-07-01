"""Tests for the version source of truth (``omnigent.version``)."""

from __future__ import annotations

from pathlib import Path

import tomllib

from omnigent.version import VERSION


def test_version_is_a_nonempty_string() -> None:
    """``VERSION`` is the exported source-of-truth constant."""
    assert isinstance(VERSION, str)
    assert VERSION


def test_version_is_pep440() -> None:
    """The literal must be a valid PEP 440 version — the build ships it as-is."""
    from packaging.version import Version

    # Raises InvalidVersion if the literal is malformed.
    Version(VERSION)


def test_version_matches_pyproject() -> None:
    """``VERSION`` must equal ``pyproject.toml``'s canonical ``[project].version``.

    ``scripts/sync_version_py.py`` (a pre-commit fixer) keeps them equal; this
    is the CI backstop that catches a commit made without the hook.
    """
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    if not pyproject.is_file():
        # Running from an installed wheel with no source tree — nothing to check.
        return
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    pyproject_version = data["project"]["version"]
    assert pyproject_version == VERSION, (
        f"pyproject.toml version {pyproject_version!r} != omnigent.version.VERSION "
        f"{VERSION!r}; run `python scripts/sync_version_py.py`"
    )

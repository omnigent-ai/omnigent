"""
Unit tests for ``scripts/update_versions.py`` (the lockstep version bumper).

The ``repo_copy`` fixture copies the repo's *real* ``pyproject.toml``
files into a temp tree, so the regex anchors are exercised against the
actual file formatting — a drift in either the script or the files
fails here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import update_versions

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PYPROJECTS = [
    "pyproject.toml",
    "sdks/python-client/pyproject.toml",
    "sdks/ui/pyproject.toml",
]


@pytest.fixture
def repo_copy(tmp_path: Path) -> Path:
    """Copy the three real pyproject.toml files into a temp repo root."""
    root = tmp_path / "repo"
    for rel in _PYPROJECTS:
        dst = root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text((_REPO_ROOT / rel).read_text())
    return root


def test_set_version_rewrites_every_location(repo_copy: Path) -> None:
    changed = update_versions.set_version(repo_copy, "9.9.9")
    assert len(changed) == 3
    # root: version line + two sibling pins; SDKs: version line + one pin.
    assert (repo_copy / "pyproject.toml").read_text().count("9.9.9") == 3
    assert (repo_copy / "sdks/python-client/pyproject.toml").read_text().count("9.9.9") == 2
    assert (repo_copy / "sdks/ui/pyproject.toml").read_text().count("9.9.9") == 2
    # check() round-trips: all agree and pins are exact.
    assert update_versions.check(repo_copy, expect="9.9.9") == "9.9.9"


def test_set_version_preserves_unrelated_version_literals(repo_copy: Path) -> None:
    root_pyproject = repo_copy / "pyproject.toml"
    before = root_pyproject.read_text()
    # Real third-party floor that shares the old version digits — must
    # survive a bump untouched (anchored-on-name replacement, not blind).
    assert '"databricks-mcp>=0.1.0",' in before
    update_versions.set_version(repo_copy, "9.9.9")
    assert '"databricks-mcp>=0.1.0",' in root_pyproject.read_text()


def test_check_detects_version_drift(repo_copy: Path) -> None:
    update_versions.set_version(repo_copy, "9.9.9")
    # Knock one package out of lockstep but keep it internally consistent
    # (version + its own sibling pin both move) so the cross-package
    # disagreement is what surfaces, not a missing pin.
    ui = repo_copy / "sdks/ui/pyproject.toml"
    ui.write_text(ui.read_text().replace("9.9.9", "9.9.8"))
    with pytest.raises(ValueError, match="disagree"):
        update_versions.check(repo_copy)


def test_check_detects_missing_pin(repo_copy: Path) -> None:
    update_versions.set_version(repo_copy, "9.9.9")
    # Break the sibling pin while leaving the version intact.
    client = repo_copy / "sdks/python-client/pyproject.toml"
    client.write_text(client.read_text().replace('"omnigent==9.9.9"', '"omnigent==9.9.8"'))
    with pytest.raises(ValueError, match="missing exact pin"):
        update_versions.check(repo_copy)


def test_set_version_fails_loud_when_line_absent(tmp_path: Path) -> None:
    # A pyproject missing the version line must raise, not silently no-op.
    root = tmp_path / "repo"
    (root / "sdks/python-client").mkdir(parents=True)
    (root / "sdks/ui").mkdir(parents=True)
    (root / "pyproject.toml").write_text('[project]\nname = "omnigent"\n')
    (root / "sdks/python-client/pyproject.toml").write_text("[project]\n")
    (root / "sdks/ui/pyproject.toml").write_text("[project]\n")
    with pytest.raises(ValueError, match="expected exactly 1 match"):
        update_versions.set_version(root, "9.9.9")


@pytest.mark.parametrize(
    ("released", "expected"),
    [
        ("0.1.2", "0.1.3.dev0"),
        ("1.0.0", "1.0.1.dev0"),
        ("0.1.2rc1", "0.1.3.dev0"),
        ("2.5.9", "2.5.10.dev0"),
    ],
)
def test_next_dev_version(released: str, expected: str) -> None:
    assert update_versions.next_dev_version(released) == expected


def test_validate_pep440_rejects_junk() -> None:
    with pytest.raises(SystemExit, match="invalid version"):
        update_versions._validate_pep440("not-a-version")

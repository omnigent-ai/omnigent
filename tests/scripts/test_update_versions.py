"""
Unit tests for ``scripts/update_versions.py`` (the lockstep version bumper).

The ``repo_copy`` fixture copies the repo's *real* ``pyproject.toml``
files into a temp tree, so the regex anchors are exercised against the
actual file formatting — a drift in either the script or the files
fails here.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

# ``scripts`` is a namespace package (no ``__init__.py``), so a bare
# ``from scripts import update_versions`` is shadowed by the regular
# ``tests/scripts`` package when both resolve as a top-level ``scripts`` during
# a full-suite collection (pytest's default "prepend" import mode), failing with
# ``ImportError: cannot import name 'update_versions' from 'scripts'``. Load the
# module by its repo-root file path instead, which is immune to the collision.
_UPDATE_VERSIONS_SPEC = importlib.util.spec_from_file_location(
    "_update_versions_under_test", _REPO_ROOT / "scripts" / "update_versions.py"
)
assert _UPDATE_VERSIONS_SPEC is not None and _UPDATE_VERSIONS_SPEC.loader is not None
update_versions = importlib.util.module_from_spec(_UPDATE_VERSIONS_SPEC)
# Register before exec so dataclasses defined in the module can resolve their
# defining module via ``sys.modules`` during class creation.
sys.modules[_UPDATE_VERSIONS_SPEC.name] = update_versions
_UPDATE_VERSIONS_SPEC.loader.exec_module(update_versions)
_PYPROJECTS = [
    "pyproject.toml",
    "sdks/python-client/pyproject.toml",
    "sdks/ui/pyproject.toml",
]
# The runtime version constant is stamped/verified alongside the pyprojects.
_VERSION_PY = "omnigent/version.py"


@pytest.fixture
def repo_copy(tmp_path: Path) -> Path:
    """Copy the real pyproject.toml files + version.py into a temp repo root."""
    root = tmp_path / "repo"
    for rel in (*_PYPROJECTS, _VERSION_PY):
        dst = root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text((_REPO_ROOT / rel).read_text())
    return root


def test_set_version_rewrites_every_location(repo_copy: Path) -> None:
    changed = update_versions.set_version(repo_copy, "9.9.9")
    # Three pyprojects plus omnigent/version.py.
    assert len(changed) == 4
    # root: version line + two sibling pins; SDKs: version line + one pin.
    assert (repo_copy / "pyproject.toml").read_text().count("9.9.9") == 3
    assert (repo_copy / "sdks/python-client/pyproject.toml").read_text().count("9.9.9") == 2
    assert (repo_copy / "sdks/ui/pyproject.toml").read_text().count("9.9.9") == 2
    # The runtime constant is stamped too.
    assert 'VERSION = "9.9.9"' in (repo_copy / _VERSION_PY).read_text()
    # check() round-trips: all agree and pins are exact.
    assert update_versions.check(repo_copy, expect="9.9.9") == "9.9.9"


def test_set_version_updates_runtime_constant(repo_copy: Path) -> None:
    """The bump path keeps omnigent/version.py's VERSION in lockstep.

    This is the gap that would otherwise make the automated bot bump commit a
    stale constant and trip the ``test_version_matches_pyproject`` backstop.
    """
    version_py = repo_copy / _VERSION_PY
    assert 'VERSION = "9.9.9"' not in version_py.read_text()
    changed = update_versions.set_version(repo_copy, "9.9.9")
    assert version_py in changed
    assert 'VERSION = "9.9.9"' in version_py.read_text()


def test_check_detects_version_py_drift(repo_copy: Path) -> None:
    """A stale VERSION constant (pyprojects consistent) fails check()."""
    update_versions.set_version(repo_copy, "9.9.9")
    version_py = repo_copy / _VERSION_PY
    version_py.write_text(version_py.read_text().replace('VERSION = "9.9.9"', 'VERSION = "9.9.8"'))
    with pytest.raises(ValueError, match=r"omnigent/version\.py VERSION"):
        update_versions.check(repo_copy)


def test_set_version_fails_loud_when_constant_absent(repo_copy: Path) -> None:
    """A version.py missing the VERSION line must raise, not silently no-op."""
    version_py = repo_copy / _VERSION_PY
    version_py.write_text('"""No constant here."""\n')
    with pytest.raises(ValueError, match="expected exactly 1 match"):
        update_versions.set_version(repo_copy, "9.9.9")


def test_set_version_preserves_unrelated_version_literals(repo_copy: Path) -> None:
    root_pyproject = repo_copy / "pyproject.toml"
    before = root_pyproject.read_text()
    # Real third-party floor that shares the old version digits — must
    # survive a bump untouched (anchored-on-name replacement, not blind).
    assert '"databricks-mcp>=0.9.0",' in before
    update_versions.set_version(repo_copy, "9.9.9")
    assert '"databricks-mcp>=0.9.0",' in root_pyproject.read_text()


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
        ("0.1.2", "0.2.0.dev0"),
        ("1.0.0", "1.1.0.dev0"),
        ("0.6.0rc1", "0.7.0.dev0"),
        ("2.5.9", "2.6.0.dev0"),
    ],
)
def test_next_dev_version(released: str, expected: str) -> None:
    assert update_versions.next_dev_version(released) == expected


def test_validate_pep440_rejects_junk() -> None:
    with pytest.raises(SystemExit, match="invalid version"):
        update_versions._validate_pep440("not-a-version")

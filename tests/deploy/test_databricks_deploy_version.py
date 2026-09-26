"""The Databricks deploy stamps a commit-identified version into the runtime.

``deploy.py`` derives ``<base>.post<epoch>+g<sha>`` for each deploy and writes
it into the three pyprojects *and* ``omnigent/version.py`` — the constant the
debug-log ``app_version`` column, the runner hello and ``omnigent --version``
read — then restores the files after the wheel build. A generated version must
also pass the explicit ``--version`` validator so already-built wheels can be
redeployed with ``--skip-build --version <generated>``.
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
from packaging.version import Version

_ROOT = Path(__file__).resolve().parents[2]
_DEPLOY_PY = _ROOT / "deploy" / "databricks" / "deploy.py"


@pytest.fixture(scope="module")
def deploy_mod() -> ModuleType:
    """Load ``deploy.py`` by path — ``deploy/`` is not an installed package."""
    spec = importlib.util.spec_from_file_location("_databricks_deploy_version", _DEPLOY_PY)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_generated_version_names_the_commit_and_round_trips(
    deploy_mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(deploy_mod, "_git_build_suffix", lambda: "+g1a2b3c4")
    generated = deploy_mod._compute_deploy_version("0.16.0.dev0", None)
    assert re.fullmatch(r"0\.16\.0\.post\d+\+g1a2b3c4", generated)
    assert Version(generated).local == "g1a2b3c4"
    # ``--skip-build --version <generated>`` redeploys the wheels built with it.
    assert deploy_mod._compute_deploy_version("0.16.0.dev0", generated) == generated


def test_generated_version_does_not_stack_a_previous_stamp(
    deploy_mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(deploy_mod, "_git_build_suffix", lambda: "+gnew0000")
    restamped = deploy_mod._compute_deploy_version("0.16.0.post1700000000+gold0000.dirty", None)
    assert re.fullmatch(r"0\.16\.0\.post\d+\+gnew0000", restamped)


def test_explicit_version_must_be_pep440(deploy_mod: ModuleType) -> None:
    with pytest.raises(SystemExit, match="not a valid PEP 440 version"):
        deploy_mod._compute_deploy_version("0.16.0.dev0", "not-a-version")


def test_build_suffix_is_empty_outside_a_git_checkout(
    deploy_mod: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(deploy_mod, "_repo_root", lambda: tmp_path)
    assert deploy_mod._git_build_suffix() == ""


def test_build_suffix_marks_untracked_and_modified_trees_dirty(
    deploy_mod: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An untracked module ships in the wheel, so it must not pass as the commit."""

    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )

    git("init", "-q")
    (tmp_path / "omnigent").mkdir()
    (tmp_path / "omnigent" / "version.py").write_text('VERSION = "0.16.0.dev0"\n')
    git("add", ".")
    git("commit", "-q", "-m", "baseline")
    monkeypatch.setattr(deploy_mod, "_repo_root", lambda: tmp_path)

    assert re.fullmatch(r"\+g[0-9a-f]{7,}", deploy_mod._git_build_suffix())
    (tmp_path / "omnigent" / "extra.py").write_text("EXTRA = 1\n")
    assert deploy_mod._git_build_suffix().endswith(".dirty")
    git("add", ".")
    git("commit", "-q", "-m", "add extra")
    (tmp_path / "omnigent" / "version.py").write_text('VERSION = "0.16.0.dev1"\n')
    assert deploy_mod._git_build_suffix().endswith(".dirty")


def test_stamp_writes_runtime_constant_and_restore_reverts(
    deploy_mod: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pyprojects: list[Path] = []
    for name in ("root", "client", "ui"):
        path = tmp_path / name / "pyproject.toml"
        path.parent.mkdir()
        path.write_text(
            '[project]\nname = "x"\nversion = "0.16.0.dev0"\n'
            'dependencies = ["omnigent-client==0.16.0.dev0"]\n'
        )
        pyprojects.append(path)
    version_py = tmp_path / "version.py"
    shutil.copy(_ROOT / "omnigent" / "version.py", version_py)
    monkeypatch.setattr(deploy_mod, "_pyproject_paths", lambda: pyprojects)
    monkeypatch.setattr(deploy_mod, "_version_py_path", lambda: version_py)
    originals = {path: path.read_text() for path in (*pyprojects, version_py)}

    backups = deploy_mod._stamp_versions("0.16.0.post1+gabc1234")

    # The stamped constant is what the runtime imports, so the file must still
    # be a module with that single VERSION assignment.
    namespace: dict[str, object] = {}
    exec(version_py.read_text(), namespace)
    assert namespace["VERSION"] == "0.16.0.post1+gabc1234"
    assert all('version = "0.16.0.post1+gabc1234"' in path.read_text() for path in pyprojects)
    assert all('"omnigent-client==0.16.0.post1+gabc1234"' in p.read_text() for p in pyprojects)
    assert backups == originals
    deploy_mod._restore_versions(backups)
    assert {path: path.read_text() for path in (*pyprojects, version_py)} == originals

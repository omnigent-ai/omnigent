from __future__ import annotations

import pytest
import tomllib

from scripts.build_subpackages import (
    LEGAL_FILES,
    PACKAGE_DIRS,
    REPO_ROOT,
    staged_package,
)


@pytest.mark.parametrize("package_name", PACKAGE_DIRS)
def test_subpackage_stages_canonical_legal_files(package_name: str) -> None:
    package_dir = PACKAGE_DIRS[package_name]
    project = tomllib.loads((REPO_ROOT / package_dir / "pyproject.toml").read_text())["project"]

    assert project["license"] == "Apache-2.0"
    assert "license-files" not in project
    assert all(not (REPO_ROOT / package_dir / filename).exists() for filename in LEGAL_FILES)

    with staged_package(package_dir) as staged:
        staged_project = tomllib.loads((staged / "pyproject.toml").read_text())["project"]
        assert staged_project["license-files"] == list(LEGAL_FILES)
        for filename in LEGAL_FILES:
            assert (staged / filename).read_bytes() == (REPO_ROOT / filename).read_bytes()


def test_staging_preserves_package_depth_for_relative_paths() -> None:
    with staged_package(PACKAGE_DIRS["omnigent-client"]) as staged:
        assert staged.parent == REPO_ROOT / "sdks"
        assert (staged / "../..").resolve() == REPO_ROOT

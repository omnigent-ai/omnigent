"""The Files journeys' cleanup removes only what they seeded."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from tests.e2e_ui.files._files_page import remove_seeded
from tests.e2e_ui.files.test_files_tree_reveal import seeded_large_tree


def test_cleanup_removes_the_seeded_files_and_their_empty_folders_only(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "src" / "deep").mkdir(parents=True)
    (workspace / "docs").mkdir()
    seeded = ["src/deep/a.ts", "src/b.ts", "README.md"]
    for path in seeded:
        (workspace / path).write_text("x")
    # Files the workspace already held, one beside a seeded file.
    (workspace / "docs" / "notes.md").write_text("keep")
    (workspace / "src" / "keep.ts").write_text("keep")

    remove_seeded(workspace, [*seeded, "never/written.txt"])

    assert sorted(str(path.relative_to(workspace)) for path in workspace.rglob("*")) == [
        "docs",
        "docs/notes.md",
        "src",
        "src/keep.ts",
    ]


def _shell_in(workspace: Path):
    def run_shell(command: str) -> Mapping[str, Any]:
        done = subprocess.run(
            ["sh", "-c", command], cwd=workspace, capture_output=True, text=True, check=False
        )
        return {"exit_code": done.returncode, "stdout": done.stdout, "stderr": done.stderr}

    return run_shell


def test_the_reveal_tree_is_removed_after_the_journey(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("keep")

    with seeded_large_tree(_shell_in(tmp_path)) as workspace:
        assert (workspace / "packages" / "widgets" / "OWNERS").is_file()

    assert sorted(path.name for path in tmp_path.iterdir()) == ["README.md"]


def test_the_reveal_tree_never_seeds_into_or_removes_a_packages_folder_it_did_not_create(
    tmp_path: Path,
) -> None:
    (tmp_path / "packages").mkdir()
    (tmp_path / "packages" / "keep.txt").write_text("keep")

    with pytest.raises(AssertionError), seeded_large_tree(_shell_in(tmp_path)):
        pass

    assert [path.name for path in (tmp_path / "packages").iterdir()] == ["keep.txt"]

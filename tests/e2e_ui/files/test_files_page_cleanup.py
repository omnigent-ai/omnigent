"""The Files journeys' cleanup removes only what they seeded."""

from __future__ import annotations

from pathlib import Path

from tests.e2e_ui.files._files_page import remove_seeded


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

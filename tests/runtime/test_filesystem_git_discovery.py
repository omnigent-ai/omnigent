"""Optional Git discovery must not prevent ordinary file-change tracking."""

from __future__ import annotations

import errno
from pathlib import Path

import pytest

from omnigent.runtime.filesystem_registry import (
    AgentEditFilesystemRegistry,
    create_filesystem_registry,
)


@pytest.mark.parametrize(
    "probe_method,blocked_relative_path",
    [("is_dir", ".git"), ("exists", ".git/HEAD"), ("is_dir", ".git/objects")],
)
def test_inaccessible_ancestor_git_metadata_preserves_file_tracking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    probe_method: str,
    blocked_relative_path: str,
) -> None:
    git_dir = tmp_path / ".git"
    (git_dir / "objects").mkdir(parents=True)
    (git_dir / "refs").mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    blocked = tmp_path / blocked_relative_path
    original = getattr(Path, probe_method)

    def inaccessible_probe(path: Path) -> bool:
        if path == blocked:
            raise PermissionError(errno.EACCES, "synthetic metadata restriction", str(path))
        return original(path)

    monkeypatch.setattr(Path, probe_method, inaccessible_probe)

    registry = create_filesystem_registry(workspace)
    assert isinstance(registry, AgentEditFilesystemRegistry)
    registry.record_change("sample.txt", "created", "synthetic-session")
    changes = registry.list_changed_files("synthetic-session", limit=10)
    assert [entry["path"] for entry in changes] == ["sample.txt"]

    records = [
        record
        for record in caplog.records
        if getattr(record, "event_name", None) == "filesystem_git_discovery_failed"
    ]
    assert len(records) == 1
    assert records[0].attributes == {"exception_type": "PermissionError", "errno": errno.EACCES}
    assert str(blocked) not in caplog.text
    assert "synthetic metadata restriction" not in caplog.text

"""Tests for :mod:`omnigent.workspace_fs`.

The :class:`WorkspaceReader` serves the web file panel (browse, changed
files, diffs, search, file content) directly from disk when a session's
runner is offline but its host still holds the workspace. These tests
cover the four ops plus the read-only path-confinement guarantee, using a
real temp directory (and a real git repo for the change-tracking ops) so
the subprocess/os codepaths are exercised end to end.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from omnigent.workspace_fs import WorkspaceReader, WorkspaceReaderError


def _git_env() -> dict[str, str]:
    """Env with a dummy git identity so ``git commit`` never prompts.

    :returns: Copy of the current environment with author/committer set.
    """
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }


def _git_repo(path: Path) -> None:
    """Initialize a git repo at *path* with one committed file.

    :param path: Directory to turn into a git working tree.
    """
    env = _git_env()
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True, env=env)
    (path / "committed.txt").write_text("original\n")
    subprocess.run(
        ["git", "add", "committed.txt"], cwd=path, check=True, capture_output=True, env=env
    )
    subprocess.run(
        ["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True, env=env
    )


# ── list_or_read ──────────────────────────────────────────────────────


def test_list_dir_returns_entries(tmp_path: Path) -> None:
    """Listing the root returns each entry with its type and size.

    Mirrors the runner's ``/filesystem`` listing so the "All files"
    tab renders identically when served from the host.
    """
    (tmp_path / "a.txt").write_text("hello")
    (tmp_path / "sub").mkdir()
    reader = WorkspaceReader(tmp_path)

    result = reader.list_or_read("", limit=100, order="asc")

    assert result["object"] == "list"
    by_path = {e["path"]: e for e in result["data"]}
    assert by_path["a.txt"]["type"] == "file"
    assert by_path["a.txt"]["bytes"] == 5
    assert by_path["sub"]["type"] == "directory"


def test_read_text_file_returns_utf8_content(tmp_path: Path) -> None:
    """Reading a text file returns its UTF-8 content inline.

    This is exactly the payload the file viewer renders; if it were
    empty the viewer would show a blank file while the agent is asleep.
    """
    (tmp_path / "note.md").write_text("# Title\nbody\n")
    reader = WorkspaceReader(tmp_path)

    result = reader.list_or_read("note.md")

    assert result["object"] == "session.environment.filesystem.file_content"
    assert result["encoding"] == "utf-8"
    assert result["content"] == "# Title\nbody\n"


def test_read_binary_file_returns_base64(tmp_path: Path) -> None:
    """Reading a non-UTF-8 file returns base64-encoded content.

    Matches the runner: binary files can't decode as text, so the panel
    receives them base64 to download/preview rather than corrupting them.
    """
    (tmp_path / "blob.bin").write_bytes(b"\xff\xfe\x00\x01")
    reader = WorkspaceReader(tmp_path)

    result = reader.list_or_read("blob.bin")

    assert result["encoding"] == "base64"


def test_read_missing_file_raises_not_found(tmp_path: Path) -> None:
    """A missing path raises a 404 ``WorkspaceReaderError``.

    The server reproduces the runner's 404 from this, so a client sees
    the same "not found" it would with a live runner.
    """
    reader = WorkspaceReader(tmp_path)
    with pytest.raises(WorkspaceReaderError) as excinfo:
        reader.list_or_read("nope.txt")
    assert excinfo.value.status == 404


def test_read_oversize_file_is_capped_and_flagged(tmp_path: Path, monkeypatch) -> None:
    """A file larger than the byte cap is truncated and marked ``truncated``.

    The reader reads at most the cap (+1 sentinel byte) from disk rather than
    slurping the whole file, so a huge file can't OOM the host. Uses a tiny cap
    so the test writes only a few bytes.
    """
    monkeypatch.setattr("omnigent.workspace_fs._MAX_READ_BYTES", 8)
    (tmp_path / "big.txt").write_text("0123456789abcdef")  # 16 bytes > cap 8
    reader = WorkspaceReader(tmp_path)

    result = reader.list_or_read("big.txt")

    assert result["truncated"] is True
    assert result["content"] == "01234567"  # exactly the first cap bytes
    assert result["bytes"] == 8


def test_oversize_text_split_on_codepoint_stays_text(tmp_path: Path, monkeypatch) -> None:
    """A text file truncated mid-codepoint still serves as UTF-8, not base64.

    The byte cap can fall inside a multi-byte character; dropping the partial
    trailing codepoint keeps the file classified as text (matching the runner's
    boundary-safe truncation) instead of flipping it to base64.
    """
    monkeypatch.setattr("omnigent.workspace_fs._MAX_READ_BYTES", 4)
    # "aé" → b"a\xc3\xa9"; cap 4 keeps "aé" whole, so pad so the cap lands
    # inside the é: 3 ASCII + é = b"abc\xc3\xa9", cap 4 splits the é.
    (tmp_path / "u.txt").write_text("abcé")
    reader = WorkspaceReader(tmp_path)

    result = reader.list_or_read("u.txt")

    assert result["encoding"] == "utf-8"
    assert result["truncated"] is True
    assert result["content"] == "abc"  # partial é dropped, not corrupted


def test_oversize_binary_still_serves_base64(tmp_path: Path, monkeypatch) -> None:
    """A truncated genuinely-binary file stays base64, not rescued as text.

    The codepoint-split rescue must only apply to a partial trailing sequence;
    a binary file has invalid bytes earlier in the buffer and must fall through
    to base64.
    """
    monkeypatch.setattr("omnigent.workspace_fs._MAX_READ_BYTES", 4)
    (tmp_path / "b.bin").write_bytes(b"\xff\xfe\x00\x01\x02\x03")  # 6 bytes > cap 4
    reader = WorkspaceReader(tmp_path)

    result = reader.list_or_read("b.bin")

    assert result["encoding"] == "base64"
    assert result["truncated"] is True


def test_list_dir_includes_broken_symlink_as_file(tmp_path: Path) -> None:
    """A broken symlink is listed as ``type="file"`` with no size, like the runner.

    ``stat`` (follows the link) raises for a dangling target; the reader falls
    back to ``lstat`` and still lists the entry rather than silently dropping
    it — matching the runner's ``list_dir``.
    """
    (tmp_path / "dangling").symlink_to(tmp_path / "does_not_exist")
    reader = WorkspaceReader(tmp_path)

    result = reader.list_or_read("", limit=100, order="asc")

    by_path = {e["path"]: e for e in result["data"]}
    assert "dangling" in by_path, "broken symlink must still appear in the listing"
    assert by_path["dangling"]["type"] == "file"
    assert by_path["dangling"]["bytes"] is None


# ── Path confinement (read-only, workspace-root only) ─────────────────


def test_path_traversal_is_rejected(tmp_path: Path) -> None:
    """A ``../`` escape is rejected with a 400 rather than reading outside.

    The host serves files without the harness sandbox, so confinement to
    the workspace root must live in the reader itself — a leak here would
    expose arbitrary host files.
    """
    reader = WorkspaceReader(tmp_path)
    with pytest.raises(WorkspaceReaderError) as excinfo:
        reader.list_or_read("../secret")
    assert excinfo.value.status == 400


def test_absolute_path_is_rejected(tmp_path: Path) -> None:
    """An absolute path is rejected rather than escaping the root."""
    reader = WorkspaceReader(tmp_path)
    with pytest.raises(WorkspaceReaderError) as excinfo:
        reader.list_or_read("/etc/passwd")
    assert excinfo.value.status == 400


def test_symlink_escaping_root_is_rejected(tmp_path: Path) -> None:
    """A symlink pointing outside the root resolves out and is rejected.

    ``_resolve`` canonicalizes then checks containment, so a symlink can't
    smuggle a read of a file outside the workspace.
    """
    outside = tmp_path.parent / "outside_secret.txt"
    outside.write_text("secret")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "link.txt").symlink_to(outside)
    reader = WorkspaceReader(workspace)

    with pytest.raises(WorkspaceReaderError) as excinfo:
        reader.list_or_read("link.txt")
    assert excinfo.value.status == 400


# ── search ────────────────────────────────────────────────────────────


def test_search_matches_by_substring(tmp_path: Path) -> None:
    """Search returns files whose name/path contains the query.

    Same semantics as the runner's ``/search`` so the Explore tab's
    results match when served from the host.
    """
    (tmp_path / "alpha.py").write_text("x")
    (tmp_path / "beta.py").write_text("y")
    reader = WorkspaceReader(tmp_path)

    result = reader.search("alpha")

    paths = {e["path"] for e in result["data"]}
    assert paths == {"alpha.py"}


def test_search_blank_query_returns_empty(tmp_path: Path) -> None:
    """A whitespace-only query returns nothing instead of walking the tree.

    Matches the runner's guard against an accidental full-tree scan.
    """
    (tmp_path / "a.py").write_text("x")
    reader = WorkspaceReader(tmp_path)

    result = reader.search("   ")

    assert result["data"] == []


def test_search_exclude_glob_prunes_results(tmp_path: Path) -> None:
    """An exclude glob drops matching files from the results."""
    (tmp_path / "keep.py").write_text("x")
    node_modules = tmp_path / "node_modules"
    node_modules.mkdir()
    (node_modules / "keep.py").write_text("y")
    reader = WorkspaceReader(tmp_path)

    result = reader.search("keep", exclude="**/node_modules")

    paths = {e["path"] for e in result["data"]}
    assert paths == {"keep.py"}


# ── changes / diff (git mode) ─────────────────────────────────────────


def test_changes_lists_git_working_tree_modifications(tmp_path: Path) -> None:
    """In a git workspace, ``changes`` reports working-tree modifications.

    This is the "Changed" tab. Git mode reconstructs it purely from disk,
    so it works identically whether the runner or the host answers.
    """
    _git_repo(tmp_path)
    (tmp_path / "committed.txt").write_text("modified\n")
    (tmp_path / "new.txt").write_text("added\n")
    reader = WorkspaceReader(tmp_path)

    result = reader.changes("conv_x")

    by_path = {e["path"]: e for e in result["data"]}
    assert by_path["committed.txt"]["status"] == "modified"
    assert by_path["new.txt"]["status"] == "created"
    # Line counts come from numstat (git diff HEAD), so the host-served
    # list surfaces them just like the runner endpoint. The tracked edit
    # rewrote one line; the untracked file isn't in numstat → no counts.
    assert by_path["committed.txt"]["lines_added"] == 1
    assert by_path["committed.txt"]["lines_removed"] == 1
    assert by_path["new.txt"]["lines_added"] is None
    assert by_path["new.txt"]["lines_removed"] is None


def test_diff_returns_before_and_after_for_modified_file(tmp_path: Path) -> None:
    """``diff`` returns committed ``before`` and on-disk ``after`` content.

    Backs the before/after diff view; ``before`` comes from ``git show``
    and ``after`` from disk — both readable without a runner.
    """
    _git_repo(tmp_path)
    (tmp_path / "committed.txt").write_text("modified\n")
    reader = WorkspaceReader(tmp_path)

    result = reader.diff("conv_x", "committed.txt")

    assert result["before"] == "original\n"
    assert result["after"] == "modified\n"


def test_diff_unchanged_file_raises_not_found(tmp_path: Path) -> None:
    """A file with no working-tree change is not in the registry → 404.

    Matches the runner's diff endpoint, which 404s for a path that was
    never modified this session.
    """
    _git_repo(tmp_path)
    reader = WorkspaceReader(tmp_path)

    with pytest.raises(WorkspaceReaderError) as excinfo:
        reader.diff("conv_x", "committed.txt")
    assert excinfo.value.status == 404


def test_changes_non_git_workspace_is_empty(tmp_path: Path) -> None:
    """A non-git workspace reports no changes from the host.

    The host has no access to the live agent's in-memory edit history, so
    the changed-files list is empty (documented degradation) — but the
    call must succeed, not error.
    """
    (tmp_path / "a.txt").write_text("x")
    reader = WorkspaceReader(tmp_path)

    result = reader.changes("conv_x")

    assert result["data"] == []

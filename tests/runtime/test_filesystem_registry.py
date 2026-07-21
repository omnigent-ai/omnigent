"""Tests for :mod:`omnigent.runtime.filesystem_registry`.

Covers the ``list_changed_files`` merge logic for :class:`AgentEditFilesystemRegistry`
— specifically the invariant that a file first created in a session keeps status
``"created"`` even when subsequently edited within the same session.

Also covers ``seed_snapshot``, ``get_baseline`` for both implementations, and
``_normalize_path``.

Events are injected via :func:`_inject`, which calls :meth:`record_change` on
the registry so tests exercise the same code path as real tool calls.
"""

import os
import subprocess
from pathlib import Path

import pytest

from omnigent.runtime.filesystem_registry import (
    AgentEditFilesystemRegistry,
    GitFilesystemRegistry,
    GitStatusUnavailable,
    _git_timeout_seconds,
    _normalize_path,
    _parse_git_porcelain_line,
    _unquote_git_path,
    create_filesystem_registry,
)


def _inject(
    registry: AgentEditFilesystemRegistry,
    path: str,
    operation: str,
    conv_id: str,
) -> None:
    """Inject a synthetic file-change event into *registry* via :meth:`record_change`.

    Uses the public API so tests exercise the same recording path as real
    tool calls, rather than writing directly to internal state.

    :param registry: The registry to inject into.
    :param path: Relative file path, e.g. ``"src/foo.py"``.
    :param operation: One of ``"created"``, ``"modified"``, ``"deleted"``.
    :param conv_id: The session to attribute the event to,
        e.g. ``"conv_abc123"``.
    """
    registry.record_change(path, operation, conv_id)


@pytest.fixture
def registry(tmp_path: Path) -> AgentEditFilesystemRegistry:
    """An :class:`AgentEditFilesystemRegistry` rooted at a fresh temp directory.

    :param tmp_path: pytest's built-in temporary directory fixture.
    :returns: An :class:`AgentEditFilesystemRegistry` instance with in-memory
        event tracking (no persistence).
    """
    return AgentEditFilesystemRegistry(watch_path=tmp_path)


def test_created_then_modified_shows_added(registry: AgentEditFilesystemRegistry) -> None:
    """A file created and then edited in the same session must show status ``"created"``.

    Regression test for the bug where a ``"modified"`` event (later timestamp)
    would overwrite the ``"created"`` event in the merge, causing the file
    viewer to display ``"modified"`` instead of ``"created"`` for a newly created file.
    """
    conv_id = "conv_test_created_modified"
    _inject(registry, "trip.md", "created", conv_id)
    _inject(registry, "trip.md", "modified", conv_id)

    results = registry.list_changed_files(conv_id, limit=10)

    # Exactly one record should appear for trip.md.
    assert len(results) == 1, (
        f"Expected 1 record for trip.md, got {len(results)}. "
        "Duplicate entries suggest the dedup merge didn't fire."
    )

    # Status must be "created" — the file is new to this session regardless of edits.
    # If "modified", the modified event overwrote the created event (the bug).
    assert results[0]["status"] == "created", (
        f"Expected status 'created' (file is newly created this session), "
        f"got '{results[0]['status']}'. "
        "A 'M' result means the modified event incorrectly replaced the created event."
    )
    assert results[0]["path"] == "trip.md"


def test_modified_only_shows_modified(registry: AgentEditFilesystemRegistry) -> None:
    """A file that was only ever modified (pre-existing) shows status ``"modified"``."""
    conv_id = "conv_test_modified_only"
    _inject(registry, "existing.md", "modified", conv_id)

    results = registry.list_changed_files(conv_id, limit=10)

    # Exactly one record — the single injected event for existing.md.
    # More than 1 would mean dedup is broken; 0 would mean the event was filtered.
    assert len(results) == 1
    # Pre-existing file touched in this session should remain "modified".
    assert results[0]["status"] == "modified", (
        f"Expected status 'modified' for a pre-existing modified file, "
        f"got '{results[0]['status']}'."
    )


def test_created_then_deleted_is_hidden(registry: AgentEditFilesystemRegistry) -> None:
    """A file created and then deleted in the same session must not appear at all.

    The file never existed before the session started, and it is gone now —
    from the user's perspective it never existed.  Showing it as ``"D"``
    would be misleading because there is nothing to diff or open.
    """
    conv_id = "conv_test_created_deleted"
    _inject(registry, "gone.md", "created", conv_id)
    _inject(registry, "gone.md", "deleted", conv_id)

    results = registry.list_changed_files(conv_id, limit=10)

    assert results == [], (
        f"Expected no results for a file created then deleted this session, got {results}. "
        "A file that never existed before the session and is now gone should be hidden."
    )


def test_ephemeral_files_are_suppressed(registry: AgentEditFilesystemRegistry) -> None:
    """Ephemeral process-artifact files must never appear in the Files panel.

    Patterns like ``*.tmp``, ``*.tmp.*``, ``*~``, ``*.swp``, ``*.swo``,
    and ``#*#`` are write-temp / editor-artifact files that no user wants
    to see.  They must be filtered regardless of ``.gitignore`` content.
    """
    conv_id = "conv_test_ephemeral"

    # Inject one event per ephemeral pattern; also inject a real file to
    # confirm the filter is selective.
    ephemeral_files = [
        "pyproject.toml.tmp.12345",  # write-then-rename temp (uv, pip, …)
        "pyproject.toml.tmp",  # plain *.tmp
        "notes.md~",  # editor backup
        ".main.py.swp",  # vim swap
        ".main.py.swo",  # vim secondary swap
        "#README.md#",  # Emacs auto-save
    ]
    for f in ephemeral_files:
        _inject(registry, f, "created", conv_id)
    _inject(registry, "real_file.md", "created", conv_id)

    results = registry.list_changed_files(conv_id, limit=50)
    paths = [r["path"] for r in results]

    # Only the real file should appear.
    assert paths == ["real_file.md"], (
        f"Expected only 'real_file.md', got {paths}. "
        "Ephemeral process-artifact files must be suppressed by record_change."
    )


def test_created_modified_multiple_times_stays_added(
    registry: AgentEditFilesystemRegistry,
) -> None:
    """Multiple edits after creation must not degrade the ``"created"`` status."""
    conv_id = "conv_test_multi_edit"
    _inject(registry, "notes.md", "created", conv_id)
    _inject(registry, "notes.md", "modified", conv_id)
    _inject(registry, "notes.md", "modified", conv_id)
    _inject(registry, "notes.md", "modified", conv_id)

    results = registry.list_changed_files(conv_id, limit=10)

    assert len(results) == 1
    # Three subsequent edits must not degrade the status from "created" to "modified".
    assert results[0]["status"] == "created", (
        f"Expected status 'created' after multiple edits to a newly created file, "
        f"got '{results[0]['status']}'."
    )


def test_modified_then_deleted_shows_deleted(registry: AgentEditFilesystemRegistry) -> None:
    """A pre-existing file that is modified then deleted must show status ``"deleted"``.

    Exercises the ``_net_operation("modified", "deleted") -> "deleted"`` branch.
    If this fails with ``"modified"``, the deleted-event handling is not overriding the
    earlier modified event.
    """
    conv_id = "conv_test_modified_deleted"
    _inject(registry, "removed.md", "modified", conv_id)
    _inject(registry, "removed.md", "deleted", conv_id)

    results = registry.list_changed_files(conv_id, limit=10)

    assert len(results) == 1, f"Expected 1 record for removed.md, got {len(results)}."
    assert results[0]["status"] == "deleted", (
        f"Expected status 'deleted' for a pre-existing file that was deleted, "
        f"got '{results[0]['status']}'. "
        "A 'M' result means the deleted event did not override the modified event."
    )
    assert results[0]["path"] == "removed.md"


def test_deleted_then_created_shows_modified(registry: AgentEditFilesystemRegistry) -> None:
    """A file deleted then recreated in the same session shows status ``"modified"``.

    Exercises the ``_net_operation("deleted", "created") -> "modified"`` branch:
    the file existed before the session, was removed, then put back — the net
    effect from the user's perspective is a modification of a pre-existing file.
    If this fails with ``"created"``, the replace-within-session path is broken.
    """
    conv_id = "conv_test_deleted_created"
    _inject(registry, "replaced.md", "deleted", conv_id)
    _inject(registry, "replaced.md", "created", conv_id)

    results = registry.list_changed_files(conv_id, limit=10)

    assert len(results) == 1, f"Expected 1 record for replaced.md, got {len(results)}."
    assert results[0]["status"] == "modified", (
        f"Expected status 'modified' for a file deleted then recreated this session, "
        f"got '{results[0]['status']}'. "
        "An 'A' result means the replace-within-session case is mis-classified as new."
    )
    assert results[0]["path"] == "replaced.md"


def test_session_isolation_events_not_shared_between_sessions(
    registry: AgentEditFilesystemRegistry,
) -> None:
    """Events recorded for session A must not appear when querying session B.

    With per-session event lists, isolation is guaranteed by the data structure:
    record_change attributes events to a specific session, so another session
    can never see them.
    """
    conv_a = "conv_isolation_A"
    conv_b = "conv_isolation_B"

    # Record an event attributed to session A only.
    _inject(registry, "shared.md", "modified", conv_a)

    results_a = registry.list_changed_files(conv_a, limit=10)
    results_b = registry.list_changed_files(conv_b, limit=10)

    # Session A must see its own event.
    assert any(r["path"] == "shared.md" for r in results_a), (
        f"Session A should see 'shared.md' (its own event), but results_a = {results_a}."
    )
    # Session B must see nothing — the event was attributed to session A.
    assert results_b == [], (
        f"Session B should see no events (no events attributed to it), "
        f"but results_b = {results_b}. "
        "Per-session isolation is broken."
    )


def test_limit_parameter_caps_results(registry: AgentEditFilesystemRegistry) -> None:
    """``list_changed_files`` honours the ``limit`` parameter.

    Injecting more files than the limit must not return more records
    than requested.
    """
    conv_id = "conv_test_limit"

    # Inject 5 distinct files.
    for i in range(5):
        _inject(registry, f"file_{i}.md", "created", conv_id)

    results = registry.list_changed_files(conv_id, limit=3)

    # At most 3 records must be returned.
    assert len(results) <= 3, (
        f"Expected at most 3 results with limit=3, got {len(results)}. "
        "The limit parameter is not being respected."
    )


# ── seed_snapshot / get_baseline ─────────────────────────────────────────────


def test_seed_snapshot_stores_content(tmp_path: Path) -> None:
    """``seed_snapshot`` persists content that ``get_baseline`` returns on a non-git workspace.

    The registry is rooted at ``tmp_path`` which is not a git repo, so
    ``get_baseline`` must fall back to the in-memory snapshot dict.
    Failure here means either ``seed_snapshot`` is not writing to
    ``_snapshots`` or ``get_baseline`` is not reading from it.
    """
    reg = AgentEditFilesystemRegistry(watch_path=tmp_path)

    reg.seed_snapshot("foo.py", "original")

    result = reg.get_baseline("foo.py")
    # get_baseline must return exactly what seed_snapshot stored.
    # None here means the snapshot was not persisted or the key was normalised
    # differently between seed_snapshot and get_baseline.
    assert result == "original", (
        f"Expected 'original', got {result!r}. "
        "seed_snapshot did not persist the content or get_baseline could not retrieve it."
    )


def test_seed_snapshot_is_no_op_if_already_exists(tmp_path: Path) -> None:
    """A second ``seed_snapshot`` call with different content must not overwrite the first.

    First-write-wins semantics guarantee that the snapshot always reflects
    the state *before* the very first write — subsequent writes should not
    corrupt it.  Failure means the guard ``if norm not in self._snapshots``
    is missing or broken.
    """
    reg = AgentEditFilesystemRegistry(watch_path=tmp_path)

    reg.seed_snapshot("bar.py", "first")
    reg.seed_snapshot("bar.py", "second")  # must be a no-op

    result = reg.get_baseline("bar.py")
    # Must still be 'first' — the second call must not overwrite.
    assert result == "first", (
        f"Expected 'first' (first-write-wins), got {result!r}. "
        "The second seed_snapshot call overwrote the first snapshot."
    )


def test_get_baseline_returns_none_when_no_snapshot(tmp_path: Path) -> None:
    """``get_baseline`` returns ``None`` when no snapshot exists and there is no git repo.

    Verifies the non-git fallback path ends with ``_snapshots.get(norm)``
    which returns ``None`` for an unknown key.  Failure (returning a non-None
    value) would mean a phantom baseline is being manufactured.
    """
    reg = AgentEditFilesystemRegistry(watch_path=tmp_path)

    result = reg.get_baseline("never_seeded.py")
    # No snapshot, no git → must return None.
    assert result is None, f"Expected None (no snapshot, no git), got {result!r}."


def test_get_baseline_returns_snapshot_for_non_git_workspace(tmp_path: Path) -> None:
    """``get_baseline`` returns the snapshot seeded via ``seed_snapshot`` in a non-git workspace.

    Redundant with ``test_seed_snapshot_stores_content`` but explicitly
    documents the non-git dispatch path of ``get_baseline``.  Both tests
    cover the same branch so that if either regresses the failure message
    clearly names the failing path.
    """
    reg = AgentEditFilesystemRegistry(watch_path=tmp_path)

    reg.seed_snapshot("src/lib.py", "lib original")

    result = reg.get_baseline("src/lib.py")
    # Must match what was seeded — proves the non-git snapshot fallback works.
    assert result == "lib original", (
        f"Expected 'lib original', got {result!r}. "
        "Non-git get_baseline fallback is not returning the seeded snapshot."
    )


def _git_env() -> dict[str, str]:
    """Build an env dict with dummy git identity to avoid 'user.email' errors.

    :returns: Copy of the current environment with GIT_AUTHOR_* and
        GIT_COMMITTER_* set to safe dummy values.
    """
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }


def test_get_baseline_uses_git_show_for_committed_file(tmp_path: Path) -> None:
    """``get_baseline`` returns committed content via ``git show HEAD:<path>`` in a git workspace.

    Uses a real git repo initialised in ``tmp_path`` so the subprocess
    codepath is fully exercised.  Failure means either ``_git_root`` was
    not detected or the ``git show`` invocation returned wrong content.
    """
    env = _git_env()
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True, env=env)
    (tmp_path / "committed.py").write_text("committed content")
    subprocess.run(
        ["git", "add", "committed.py"], cwd=tmp_path, check=True, capture_output=True, env=env
    )
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        env=env,
    )

    reg = GitFilesystemRegistry(watch_path=tmp_path, git_root=tmp_path)

    result = reg.get_baseline("committed.py")
    # Must return exactly what was committed — confirms git show is being called
    # and its stdout is decoded correctly.
    assert result == "committed content", (
        f"Expected 'committed content', got {result!r}. "
        "get_baseline did not return the committed file content via git show."
    )


def test_get_baseline_returns_none_for_new_untracked_file(tmp_path: Path) -> None:
    """``get_baseline`` returns ``None`` for a file that is not tracked in git.

    Uses a real ``GitFilesystemRegistry`` so the git show subprocess path is
    fully exercised.  An empty commit ensures HEAD exists so that
    ``git show HEAD:<path>`` fails cleanly (non-zero exit) rather than
    erroring on a missing HEAD ref.  Failure (returning non-None) would
    mean git show returned exit 0 for an untracked file.
    """
    env = _git_env()
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True, env=env)
    # Create HEAD via an empty commit so `git show HEAD:<path>` fails cleanly.
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "init"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        env=env,
    )

    reg = GitFilesystemRegistry(watch_path=tmp_path, git_root=tmp_path)

    result = reg.get_baseline("untracked.py")
    # untracked.py is not in git — git show must return non-zero, so None.
    assert result is None, (
        f"Expected None for an untracked file, got {result!r}. "
        "get_baseline returned a non-None baseline for a file not in git."
    )


def test_git_list_changed_files_excludes_terminals_dir(tmp_path: Path) -> None:
    """``list_changed_files`` must not surface files under the ``terminals/`` directory.

    The runner writes terminal session output to ``<workspace>/terminals/<id>.txt``.
    These files are never agent-edited source files and must be hidden from the
    Files panel regardless of their git status.  Failure means terminal output
    files would appear as phantom "changes" in sessions that made no file edits.
    """
    env = _git_env()
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True, env=env)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "init"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        env=env,
    )

    # Simulate the runner creating terminal output files.
    terminals_dir = tmp_path / "terminals"
    terminals_dir.mkdir()
    (terminals_dir / "6.txt").write_text("terminal output")

    # Also create a legitimate source file change so we can confirm list_changed_files
    # is still returning real changes (not silently returning empty).
    (tmp_path / "real_change.py").write_text("agent wrote this")

    reg = GitFilesystemRegistry(watch_path=tmp_path, git_root=tmp_path)
    results = reg.list_changed_files("any-conv", limit=100)

    paths = [r["path"] for r in results]
    # The real source file must appear — confirms list_changed_files is working.
    assert "real_change.py" in paths, (
        f"Expected 'real_change.py' in results but got {paths}. "
        "list_changed_files may not be returning untracked source files."
    )
    # Terminal output files must be suppressed.
    terminal_paths = [p for p in paths if p.startswith("terminals/")]
    assert terminal_paths == [], (
        f"Expected no terminals/ paths but got {terminal_paths}. "
        "Terminal output files are leaking into the Files panel."
    )


def test_git_changed_files_suppress_ephemeral_files(tmp_path: Path) -> None:
    """Git-backed changed files must hide temp/editor artifacts.

    The non-git registry already suppresses these names when agent tools record
    changes.  Git workspaces should behave the same way even though they read
    from ``git status`` instead of recorded agent events.
    """
    env = _git_env()
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True, env=env)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "init"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        env=env,
    )

    ephemeral_files = [
        "pyproject.toml.tmp.12345",
        "pyproject.toml.tmp",
        "notes.md~",
        ".main.py.swp",
        ".main.py.swo",
        "#README.md#",
    ]
    for file_path in ephemeral_files:
        (tmp_path / file_path).write_text("temporary artifact")
    (tmp_path / "real_change.py").write_text("agent wrote this")

    reg = GitFilesystemRegistry(watch_path=tmp_path, git_root=tmp_path)
    results = reg.list_changed_files("any-conv", limit=100)

    paths = [r["path"] for r in results]
    assert paths == ["real_change.py"], (
        f"Expected only 'real_change.py', got {paths}. "
        "Git-backed changed files should suppress temp/editor artifacts."
    )
    for file_path in ephemeral_files:
        result = reg.get_changed_file("any-conv", file_path)
        assert result is None, (
            f"Expected get_changed_file to hide {file_path!r}, got {result!r}. "
            "Direct file lookup should match the changed-files list."
        )

    real_result = reg.get_changed_file("any-conv", "real_change.py")
    assert real_result is not None
    assert real_result["status"] == "created"


def test_git_list_changed_files_raises_on_timeout(tmp_path: Path, monkeypatch) -> None:
    """A ``git status`` timeout must raise, not silently return an empty list.

    The old code swallowed ``TimeoutExpired`` to ``[]``, so the Files panel
    showed "No workspace changes yet" even with real modifications — a state
    indistinguishable from a clean tree. The failure must surface so the
    endpoint can report it and the cause is no longer hidden.
    """
    env = _git_env()
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True, env=env)

    def _raise_timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="git status", timeout=5)

    monkeypatch.setattr("omnigent.runtime.filesystem_registry.subprocess.run", _raise_timeout)

    reg = GitFilesystemRegistry(watch_path=tmp_path, git_root=tmp_path)
    with pytest.raises(GitStatusUnavailable, match="timed out"):
        reg.list_changed_files("any-conv", limit=100)


def test_git_list_changed_files_raises_on_nonzero_exit(tmp_path: Path, monkeypatch) -> None:
    """A non-zero ``git status`` exit must raise, not silently return ``[]``.

    e.g. "detected dubious ownership" when the runner uid differs from the
    checkout owner — previously swallowed to an empty list.
    """
    env = _git_env()
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True, env=env)

    def _nonzero(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args="git status",
            returncode=128,
            stdout=b"",
            stderr=b"fatal: detected dubious ownership in repository",
        )

    monkeypatch.setattr("omnigent.runtime.filesystem_registry.subprocess.run", _nonzero)

    reg = GitFilesystemRegistry(watch_path=tmp_path, git_root=tmp_path)
    with pytest.raises(GitStatusUnavailable, match="exited 128"):
        reg.list_changed_files("any-conv", limit=100)


def test_git_get_changed_file_raises_on_timeout(tmp_path: Path, monkeypatch) -> None:
    """A ``git status`` timeout in the single-file lookup must raise, not return ``None``.

    Swallowing it to ``None`` made the diff endpoint answer 404 — a state
    indistinguishable from "this path has no changes" — for a read that
    *could not run*. The failure must surface like the list path.
    """
    env = _git_env()
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True, env=env)

    def _raise_timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="git status", timeout=5)

    monkeypatch.setattr("omnigent.runtime.filesystem_registry.subprocess.run", _raise_timeout)

    reg = GitFilesystemRegistry(watch_path=tmp_path, git_root=tmp_path)
    with pytest.raises(GitStatusUnavailable, match="timed out"):
        reg.get_changed_file("any-conv", "a.txt")


def test_git_get_changed_file_raises_on_nonzero_exit(tmp_path: Path, monkeypatch) -> None:
    """A non-zero ``git status`` exit in the single-file lookup must raise, not return ``None``."""
    env = _git_env()
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True, env=env)

    def _nonzero(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args="git status",
            returncode=128,
            stdout=b"",
            stderr=b"fatal: detected dubious ownership in repository",
        )

    monkeypatch.setattr("omnigent.runtime.filesystem_registry.subprocess.run", _nonzero)

    reg = GitFilesystemRegistry(watch_path=tmp_path, git_root=tmp_path)
    with pytest.raises(GitStatusUnavailable, match="exited 128"):
        reg.get_changed_file("any-conv", "a.txt")


def test_git_get_changed_file_returns_none_when_unchanged(tmp_path: Path) -> None:
    """A clean ``git status`` (exit 0, no output) still means "no changes" → ``None``.

    Guards against the raise paths swallowing the legitimate empty case: a
    tracked, unmodified file must return ``None``, not raise.
    """
    env = _git_env()
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True, env=env)
    (tmp_path / "a.txt").write_text("hello\n")
    subprocess.run(["git", "add", "a.txt"], cwd=tmp_path, check=True, capture_output=True, env=env)
    subprocess.run(
        ["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True, env=env
    )

    reg = GitFilesystemRegistry(watch_path=tmp_path, git_root=tmp_path)
    assert reg.get_changed_file("any-conv", "a.txt") is None


def test_git_list_changed_files_expands_untracked_nested_dir(tmp_path: Path) -> None:
    """A new file in a brand-new untracked directory tree returns its full path.

    Default ``git status --porcelain`` collapses an entirely-untracked directory
    to a single ``?? dir/`` line, so the Files panel would show the directory
    (stat'd as ~96 B) with an "A" badge instead of the actual added file. The
    ``--untracked-files=all`` flag forces git to expand the directory. Failure
    means the nested file is missing and only the top-level dir appears.
    """
    env = _git_env()
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True, env=env)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "init"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        env=env,
    )

    nested_rel = "projects/dais-2026-outlines/context/outlines/2026-06-01-revision.md"
    nested = tmp_path / nested_rel
    nested.parent.mkdir(parents=True)
    nested.write_text("outline content")

    reg = GitFilesystemRegistry(watch_path=tmp_path, git_root=tmp_path)
    results = reg.list_changed_files("any-conv", limit=100)

    paths = [r["path"] for r in results]
    assert nested_rel in paths, (
        f"Expected the full nested file path in results but got {paths}. "
        "git status is collapsing the untracked directory instead of expanding it."
    )
    # The bare directory must NOT appear as a phantom file.
    assert "projects" not in paths, (
        f"Expected no bare 'projects' directory entry but got {paths}. "
        "The untracked directory is masquerading as the added file."
    )
    record = next(r for r in results if r["path"] == nested_rel)
    assert record["status"] == "created", (
        f"Expected status 'created' for the new file, got {record['status']!r}."
    )


# ── git-status performance tuning (timeout / pathspec / untracked cache) ──────


def test_git_timeout_seconds_default_and_env_override(monkeypatch) -> None:
    """The git timeout defaults to 30s and honors the env override.

    Guards the large-repo headroom bump and the operator-tunable knob: unset
    → default, a valid positive value → that value, and invalid/non-positive
    values fall back to the default rather than raising or disabling the cap.
    """
    monkeypatch.delenv("OMNIGENT_GIT_STATUS_TIMEOUT_SECONDS", raising=False)
    assert _git_timeout_seconds() == pytest.approx(30.0)

    monkeypatch.setenv("OMNIGENT_GIT_STATUS_TIMEOUT_SECONDS", "90")
    assert _git_timeout_seconds() == pytest.approx(90.0)

    for bad in ("not-a-number", "0", "-5", ""):
        monkeypatch.setenv("OMNIGENT_GIT_STATUS_TIMEOUT_SECONDS", bad)
        assert _git_timeout_seconds() == pytest.approx(30.0), (
            f"Expected fallback to default for invalid value {bad!r}."
        )


def test_git_list_changed_files_honors_env_timeout(tmp_path: Path, monkeypatch) -> None:
    """``list_changed_files`` passes the env-overridden timeout to the subprocess.

    A slow-but-not-hung ``git status`` on a large repo must survive when the
    operator raises the timeout, instead of failing at the old 5s cap.
    """
    env = _git_env()
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True, env=env)
    monkeypatch.setenv("OMNIGENT_GIT_STATUS_TIMEOUT_SECONDS", "42")

    seen: dict[str, float | None] = {}

    def _capture(*_args, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        return subprocess.CompletedProcess(args="git", returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr("omnigent.runtime.filesystem_registry.subprocess.run", _capture)

    reg = GitFilesystemRegistry(watch_path=tmp_path, git_root=tmp_path)
    reg.list_changed_files("any-conv", limit=100)

    assert seen["timeout"] == pytest.approx(42.0), (
        f"Expected the env-overridden 42s timeout, got {seen['timeout']!r}."
    )


def test_git_list_changed_files_excludes_skip_dirs_via_pathspec(tmp_path: Path) -> None:
    """Untracked files inside ``_SKIP_DIRS`` are excluded and never returned.

    The ``:(exclude)`` pathspecs stop git from walking large build/cache trees
    (node_modules/ …). A real repo confirms both that git honors the pathspec
    (the skip-dir file is absent) and that a genuine change still surfaces.
    """
    env = _git_env()
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True, env=env)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "init"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        env=env,
    )

    # Root-level skip dir: must be pruned.
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "big.js").write_text("x" * 10)
    # A skip-dir name nested under a real source dir is NOT a root-level match,
    # so it stays visible — mirrors the first-component post-filter semantics.
    (tmp_path / "src" / "node_modules").mkdir(parents=True)
    (tmp_path / "src" / "node_modules" / "keep.js").write_text("y")
    (tmp_path / "real_change.py").write_text("agent wrote this")

    reg = GitFilesystemRegistry(watch_path=tmp_path, git_root=tmp_path)
    paths = [r["path"] for r in reg.list_changed_files("any-conv", limit=100)]

    assert "real_change.py" in paths, f"Expected 'real_change.py' in results but got {paths}."
    assert not any(p.startswith("node_modules/") for p in paths), (
        f"Root-level node_modules/ should be pruned but got {paths}."
    )
    assert "src/node_modules/keep.js" in paths, (
        f"Nested (non-root) node_modules should stay visible but got {paths}."
    )


def test_skip_dir_pathspecs_anchored_to_workspace_subdir(tmp_path: Path) -> None:
    """Pathspecs are anchored to the workspace's prefix within the git root.

    When the workspace is a subdirectory of the git root, the excludes must be
    prefixed with that subdir so a skip dir elsewhere in the repo is untouched.
    """
    git_root = tmp_path
    workspace = tmp_path / "sub" / "ws"
    workspace.mkdir(parents=True)

    reg = GitFilesystemRegistry(watch_path=workspace, git_root=git_root)
    specs = reg._skip_dir_pathspecs()

    assert ":(exclude)sub/ws/node_modules" in specs, (
        f"Expected workspace-prefixed exclude pathspec, got {specs}."
    )
    # No bare (unprefixed) skip-dir exclude should be present.
    assert ":(exclude)node_modules" not in specs


def test_untracked_cache_enabled_on_init(tmp_path: Path) -> None:
    """The registry enables ``core.untrackedCache`` on the repo at construction.

    This is the large-repo ``git status`` speedup (upstream git ≥ 2.8); the
    registry sets it best-effort on init. Verifies the config lands.
    """
    env = _git_env()
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True, env=env)

    GitFilesystemRegistry(watch_path=tmp_path, git_root=tmp_path)

    result = subprocess.run(
        ["git", "config", "--get", "core.untrackedCache"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.stdout.strip() == "true", (
        f"Expected core.untrackedCache=true after init, got {result.stdout.strip()!r}."
    )


def test_untracked_cache_failure_does_not_break_init(tmp_path: Path, monkeypatch) -> None:
    """A failure enabling the untracked cache must not break registry construction.

    The setting is a pure speedup; old git / read-only .git / mtime-unreliable
    filesystems should degrade silently rather than raising.
    """
    env = _git_env()
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True, env=env)

    def _raise_oserror(*_args, **_kwargs):
        raise OSError("git not found")

    monkeypatch.setattr("omnigent.runtime.filesystem_registry.subprocess.run", _raise_oserror)

    # Construction must not raise even though the config subprocess fails.
    GitFilesystemRegistry(watch_path=tmp_path, git_root=tmp_path)


def test_untracked_cache_config_written_once_per_root(tmp_path: Path, monkeypatch) -> None:
    """The ``git config`` write runs at most once per git-root per process.

    The host fallback path builds a fresh registry per fs request, so without
    the one-shot guard every request would re-spawn ``git config``. Building
    several registries on the same root must issue the config write only once.
    """
    from omnigent.runtime import filesystem_registry as fsr

    env = _git_env()
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True, env=env)

    # Reset the process-global guard so this test is order-independent.
    monkeypatch.setattr(fsr, "_untracked_cache_enabled", set())

    config_calls: list[tuple] = []
    real_run = subprocess.run

    def _counting_run(args, *a, **kw):
        if args[:3] == ["git", "config", "core.untrackedCache"]:
            config_calls.append(tuple(args))
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=b"", stderr=b"")
        return real_run(args, *a, **kw)

    monkeypatch.setattr(fsr.subprocess, "run", _counting_run)

    for _ in range(3):
        GitFilesystemRegistry(watch_path=tmp_path, git_root=tmp_path)

    assert len(config_calls) == 1, (
        f"Expected core.untrackedCache config write exactly once, got {len(config_calls)}."
    )


def test_untracked_cache_not_enabled_when_probe_fails(tmp_path: Path, monkeypatch) -> None:
    """When ``--test-untracked-cache`` fails, the cache config is not written.

    On mtime-unreliable filesystems git's probe exits non-zero; enabling the
    cache there risks a newly-untracked file missing from the panel, so the
    registry must leave ``core.untrackedCache`` unset.
    """
    from omnigent.runtime import filesystem_registry as fsr

    env = _git_env()
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True, env=env)

    monkeypatch.setattr(fsr, "_untracked_cache_enabled", set())

    config_calls: list[tuple] = []
    real_run = subprocess.run

    def _probe_fails_run(args, *a, **kw):
        if args[:2] == ["git", "update-index"]:
            return subprocess.CompletedProcess(
                args=args, returncode=1, stdout=b"", stderr=b"mtime unreliable"
            )
        if args[:3] == ["git", "config", "core.untrackedCache"]:
            config_calls.append(tuple(args))
        return real_run(args, *a, **kw)

    monkeypatch.setattr(fsr.subprocess, "run", _probe_fails_run)

    GitFilesystemRegistry(watch_path=tmp_path, git_root=tmp_path)

    assert config_calls == [], (
        f"Expected no config write when the probe fails, got {config_calls}."
    )
    result = subprocess.run(
        ["git", "config", "--get", "core.untrackedCache"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.stdout.strip() == "", (
        f"core.untrackedCache should be unset when the probe fails, got {result.stdout.strip()!r}."
    )


# ── _normalize_path ──────────────────────────────────────────────────────────


def test_normalize_path_absolute_path_is_made_relative(tmp_path: Path) -> None:
    """An absolute path under ``cwd`` is returned as a relative string.

    ``_normalize_path`` must strip the ``cwd`` prefix and return the
    remainder as a plain string.  Failure (returning the absolute path)
    means the ``p.relative_to(cwd)`` branch is not being taken.
    """
    cwd = tmp_path
    abs_path = str(cwd / "src" / "foo.py")

    result = _normalize_path(abs_path, cwd)

    # The absolute prefix must be stripped; only the relative tail remains.
    assert result == "src/foo.py", (
        f"Expected 'src/foo.py', got {result!r}. Absolute path under cwd was not made relative."
    )


def test_normalize_path_relative_path_passthrough(tmp_path: Path) -> None:
    """A relative path is returned unchanged.

    ``_normalize_path`` must not modify a path that is already relative.
    Failure means the relative branch is being incorrectly rewritten.
    """
    cwd = tmp_path

    result = _normalize_path("src/bar.py", cwd)

    # Relative path must pass through without modification.
    assert result == "src/bar.py", (
        f"Expected 'src/bar.py', got {result!r}. Relative path was modified unexpectedly."
    )


def test_normalize_path_absolute_outside_cwd_returns_none(tmp_path: Path) -> None:
    """An absolute path outside ``cwd`` is rejected (returns ``None``).

    The traversal-prevention logic must return ``None`` rather than letting
    an out-of-bounds path through to the registry.  Failure (returning the
    raw path string) would mean the traversal check is absent or broken.
    """
    cwd = (tmp_path / "sub").resolve()
    outside = "/etc/passwd"

    result = _normalize_path(outside, cwd)

    # Paths outside the workspace root must be rejected.
    assert result is None, (
        f"Expected None for out-of-bounds path, got {result!r}. "
        "Traversal check did not reject an absolute path outside cwd."
    )


def test_normalize_path_relative_traversal_returns_none(tmp_path: Path) -> None:
    """A relative path with ``..`` components that escapes ``cwd`` is rejected.

    ``../../etc/passwd`` resolves outside the workspace root and must return
    ``None``.  Failure (returning the raw traversal string) would let a
    caller-supplied path pollute the registry with misleading entries.
    """
    cwd = (tmp_path / "sub").resolve()

    result = _normalize_path("../../etc/passwd", cwd)

    # Relative traversal that escapes the workspace must be rejected.
    assert result is None, (
        f"Expected None for escaping relative path, got {result!r}. "
        "Traversal check did not reject a '../..' path that exits cwd."
    )


def test_normalize_path_relative_dotdot_within_cwd_is_normalized(tmp_path: Path) -> None:
    """A ``..`` path that stays within ``cwd`` is normalized, not rejected.

    ``src/../foo.py`` resolves to ``foo.py`` inside the workspace and must
    be returned as the normalized relative form.  Failure (returning ``None``)
    would incorrectly block legitimate paths with redundant ``..`` segments.
    """
    cwd = tmp_path.resolve()

    result = _normalize_path("src/../foo.py", cwd)

    # Safe traversal that stays within the workspace must survive.
    assert result == "foo.py", (
        f"Expected 'foo.py' after normalizing 'src/../foo.py', got {result!r}. "
        "In-bounds '..' traversal was incorrectly rejected."
    )


# ── create_filesystem_registry factory ───────────────────────────────────────


def test_create_filesystem_registry_git_workspace(tmp_path: Path) -> None:
    """A directory with a .git subdirectory yields :class:`GitFilesystemRegistry`.

    Failure means the factory's _find_git_root detection is broken and git
    workspaces would fall back to the plain agent-edit registry, losing
    git-backed baseline support.
    """
    (tmp_path / ".git").mkdir()
    registry = create_filesystem_registry(tmp_path)
    assert isinstance(registry, GitFilesystemRegistry), (
        f"Expected GitFilesystemRegistry for a git workspace, got {type(registry).__name__}. "
        "The factory's _find_git_root detection may be broken."
    )


def test_create_filesystem_registry_plain_dir(tmp_path: Path) -> None:
    """A plain directory (no .git) yields :class:`AgentEditFilesystemRegistry`.

    Failure means the factory is incorrectly treating non-git workspaces as git.
    """
    registry = create_filesystem_registry(tmp_path)
    assert isinstance(registry, AgentEditFilesystemRegistry), (
        f"Expected AgentEditFilesystemRegistry for a plain dir, got {type(registry).__name__}. "
        "The factory may be finding a .git directory it shouldn't."
    )


def test_create_filesystem_registry_nested_git_workspace(tmp_path: Path) -> None:
    """A subdirectory inside a git repo yields :class:`GitFilesystemRegistry`.

    Failure means _find_git_root doesn't walk parent directories, so nested
    workspaces (agent sandboxes inside a repo) would incorrectly use the
    plain agent-edit registry and lose git-backed baseline support.
    """
    (tmp_path / ".git").mkdir()
    nested = tmp_path / "subdir" / "workspace"
    nested.mkdir(parents=True)
    registry = create_filesystem_registry(nested)
    assert isinstance(registry, GitFilesystemRegistry), (
        f"Expected GitFilesystemRegistry for a nested git workspace, "
        f"got {type(registry).__name__}. "
        "_find_git_root may not be walking parent directories."
    )


# ── _parse_git_porcelain_line ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "line, expected",
    [
        # Untracked file (both columns '?') → created
        ("?? new_file.py", ("new_file.py", "created")),
        # Staged new file (index 'A') → created
        ("A  staged.py", ("staged.py", "created")),
        # Staged new + modified in worktree (index 'A' takes precedence) → created
        ("AM staged_then_modified.py", ("staged_then_modified.py", "created")),
        # Staged modification (index 'M') → modified
        ("M  staged_mod.py", ("staged_mod.py", "modified")),
        # Unstaged modification (worktree 'M') → modified
        (" M unstaged_mod.py", ("unstaged_mod.py", "modified")),
        # Both staged and unstaged modifications → modified
        ("MM both_mod.py", ("both_mod.py", "modified")),
        # Staged deletion (index 'D') → deleted
        ("D  staged_del.py", ("staged_del.py", "deleted")),
        # Unstaged deletion (worktree 'D') → deleted
        (" D unstaged_del.py", ("unstaged_del.py", "deleted")),
        # Rename: destination path (after ' -> ') is used, operation is modified
        ("R  old.py -> new.py", ("new.py", "modified")),
        # git-quoted path (spaces in filename) → quotes are stripped
        ('?? "dir/file with spaces.py"', ("dir/file with spaces.py", "created")),
        # Quoted rename destination
        ('R  old.py -> "new with spaces.py"', ("new with spaces.py", "modified")),
        # Both source and destination git-quoted (both paths have spaces).
        # The outer-quote strip must NOT fire before the ' -> ' split —
        # 'R  "old name.py" -> "new name.py"' starts and ends with '"' so
        # a naive strip would corrupt the separator and leave a dangling quote.
        ('R  "old name.py" -> "new name.py"', ("new name.py", "modified")),
        # Non-rename file whose name literally contains ' -> ': must NOT be
        # treated as a rename — the old path-content heuristic would misfire here.
        (" M file -> backup.py", ("file -> backup.py", "modified")),
        # Git C-quoted non-ASCII filename (UTF-8 bytes as octal sequences).
        # git encodes 'é' (U+00E9) as the two UTF-8 bytes \303\251.
        ('?? "caf\\303\\251.py"', ("café.py", "created")),
        # Lines shorter than 4 characters → None (no valid XY + space + path)
        ("", None),
        ("??", None),
        ("M ", None),
    ],
    ids=[
        "untracked",
        "staged-new",
        "staged-new-and-modified",
        "staged-modified",
        "unstaged-modified",
        "both-staged-and-unstaged-modified",
        "staged-deleted",
        "unstaged-deleted",
        "rename",
        "quoted-path-with-spaces",
        "quoted-rename-destination",
        "quoted-rename-both-sides",
        "modified-filename-with-arrow",
        "non-ascii-octal-quoted",
        "empty-line",
        "two-char-line",
        "three-char-line",
    ],
)
def test_parse_git_porcelain_line(line: str, expected: tuple[str, str] | None) -> None:
    """``_parse_git_porcelain_line`` maps every ``git status --porcelain`` status code correctly.

    Failure on any case means the corresponding operation will be misclassified
    in the Files panel (e.g. a deleted file shown as modified, or a rename
    showing the source path instead of the destination).
    """
    result = _parse_git_porcelain_line(line)

    assert result == expected, (
        f"_parse_git_porcelain_line({line!r}) returned {result!r}, expected {expected!r}."
    )


# ── _unquote_git_path ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw, expected",
    [
        # Plain ASCII — no escaping needed
        ("hello.py", "hello.py"),
        # Escaped double-quote and backslash
        (r"say \"hi\"", 'say "hi"'),
        (r"back\\slash", "back\\slash"),
        # Simple escape sequences
        ("tab\\there", "tab\there"),
        ("new\\nline", "new\nline"),
        # UTF-8 non-ASCII via octal (é = 0xC3 0xA9 = \303\251)
        ("caf\\303\\251.py", "café.py"),
        # Multi-byte sequence: ñ = 0xC3 0xB1 = \303\261
        ("ma\\303\\261ana", "mañana"),
    ],
    ids=[
        "plain-ascii",
        "escaped-quotes",
        "escaped-backslash",
        "tab-escape",
        "newline-escape",
        "non-ascii-two-byte-utf8",
        "non-ascii-spanish",
    ],
)
def test_unquote_git_path(raw: str, expected: str) -> None:
    """``_unquote_git_path`` correctly reverses git's C-quoting escape sequences.

    Failure means non-ASCII or specially-named files will appear with garbled
    paths in the Files panel and diff endpoint lookups will fail to find them.
    """
    result = _unquote_git_path(raw)
    assert result == expected, (
        f"_unquote_git_path({raw!r}) returned {result!r}, expected {expected!r}."
    )


# ── Git-mode line counts (numstat) ─────────────────────────────────────────


def _init_git_with_commit(tmp_path: Path, name: str, content: str) -> dict[str, str]:
    """Init a git repo in *tmp_path* with one committed file, return the env."""
    env = _git_env()
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True, env=env)
    (tmp_path / name).write_text(content)
    subprocess.run(["git", "add", name], cwd=tmp_path, check=True, capture_output=True, env=env)
    subprocess.run(
        ["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True, env=env
    )
    return env


def test_git_line_counts_modified(tmp_path: Path) -> None:
    """A tracked modified file reports added/removed line counts from numstat."""
    _init_git_with_commit(tmp_path, "f.py", "a\nb\nc\n")
    (tmp_path / "f.py").write_text("a\nB\nc\nd\n")  # 1 line changed, 1 added → +2 -1

    reg = GitFilesystemRegistry(watch_path=tmp_path, git_root=tmp_path)
    [rec] = reg.list_changed_files("any-conv", limit=100)
    assert rec["status"] == "modified"
    assert rec["lines_added"] == 2, rec
    assert rec["lines_removed"] == 1, rec


def test_git_line_counts_untracked_is_none(tmp_path: Path) -> None:
    """An untracked new file (absent from `git diff HEAD`) reports no counts.

    Counts come only from numstat; git doesn't diff untracked files, so the UI
    omits the counter for them — matching VS Code / Cursor's source-control view.
    """
    _init_git_with_commit(tmp_path, "committed.py", "x\n")
    (tmp_path / "new.py").write_text("one\ntwo\nthree\n")

    reg = GitFilesystemRegistry(watch_path=tmp_path, git_root=tmp_path)
    rec = next(r for r in reg.list_changed_files("any-conv", limit=100) if r["path"] == "new.py")
    assert rec["status"] == "created"
    assert rec["lines_added"] is None, rec
    assert rec["lines_removed"] is None, rec


def test_git_line_counts_staged_new_file_added_only(tmp_path: Path) -> None:
    """A staged new file IS in `git diff HEAD`, so it reports adds from numstat."""
    _init_git_with_commit(tmp_path, "committed.py", "x\n")
    (tmp_path / "new.py").write_text("one\ntwo\nthree\n")
    subprocess.run(
        ["git", "add", "new.py"], cwd=tmp_path, check=True, capture_output=True, env=_git_env()
    )

    reg = GitFilesystemRegistry(watch_path=tmp_path, git_root=tmp_path)
    rec = next(r for r in reg.list_changed_files("any-conv", limit=100) if r["path"] == "new.py")
    assert rec["status"] == "created"
    assert rec["lines_added"] == 3, rec
    assert rec["lines_removed"] == 0, rec


def test_git_line_counts_deleted_removed_only(tmp_path: Path) -> None:
    """A deleted tracked file reports removed lines only (added side is 0)."""
    _init_git_with_commit(tmp_path, "gone.py", "a\nb\nc\n")
    (tmp_path / "gone.py").unlink()

    reg = GitFilesystemRegistry(watch_path=tmp_path, git_root=tmp_path)
    [rec] = reg.list_changed_files("any-conv", limit=100)
    assert rec["status"] == "deleted"
    assert rec["lines_added"] == 0, rec
    assert rec["lines_removed"] == 3, rec


def test_git_line_counts_binary_is_none(tmp_path: Path) -> None:
    """A binary file reports (None, None) — numstat emits `-\\t-`."""
    _init_git_with_commit(tmp_path, "keep.txt", "hi\n")
    (tmp_path / "img.bin").write_bytes(bytes(range(256)) * 4)
    subprocess.run(
        ["git", "add", "img.bin"], cwd=tmp_path, check=True, capture_output=True, env=_git_env()
    )

    reg = GitFilesystemRegistry(watch_path=tmp_path, git_root=tmp_path)
    rec = next(r for r in reg.list_changed_files("any-conv", limit=100) if r["path"] == "img.bin")
    assert rec["lines_added"] is None, rec
    assert rec["lines_removed"] is None, rec


def test_git_line_counts_numstat_failure_degrades_but_status_intact(
    tmp_path: Path, monkeypatch
) -> None:
    """If numstat fails, counts are None but the status list still renders.

    Only the numstat subprocess is broken; `git status` still runs, so the
    file must still appear (with counts degraded to None) rather than the whole
    list failing.
    """
    _init_git_with_commit(tmp_path, "f.py", "a\nb\n")
    (tmp_path / "f.py").write_text("a\nb\nc\n")

    reg = GitFilesystemRegistry(watch_path=tmp_path, git_root=tmp_path)
    real_run = subprocess.run

    def _fail_numstat(argv, *args, **kwargs):
        if isinstance(argv, list) and "--numstat" in argv:
            raise subprocess.TimeoutExpired(cmd="git diff --numstat", timeout=5)
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr("omnigent.runtime.filesystem_registry.subprocess.run", _fail_numstat)

    [rec] = reg.list_changed_files("any-conv", limit=100)
    assert rec["status"] == "modified"
    assert rec["lines_added"] is None, rec
    assert rec["lines_removed"] is None, rec

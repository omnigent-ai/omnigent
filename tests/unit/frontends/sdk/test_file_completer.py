"""Tests for the ``@``-mention file completer and extraction helpers.

Covers trigger detection, fuzzy matching, recursive file scanning,
attachment extraction, and text stripping — all using temporary
directories so no real filesystem state leaks.
"""

from __future__ import annotations

import os
import pathlib

from omnigent_ui_sdk.terminal._completer import (
    FileMentionCompleter,
    _find_at_mention,
    _fuzzy_score,
    _is_hidden,
    _list_files,
    _walk_files,
    extract_at_mentions,
    strip_at_mentions,
)
from omnigent_ui_sdk.terminal._host import PendingAttachment
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document

# ---------------------------------------------------------------------------
# _find_at_mention
# ---------------------------------------------------------------------------


def test_find_at_mention_start_of_line():
    """Bare ``@query`` at position 0 triggers."""
    result = _find_at_mention("@host")
    assert result is not None
    query, start = result
    assert query == "host"
    assert start == -5  # len("@host")


def test_find_at_mention_mid_line():
    """``@`` preceded by a space triggers."""
    result = _find_at_mention("check @main")
    assert result is not None
    query, start = result
    assert query == "main"
    assert start == -5


def test_find_at_mention_bare_at():
    """Bare ``@`` with no query text triggers (lists all files)."""
    result = _find_at_mention("look @")
    assert result is not None
    query, start = result
    assert query == ""
    assert start == -1


def test_find_at_mention_email_no_trigger():
    """``@`` inside a word (email) does NOT trigger."""
    assert _find_at_mention("user@domain.com") is None


def test_find_at_mention_no_at():
    """No ``@`` at all returns ``None``."""
    assert _find_at_mention("plain text") is None


def test_find_at_mention_at_with_space_after():
    """``@`` followed by whitespace does NOT trigger (query has spaces)."""
    assert _find_at_mention("@ foo") is None


def test_find_at_mention_start_bare():
    """Bare ``@`` at start of line triggers."""
    result = _find_at_mention("@")
    assert result is not None
    query, start = result
    assert query == ""
    assert start == -1


# ---------------------------------------------------------------------------
# _fuzzy_score
# ---------------------------------------------------------------------------


def test_fuzzy_score_exact_prefix():
    """Exact prefix match returns a positive score."""
    score = _fuzzy_score("host", "_host.py")
    assert score is not None
    assert score > 0


def test_fuzzy_score_scattered_chars():
    """Characters scattered across candidate still match."""
    score = _fuzzy_score("hp", "_host.py")
    assert score is not None
    assert score > 0


def test_fuzzy_score_no_match():
    """Non-matching query returns ``None``."""
    assert _fuzzy_score("xyz", "_host.py") is None


def test_fuzzy_score_empty_query():
    """Empty query matches everything with score 0."""
    assert _fuzzy_score("", "anything.py") == 0


def test_fuzzy_score_case_insensitive():
    """Matching is case-insensitive."""
    score = _fuzzy_score("HOST", "_host.py")
    assert score is not None
    assert score > 0


def test_fuzzy_score_consecutive_beats_scattered():
    """Consecutive character matches score higher than scattered ones."""
    consecutive = _fuzzy_score("main", "main.py")
    scattered = _fuzzy_score("main", "m_a_i_n.py")
    assert consecutive is not None
    assert scattered is not None
    assert consecutive > scattered


def test_fuzzy_score_boundary_bonus():
    """Matching at word boundaries (after ``/``, ``_``, ``.``) scores higher."""
    # "mp" in "main.py" hits the 'm' at pos 0 (boundary) and 'p' after '.'
    boundary = _fuzzy_score("mp", "main.py")
    # "mp" in "lamp.txt" — 'm' is mid-word, 'p' is consecutive but not boundary
    mid_word = _fuzzy_score("mp", "lamp.txt")
    assert boundary is not None
    assert mid_word is not None
    assert boundary > mid_word


def test_fuzzy_score_case_exact_bonus():
    """Exact-case match gets a small bonus over case-insensitive match."""
    exact = _fuzzy_score("Host", "Host.py")
    inexact = _fuzzy_score("host", "Host.py")
    assert exact is not None
    assert inexact is not None
    assert exact > inexact


# ---------------------------------------------------------------------------
# _walk_files
# ---------------------------------------------------------------------------


def test_walk_files_basic(tmp_path: pathlib.Path):
    """Direct children are returned."""
    (tmp_path / "main.py").touch()
    (tmp_path / "readme.md").touch()
    files = _walk_files(str(tmp_path))
    assert sorted(files) == ["main.py", "readme.md"]


def test_walk_files_subdirectory(tmp_path: pathlib.Path):
    """Files in subdirectories include the relative path."""
    sub = tmp_path / "src"
    sub.mkdir()
    (sub / "utils.py").touch()
    files = _walk_files(str(tmp_path))
    assert "src/utils.py" in files


def test_walk_files_hidden_excluded(tmp_path: pathlib.Path):
    """Hidden files and directories are excluded."""
    (tmp_path / ".hidden_file").touch()
    hidden_dir = tmp_path / ".git"
    hidden_dir.mkdir()
    (hidden_dir / "config").touch()
    (tmp_path / "visible.py").touch()
    files = _walk_files(str(tmp_path))
    assert files == ["visible.py"]


def test_walk_files_deep_nesting(tmp_path: pathlib.Path):
    """Deeply nested files are included (no depth limit)."""
    deep = tmp_path / "a" / "b" / "c" / "d"
    deep.mkdir(parents=True)
    (deep / "deep.py").touch()
    files = _walk_files(str(tmp_path))
    assert "a/b/c/d/deep.py" in files


# ---------------------------------------------------------------------------
# _is_hidden
# ---------------------------------------------------------------------------


def test_is_hidden_dotfile():
    """A path with a dot-prefixed component is hidden."""
    assert _is_hidden(".git/config") is True
    assert _is_hidden("src/.env") is True


def test_is_hidden_normal():
    """A path with no dot-prefixed components is not hidden."""
    assert _is_hidden("src/main.py") is False


# ---------------------------------------------------------------------------
# _list_files (git ls-files integration)
# ---------------------------------------------------------------------------


def test_list_files_respects_gitignore(tmp_path: pathlib.Path):
    """Files matched by ``.gitignore`` are excluded from git-based listing."""
    import subprocess

    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=tmp_path,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=tmp_path,
        capture_output=True,
        check=True,
    )
    (tmp_path / ".gitignore").write_text("build/\n*.log\n")
    (tmp_path / "main.py").touch()
    build_dir = tmp_path / "build"
    build_dir.mkdir()
    (build_dir / "output.js").touch()
    (tmp_path / "debug.log").touch()
    # Stage tracked files so git ls-files --cached returns them.
    subprocess.run(
        ["git", "add", "main.py", ".gitignore"],
        cwd=tmp_path,
        capture_output=True,
        check=True,
    )
    files = _list_files(str(tmp_path))
    assert "main.py" in files
    assert "build/output.js" not in files
    assert "debug.log" not in files


def test_list_files_falls_back_to_walk(tmp_path: pathlib.Path):
    """Non-git directories fall back to ``_walk_files``."""
    (tmp_path / "hello.py").touch()
    # tmp_path is not a git repo, so _list_files should fall back.
    files = _list_files(str(tmp_path))
    assert "hello.py" in files


# ---------------------------------------------------------------------------
# FileMentionCompleter.get_completions
# ---------------------------------------------------------------------------


def _completions_for(text: str, cwd: str) -> list[str]:
    """Helper: run the completer and return display texts as plain strings."""
    completer = FileMentionCompleter(cwd=cwd)
    doc = Document(text, cursor_position=len(text))
    event = CompleteEvent(text_inserted=False, completion_requested=False)
    results: list[str] = []
    for c in completer.get_completions(doc, event):
        # ``Completion.display`` may be ``FormattedText`` or ``str``.
        display = c.display
        if isinstance(display, str):
            results.append(display)
        else:
            # FormattedText is a list of (style, text) tuples.
            results.append("".join(t for _, t in display))
    return results


def test_completer_exact_prefix(tmp_path: pathlib.Path):
    """Prefix query returns matching files."""
    (tmp_path / "main.py").touch()
    (tmp_path / "readme.md").touch()
    results = _completions_for("@main", str(tmp_path))
    assert "main.py" in results
    # "readme.md" does not match "main" as fuzzy (r doesn't precede m)
    # but actually "main" in "readme" won't match because 'a' comes
    # before 'i' — let's just check main.py is there
    assert "main.py" in results


def test_completer_fuzzy_match(tmp_path: pathlib.Path):
    """Fuzzy query ``hostp`` matches ``_host.py``."""
    (tmp_path / "_host.py").touch()
    results = _completions_for("@hostp", str(tmp_path))
    assert "_host.py" in results


def test_completer_bare_at_lists_all(tmp_path: pathlib.Path):
    """Bare ``@`` lists all files."""
    (tmp_path / "a.py").touch()
    (tmp_path / "b.txt").touch()
    results = _completions_for("@", str(tmp_path))
    assert sorted(results) == ["a.py", "b.txt"]


def test_completer_no_match(tmp_path: pathlib.Path):
    """Query with no matches yields nothing."""
    (tmp_path / "main.py").touch()
    results = _completions_for("@zzz", str(tmp_path))
    assert results == []


def test_completer_email_no_trigger(tmp_path: pathlib.Path):
    """Email-like text does not trigger completions."""
    (tmp_path / "main.py").touch()
    results = _completions_for("user@main", str(tmp_path))
    assert results == []


def test_completer_subdirectory_files(tmp_path: pathlib.Path):
    """Files in subdirectories are listed with relative paths."""
    sub = tmp_path / "src"
    sub.mkdir()
    (sub / "app.py").touch()
    results = _completions_for("@src", str(tmp_path))
    assert "src/app.py" in results


def test_completer_inserts_at_prefix(tmp_path: pathlib.Path):
    """Completion text includes the ``@`` prefix."""
    (tmp_path / "main.py").touch()
    completer = FileMentionCompleter(cwd=str(tmp_path))
    doc = Document("@main", cursor_position=5)
    event = CompleteEvent(text_inserted=False, completion_requested=False)
    completions = list(completer.get_completions(doc, event))
    assert any(c.text == "@main.py" for c in completions)


# ---------------------------------------------------------------------------
# extract_at_mentions
# ---------------------------------------------------------------------------


def test_extract_existing_file(tmp_path: pathlib.Path):
    """``@filename`` for an existing file returns a PendingAttachment."""
    (tmp_path / "data.csv").touch()
    result = extract_at_mentions("check @data.csv", cwd=str(tmp_path))
    assert len(result) == 1
    assert result[0].path == str((tmp_path / "data.csv").resolve())
    assert result[0].is_image is False


def test_extract_nonexistent_file(tmp_path: pathlib.Path):
    """``@filename`` for a nonexistent file returns empty list."""
    result = extract_at_mentions("check @missing.txt", cwd=str(tmp_path))
    assert result == []


def test_extract_image_detection(tmp_path: pathlib.Path):
    """Image files are detected via extension."""
    (tmp_path / "screenshot.png").touch()
    result = extract_at_mentions("look @screenshot.png", cwd=str(tmp_path))
    assert len(result) == 1
    assert result[0].is_image is True


def test_extract_subdirectory(tmp_path: pathlib.Path):
    """Relative paths in subdirectories resolve correctly."""
    sub = tmp_path / "src"
    sub.mkdir()
    (sub / "main.py").touch()
    result = extract_at_mentions("@src/main.py", cwd=str(tmp_path))
    assert len(result) == 1
    assert result[0].path == str((sub / "main.py").resolve())


def test_extract_bare_at_ignored():
    """Bare ``@`` without a filename produces no attachments."""
    result = extract_at_mentions("@ something")
    assert result == []


def test_extract_multiple_mentions(tmp_path: pathlib.Path):
    """Multiple ``@filename`` tokens in one line all resolve."""
    (tmp_path / "a.py").touch()
    (tmp_path / "b.py").touch()
    result = extract_at_mentions("@a.py and @b.py", cwd=str(tmp_path))
    assert len(result) == 2


# ---------------------------------------------------------------------------
# strip_at_mentions
# ---------------------------------------------------------------------------


def test_strip_removes_resolved_tokens(tmp_path: pathlib.Path):
    """Resolved ``@filename`` tokens are removed from display text."""
    (tmp_path / "data.csv").touch()
    attachments = [
        PendingAttachment(
            path=str((tmp_path / "data.csv").resolve()),
            is_image=False,
        )
    ]
    old_cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        result = strip_at_mentions("check @data.csv for bugs", attachments)
    finally:
        os.chdir(old_cwd)
    assert "@data.csv" not in result
    assert "data.csv" not in result
    assert "check" in result
    assert "bugs" in result


def test_strip_preserves_non_at_tokens(tmp_path: pathlib.Path):
    """Non-``@`` tokens are preserved."""
    attachments: list[PendingAttachment] = []
    old_cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        result = strip_at_mentions("hello world", attachments)
    finally:
        os.chdir(old_cwd)
    assert result == "hello world"


def test_strip_preserves_unresolved_at_tokens(tmp_path: pathlib.Path):
    """``@filename`` tokens not in attachments are preserved."""
    attachments: list[PendingAttachment] = []
    old_cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        result = strip_at_mentions("check @missing.txt", attachments)
    finally:
        os.chdir(old_cwd)
    assert "@missing.txt" in result

"""Unit tests for the overwrite diff carried by ``_write_impl`` results.

A full-file write replaces content the caller never restates, so the write
result is the only place the "what changed" story can be told. ``_write_impl``
captures the pre-write content and reports a unified ``diff`` field that
frontends render instead of a raw full-content dump.
"""

from __future__ import annotations

from pathlib import Path

from omnigent.inner.os_env import (
    _MAX_WRITE_DIFF_CHARS,
    _write_impl,
)

_ORIGINAL = "alpha\ntimeout = 30\nomega\n"
_UPDATED = "alpha\ntimeout = 60\nomega\n"


def test_overwrite_reports_unified_diff(tmp_path: Path) -> None:
    """Overwriting an existing text file yields a diff naming both sides."""
    target = tmp_path / "config.py"
    target.write_text(_ORIGINAL, encoding="utf-8")

    result = _write_impl(target, _UPDATED)

    assert result["created"] is False
    diff = result["diff"]
    assert isinstance(diff, str)
    assert "-timeout = 30" in diff
    assert "+timeout = 60" in diff
    # Unified headers label the file so a renderer can title the hunk.
    assert f"--- {target}" in diff
    assert f"+++ {target}" in diff


def test_new_file_write_has_no_diff(tmp_path: Path) -> None:
    """Creating a file has no "before" side, so no diff field appears."""
    target = tmp_path / "fresh.txt"

    result = _write_impl(target, "hello\n")

    assert result["created"] is True
    assert "diff" not in result


def test_identical_rewrite_has_no_diff(tmp_path: Path) -> None:
    """Rewriting identical content produces no diff noise."""
    target = tmp_path / "same.txt"
    target.write_text(_ORIGINAL, encoding="utf-8")

    result = _write_impl(target, _ORIGINAL)

    assert "diff" not in result


def test_binary_overwrite_has_no_diff(tmp_path: Path) -> None:
    """A binary "before" side is not diffable; the write still succeeds."""
    target = tmp_path / "blob.bin"
    target.write_bytes(b"\x00\x01\x02binary")

    result = _write_impl(target, "now text\n")

    assert result["bytes_written"] == len(b"now text\n")
    assert "diff" not in result


def test_huge_diff_is_truncated(tmp_path: Path) -> None:
    """The diff is capped so tool results can't blow up the transcript."""
    target = tmp_path / "big.txt"
    target.write_text("\n".join(f"old line {i}" for i in range(2_000)) + "\n", encoding="utf-8")

    result = _write_impl(target, "\n".join(f"new line {i}" for i in range(2_000)) + "\n")

    diff = result["diff"]
    assert isinstance(diff, str)
    assert len(diff) <= _MAX_WRITE_DIFF_CHARS + 100  # cap plus the truncation notice
    assert "[diff truncated:" in diff


def test_unterminated_final_line_diffs_cleanly(tmp_path: Path) -> None:
    """A missing trailing newline must not glue diff lines together."""
    target = tmp_path / "no_newline.txt"
    target.write_text("value = 1", encoding="utf-8")

    result = _write_impl(target, "value = 2")

    diff = result["diff"]
    assert isinstance(diff, str)
    assert "-value = 1\n" in diff
    assert "+value = 2\n" in diff

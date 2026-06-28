"""Tests for the REPL "!" shell-passthrough handler.

`!<cmd>` runs `<cmd>` in the user's shell, shows the output, and returns a
model-facing block that is folded into the next agent turn. Covers the pure
helpers (clip + context builder) and the async runner against real (POSIX)
commands, including non-zero exit, stderr, no-output, ANSI stripping, output
capping, and timeout.
"""

from __future__ import annotations

import os
import pathlib

import pytest

from omnigent.repl import _repl
from omnigent.repl._repl import (
    _bang_shell_argv,
    _build_bang_context,
    _clip_text,
    _resolve_cd,
    _run_bang_command,
    _write_bang_overflow,
)


class _FakeHost:
    """Collects whatever the runner writes to the terminal."""

    def __init__(self) -> None:
        self.items: list = []

    def output(self, item: object) -> None:
        self.items.append(item)


class _FakeFmt:
    accent = "cyan"
    muted = "dim"
    warning = "yellow"


# ── _clip_text ──────────────────────────────────────────


def test_clip_text_under_limit_is_unchanged() -> None:
    assert _clip_text("short", 100) == "short"


def test_clip_text_over_limit_keeps_head_tail_with_marker() -> None:
    out = _clip_text("A" * 50 + "B" * 50, 40)
    assert "chars truncated" in out
    assert out.startswith("A")  # head preserved
    assert out.endswith("B")  # tail preserved
    assert len(out) < 100


# ── _build_bang_context (model-facing block) ────────────


def test_build_context_has_command_exit_and_stdout_fence() -> None:
    block = _build_bang_context("ls", "a\nb\n", "", "exit: 0")
    assert "$ ls" in block
    assert "exit: 0" in block
    assert "```stdout" in block and "a\nb" in block
    assert "```stderr" not in block  # no stderr → no stderr fence


def test_build_context_includes_stderr_fence_when_present() -> None:
    block = _build_bang_context("x", "", "boom\n", "exit: 1")
    assert "```stderr" in block and "boom" in block


def test_build_context_no_output_marker() -> None:
    block = _build_bang_context("true", "", "", "exit: 0")
    assert "(no output)" in block
    assert "```" not in block


def test_build_context_strips_ansi_for_the_model() -> None:
    block = _build_bang_context("c", "\x1b[31mred\x1b[0m text", "", "exit: 0")
    assert "red text" in block
    assert "\x1b[" not in block  # escapes removed


def test_build_context_caps_huge_output() -> None:
    block = _build_bang_context("big", "X" * (_repl._BANG_CONTEXT_MAX + 5000), "", "exit: 0")
    assert "chars truncated" in block


# ── _run_bang_command (async, real subprocess; POSIX shell semantics) ───


@pytest.mark.posix_only
async def test_run_echo_captures_stdout_and_exit_zero() -> None:
    block = await _run_bang_command("echo hello", _FakeHost(), _FakeFmt())
    assert "hello" in block
    assert "exit: 0" in block


@pytest.mark.posix_only
async def test_run_renders_to_the_host() -> None:
    host = _FakeHost()
    await _run_bang_command("echo hi", host, _FakeFmt())
    assert host.items  # echo line + output + footer were written


@pytest.mark.posix_only
async def test_run_nonzero_exit_is_reported_not_fatal() -> None:
    block = await _run_bang_command("exit 3", _FakeHost(), _FakeFmt())
    assert "exit: 3" in block


@pytest.mark.posix_only
async def test_run_captures_stderr() -> None:
    block = await _run_bang_command("echo oops 1>&2", _FakeHost(), _FakeFmt())
    assert "```stderr" in block and "oops" in block


@pytest.mark.posix_only
async def test_run_timeout_kills_and_reports(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_repl, "_BANG_TIMEOUT_S", 0.2)
    block = await _run_bang_command("sleep 5", _FakeHost(), _FakeFmt())
    assert "timed out" in block


@pytest.mark.posix_only
async def test_run_respects_cwd(tmp_path: pathlib.Path) -> None:
    (tmp_path / "marker.txt").write_text("x")
    block = await _run_bang_command("ls", _FakeHost(), _FakeFmt(), cwd=str(tmp_path))
    assert "marker.txt" in block


# ── cross-platform shell selection ──────────────────────


@pytest.mark.posix_only
def test_shell_argv_posix() -> None:
    assert _bang_shell_argv("echo hi")[-2:] == ["-c", "echo hi"]


def test_shell_argv_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("os.name", "nt")
    monkeypatch.setenv("COMSPEC", r"C:\Windows\System32\cmd.exe")
    assert _bang_shell_argv("dir") == [r"C:\Windows\System32\cmd.exe", "/c", "dir"]


# ── _resolve_cd (lightweight cwd persistence) ───────────


@pytest.mark.posix_only
def test_resolve_cd_absolute_relative_and_home() -> None:
    assert _resolve_cd("cd /tmp", "/work") == "/tmp"
    assert _resolve_cd("cd sub", "/work") == "/work/sub"
    assert _resolve_cd("cd", "/work") == os.path.expanduser("~")
    assert _resolve_cd("cd ~", "/work") == os.path.expanduser("~")


def test_resolve_cd_none_for_non_cd_or_compound() -> None:
    assert _resolve_cd("ls", "/work") is None
    assert _resolve_cd("cd a && ls", "/work") is None  # compound → not handled
    assert _resolve_cd("cdfoo", "/work") is None


# ── _write_bang_overflow (huge-output temp file) ────────


def test_write_overflow_none_when_small() -> None:
    assert _write_bang_overflow("c", "small", "") is None


def test_write_overflow_writes_full_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_repl, "_BANG_CONTEXT_MAX", 10)
    big = "Z" * 50
    path = _write_bang_overflow("mycmd", big, "")
    assert path is not None
    content = pathlib.Path(path).read_text()
    assert "mycmd" in content and big in content
    pathlib.Path(path).unlink()


def test_build_context_notes_overflow_path() -> None:
    block = _build_bang_context("c", "x", "", "exit: 0", overflow_path="/tmp/x.log")
    assert "/tmp/x.log" in block and "full output" in block

"""The native wrappers hand an initial prompt to the TUI through argv."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omnigent import claude_native

_MULTILINE = "line one\nline two\n\n  indented third"


@pytest.fixture(autouse=True)
def _tools_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend claude + tmux are installed so preflight passes."""
    monkeypatch.setattr(claude_native.shutil, "which", lambda command: f"/usr/bin/{command}")
    monkeypatch.setattr(claude_native, "resolve_native_claude_config", lambda spec: None)


def _capture_remote(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Record what ``run_claude_native`` hands the remote launch path."""
    captured: dict[str, Any] = {}

    def _fake_remote(base_url: str, spec_path: Path, **kwargs: Any) -> None:
        captured.update(kwargs)
        captured["base_url"] = base_url

    monkeypatch.setattr(claude_native, "_run_with_remote_server", _fake_remote)
    return captured


def _capture_local(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Record what ``run_claude_native`` hands the local launch path."""
    captured: dict[str, Any] = {}

    def _fake_local(spec_path: Path, **kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(claude_native, "_run_with_local_server", _fake_local)
    return captured


def test_prompt_rides_as_claudes_trailing_positional_argument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Claude Code takes the initial prompt positionally, after the flags."""
    captured = _capture_remote(monkeypatch)

    claude_native.run_claude_native(
        server="https://example.com/",
        session_id=None,
        extra_args=("--dangerously-skip-permissions",),
        prompt="review the last commit",
    )

    assert captured["claude_args"] == (
        "--dangerously-skip-permissions",
        "review the last commit",
    )


def test_multiline_prompt_stays_one_argv_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Newlines and indentation survive because the prompt is never re-split.

    Delivering the prompt through argv (not a tmux paste) is what keeps a
    multi-line prompt from being interpreted line-by-line by the TUI.
    """
    captured = _capture_remote(monkeypatch)

    claude_native.run_claude_native(
        server="https://example.com/",
        session_id=None,
        prompt=_MULTILINE,
    )

    assert captured["claude_args"] == (_MULTILINE,)


def test_prompt_is_appended_after_resume_args_are_stripped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stray ``--resume`` is still dropped, and the prompt stays last."""
    captured = _capture_remote(monkeypatch)

    claude_native.run_claude_native(
        server="https://example.com/",
        session_id=None,
        extra_args=("--resume", "abc", "--verbose"),
        prompt="hello",
    )

    assert captured["claude_args"] == ("--verbose", "hello")


def test_local_launch_path_also_receives_the_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture_local(monkeypatch)

    claude_native.run_claude_native(server=None, session_id=None, prompt="hello")

    assert captured["claude_args"] == ("hello",)


@pytest.mark.parametrize("prompt", [None, "", "   "])
def test_no_prompt_adds_no_argument(monkeypatch: pytest.MonkeyPatch, prompt: str | None) -> None:
    """An empty prompt must not become an empty positional argv entry."""
    captured = _capture_remote(monkeypatch)

    claude_native.run_claude_native(
        server="https://example.com/",
        session_id=None,
        extra_args=("--verbose",),
        prompt=prompt,
    )

    assert captured["claude_args"] == ("--verbose",)

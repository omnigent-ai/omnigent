"""Native prompts use one-shot delivery, separate from reusable remote args."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omnigent.harnesses.claude_native import main as claude_native

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


@pytest.mark.parametrize("session_id", [None, "conv_existing"])
def test_remote_prompt_is_separate_from_persistent_launch_args(
    monkeypatch: pytest.MonkeyPatch,
    session_id: str | None,
) -> None:
    """Fresh and resumed launches submit an explicit prompt separately."""
    captured = _capture_remote(monkeypatch)

    claude_native.run_claude_native(
        server="https://example.com/",
        session_id=session_id,
        extra_args=("--dangerously-skip-permissions",),
        prompt="review the last commit",
    )

    assert captured["claude_args"] == ("--dangerously-skip-permissions",)
    assert captured["prompt"] == "review the last commit"


def test_prompt_precedes_variadic_value_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """A trailing variadic value flag must not be able to swallow the prompt.

    Claude Code's ``--mcp-config <configs...>`` consumes every following
    non-flag token as another config path; a prompt forwarded after it is
    rejected as a missing config file. Leading with the prompt makes that
    impossible regardless of which value flags the user passes through.
    """
    captured = _capture_local(monkeypatch)

    claude_native.run_claude_native(
        server=None,
        session_id=None,
        extra_args=("--model", "haiku", "--mcp-config", "mcp.json"),
        prompt="hello",
    )

    assert captured["claude_args"] == ("hello", "--model", "haiku", "--mcp-config", "mcp.json")


def test_remote_multiline_prompt_stays_intact(monkeypatch: pytest.MonkeyPatch) -> None:
    """Newlines and indentation survive message delivery without re-splitting."""
    captured = _capture_remote(monkeypatch)

    claude_native.run_claude_native(
        server="https://example.com/",
        session_id=None,
        prompt=_MULTILINE,
    )

    assert captured["claude_args"] == ()
    assert captured["prompt"] == _MULTILINE


def test_prompt_is_added_after_resume_args_are_stripped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stray ``--resume`` is dropped without losing the new prompt."""
    captured = _capture_remote(monkeypatch)

    claude_native.run_claude_native(
        server="https://example.com/",
        session_id=None,
        extra_args=("--resume", "abc", "--verbose"),
        prompt="hello",
    )

    assert captured["claude_args"] == ("--verbose",)
    assert captured["prompt"] == "hello"


@pytest.mark.parametrize("prompt", ["hello", _MULTILINE])
def test_local_launch_path_receives_one_positional_prompt(
    monkeypatch: pytest.MonkeyPatch,
    prompt: str,
) -> None:
    captured = _capture_local(monkeypatch)

    claude_native.run_claude_native(server=None, session_id=None, prompt=prompt)

    assert captured["claude_args"] == (prompt,)


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
    assert captured["prompt"] is None

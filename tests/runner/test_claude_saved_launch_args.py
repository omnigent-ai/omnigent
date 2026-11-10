"""Which stored Claude launch args a launch replays after a startup exit."""

from __future__ import annotations

import pytest

from omnigent.runner.resource_registry import SessionResourceRegistry

_SESSION = "5cdbea97a2fb0c659bc09605401e2bb2"
_MISSING_CONFIG = ["--mcp-config", "/Users/someone/.openui/mcp-inject.json"]


def _recovered(registry: SessionResourceRegistry, stored_args: list[str]) -> None:
    """Replay ``stored_args`` once, then record the startup exit that rules them out."""
    assert registry.claude_launch_args(_SESSION, stored_args) == (stored_args, False)
    assert registry.skip_claude_saved_args_after_startup_exit(_SESSION) is True


def test_stored_args_are_replayed_until_a_startup_exit() -> None:
    """Stored args replay normally; a startup exit without stored args is not retried."""
    registry = SessionResourceRegistry()
    assert registry.skip_claude_saved_args_after_startup_exit(_SESSION) is False
    assert registry.claude_launch_args(_SESSION, _MISSING_CONFIG) == (_MISSING_CONFIG, False)
    assert registry.claude_launch_args(_SESSION, []) == ([], False)
    assert registry.skip_claude_saved_args_after_startup_exit(_SESSION) is False


def test_permission_mode_alone_is_never_retried() -> None:
    """Claude starts with any valid mode, so a mode-only launch keeps its mode."""
    registry = SessionResourceRegistry()
    stored = ["--permission-mode", "plan"]
    assert registry.claude_launch_args(_SESSION, stored) == (stored, False)
    assert registry.skip_claude_saved_args_after_startup_exit(_SESSION) is False


def test_recovery_drops_a_restriction_and_the_bypass_beside_it() -> None:
    """A dropped ``--disallowedTools`` never leaves a mode that approves calls itself."""
    registry = SessionResourceRegistry()
    stored = ["--disallowedTools", "Bash", *_MISSING_CONFIG, "--dangerously-skip-permissions"]
    _recovered(registry, stored)
    # ``default`` is explicit: an absent selector would let a settings default apply.
    assert registry.claude_launch_args(_SESSION, stored) == (
        ["--permission-mode", "default"],
        True,
    )
    # The relaunch's own startup exit is a real failure, not another retry.
    assert registry.skip_claude_saved_args_after_startup_exit(_SESSION) is False


@pytest.mark.parametrize(
    ("mode", "kept"),
    [
        ("default", True),
        ("plan", True),
        ("dontAsk", True),
        ("acceptEdits", False),
        ("auto", False),
        ("bypassPermissions", False),
        ("someFutureMode", False),
    ],
)
def test_recovery_keeps_only_a_mode_that_always_asks(mode: str, kept: bool) -> None:
    """The session's current mode survives recovery only if it never approves calls itself."""
    registry = SessionResourceRegistry()
    stored = [*_MISSING_CONFIG, "--permission-mode", mode]
    _recovered(registry, stored)
    expected = ["--permission-mode", mode if kept else "default"]
    assert registry.claude_launch_args(_SESSION, stored) == (expected, True)
    assert registry.claude_launch_args(
        _SESSION, [*_MISSING_CONFIG, f"--permission-mode={mode}"]
    ) == (
        expected,
        True,
    )


def test_recovery_without_a_stored_mode_selects_default() -> None:
    """No stored selector still yields an explicit ``default``, not Claude's settings default."""
    registry = SessionResourceRegistry()
    _recovered(registry, _MISSING_CONFIG)
    assert registry.claude_launch_args(_SESSION, _MISSING_CONFIG) == (
        ["--permission-mode", "default"],
        True,
    )


def test_mode_picked_after_recovery_is_the_current_mode() -> None:
    """Plan picked again after recovery reset the pane to default is kept on recreate."""
    registry = SessionResourceRegistry()
    _recovered(registry, [*_MISSING_CONFIG, "--permission-mode", "plan"])
    for current in ("default", "acceptEdits", "plan"):
        launch_args, skipped = registry.claude_launch_args(
            _SESSION, [*_MISSING_CONFIG, "--permission-mode", current]
        )
        assert skipped is True
    assert launch_args == ["--permission-mode", "plan"]


def test_changed_stored_args_are_tried_again() -> None:
    """A different set of stored args is new, so it replays and the skip is cleared."""
    registry = SessionResourceRegistry()
    _recovered(registry, _MISSING_CONFIG)
    changed = ["--mcp-config", "/elsewhere.json"]
    assert registry.claude_launch_args(_SESSION, changed) == (changed, False)
    assert registry.claude_launch_args(_SESSION, _MISSING_CONFIG) == (_MISSING_CONFIG, False)


def test_notice_is_owed_once_per_recovery() -> None:
    """The chat is told once per startup exit that ruled stored args out."""
    registry = SessionResourceRegistry()
    assert registry.take_claude_saved_args_notice(_SESSION) is False
    _recovered(registry, _MISSING_CONFIG)
    assert registry.take_claude_saved_args_notice(_SESSION) is True
    assert registry.take_claude_saved_args_notice(_SESSION) is False


@pytest.mark.asyncio
async def test_cleanup_forgets_the_recovery() -> None:
    """A cleaned-up session replays its stored args again."""
    registry = SessionResourceRegistry()
    _recovered(registry, _MISSING_CONFIG)
    await registry.cleanup_session(_SESSION)
    assert registry.claude_launch_args(_SESSION, _MISSING_CONFIG) == (_MISSING_CONFIG, False)

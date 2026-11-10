"""Tests for the shared Claude launch-arg helpers."""

from __future__ import annotations

import pytest

from omnigent.harnesses.claude_native.launch_args import (
    claude_permission_mode,
    split_claude_permission_launch_args,
)


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (
            ["--mcp-config", "/a.json", "--permission-mode", "plan", "--model", "opus"],
            (["--mcp-config", "/a.json", "--model", "opus"], ["--permission-mode", "plan"]),
        ),
        (["--permission-mode=auto", "-p"], (["-p"], ["--permission-mode=auto"])),
        (
            ["--dangerously-skip-permissions", "--plugin-dir", "/p"],
            (["--plugin-dir", "/p"], ["--dangerously-skip-permissions"]),
        ),
        # A selector with no value does not swallow the next flag.
        (["--permission-mode", "--verbose"], (["--verbose"], ["--permission-mode"])),
        # Positional text after ``--`` is never a selector.
        (
            ["--mcp-config", "/a.json", "--", "--dangerously-skip-permissions"],
            (["--mcp-config", "/a.json", "--", "--dangerously-skip-permissions"], []),
        ),
        ([], ([], [])),
    ],
)
def test_split_claude_permission_launch_args(
    args: list[str], expected: tuple[list[str], list[str]]
) -> None:
    """Permission-mode selectors split from the other stored args, order kept."""
    assert split_claude_permission_launch_args(args) == expected


@pytest.mark.parametrize(
    ("permission_args", "mode"),
    [
        (["--permission-mode", "plan"], "plan"),
        (["--permission-mode=auto"], "auto"),
        (["--dangerously-skip-permissions"], "bypassPermissions"),
        (["--permission-mode", "plan", "--dangerously-skip-permissions"], "bypassPermissions"),
        (["--permission-mode"], None),
        ([], None),
    ],
)
def test_claude_permission_mode(permission_args: list[str], mode: str | None) -> None:
    """The last selector decides the mode Claude launches into."""
    assert claude_permission_mode(permission_args) == mode

"""Validation for native session directory suggestions, without filesystem access."""

from typing import Any

import pytest

from omnigent.server.routes._sessions.helpers import _claude_native_permission_directories


def test_permission_directories_preserve_exact_paths_and_ignore_mode_suggestions() -> None:
    directories = ["/home/test/worktree", "/mnt/project with spaces", r"C:\worktree"]
    suggestions = [
        {"type": "setMode", "mode": "acceptEdits", "destination": "session"},
        {"type": "addDirectories", "directories": directories[:2], "destination": "session"},
        {"type": "addDirectories", "directories": directories[2:], "destination": "session"},
    ]
    assert _claude_native_permission_directories(suggestions) == directories


@pytest.mark.parametrize("suggestions", [None, {}, [], [None], [{"type": "addRules"}]])
def test_permission_directories_absent(suggestions: Any) -> None:
    assert _claude_native_permission_directories(suggestions) is None


@pytest.mark.parametrize("directories", [None, "/project", [], [""], ["  "], [42], ["/bad\0path"]])
def test_permission_directories_reject_entire_malformed_grant(directories: Any) -> None:
    suggestions = [
        {"type": "addDirectories", "directories": ["/valid"], "destination": "session"},
        {"type": "addDirectories", "directories": directories, "destination": "session"},
    ]
    assert _claude_native_permission_directories(suggestions) is None


@pytest.mark.parametrize("destination", [None, "userSettings", "projectSettings", "unknown"])
def test_permission_directories_reject_non_session_destination(destination: Any) -> None:
    suggestions = [
        {"type": "addDirectories", "directories": ["/project"], "destination": destination}
    ]
    assert _claude_native_permission_directories(suggestions) is None

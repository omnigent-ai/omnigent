"""Tests for reading worktree lifecycle commands out of a project's config.

``projects.config`` is an opaque, client-written JSON object, so the parsing in
``omnigent.server.worktree_hooks`` is the boundary that decides whether a hook
runs at all. These cover the "unset" spellings and the timeout clamp.
"""

from __future__ import annotations

from omnigent.host.git_worktree import DEFAULT_HOOK_TIMEOUT_S, MAX_HOOK_TIMEOUT_S
from omnigent.server.worktree_hooks import (
    NO_HOOKS,
    hook_config_from_project_config,
)


def test_no_keys_configures_nothing() -> None:
    """A project with no hook keys resolves to the shared "nothing" config."""
    assert hook_config_from_project_config(None) is NO_HOOKS
    assert hook_config_from_project_config({}) is NO_HOOKS
    assert hook_config_from_project_config({"host_id": "host_a"}) is NO_HOOKS


def test_blank_command_is_unset() -> None:
    """An empty / whitespace-only string turns the hook OFF, not on with a
    blank shell command — that's how clearing the settings field reads."""
    config = hook_config_from_project_config(
        {
            "worktree_post_create_command": "",
            "worktree_pre_delete_command": "   \n ",
        }
    )
    assert config is NO_HOOKS


def test_commands_are_trimmed_and_kept_independent() -> None:
    """Either command may be set on its own."""
    config = hook_config_from_project_config(
        {"worktree_post_create_command": "  bun install  "},
    )
    assert config.post_create_command == "bun install"
    assert config.pre_delete_command is None
    assert config.any_configured is True


def test_timeout_defaults_and_clamps() -> None:
    """An absent, bogus, or out-of-range timeout resolves to a usable one."""
    base = {"worktree_pre_delete_command": "true"}
    assert hook_config_from_project_config(base).timeout_seconds == DEFAULT_HOOK_TIMEOUT_S
    assert (
        hook_config_from_project_config(
            {**base, "worktree_hook_timeout_seconds": "600"}
        ).timeout_seconds
        == DEFAULT_HOOK_TIMEOUT_S
    )
    assert (
        hook_config_from_project_config(
            {**base, "worktree_hook_timeout_seconds": 999_999}
        ).timeout_seconds
        == MAX_HOOK_TIMEOUT_S
    )
    assert (
        hook_config_from_project_config(
            {**base, "worktree_hook_timeout_seconds": 600}
        ).timeout_seconds
        == 600.0
    )


def test_multi_line_script_keeps_its_internal_newlines() -> None:
    """Only the OUTER whitespace is trimmed — the script body is a program.

    Collapsing or stripping internal newlines would silently rewrite the
    user's script into something else.
    """
    script = "#!/bin/bash\nset -euo pipefail\n\nbun install\n  cp ../../.env .\n"
    config = hook_config_from_project_config({"worktree_post_create_command": f"\n  {script}  \n"})
    assert config.post_create_command == script.strip()
    assert config.post_create_command is not None
    assert config.post_create_command.startswith("#!/bin/bash")
    assert "  cp ../../.env ." in config.post_create_command
    assert config.post_create_command.count("\n") == 4


def test_crlf_script_is_normalized_to_unix_newlines() -> None:
    """A script pasted from a CRLF editor must not reach a POSIX shell raw.

    Stray carriage returns become part of each command name there
    (``bun install\r``: command not found), which is a baffling failure.
    """
    config = hook_config_from_project_config(
        {"worktree_pre_delete_command": "echo one\r\necho two\r\n"}
    )
    assert config.pre_delete_command == "echo one\necho two"


def test_non_string_command_is_ignored() -> None:
    """A hand-edited config holding the wrong type reads as unset, not a crash."""
    config = hook_config_from_project_config({"worktree_post_create_command": 42})
    assert config is NO_HOOKS

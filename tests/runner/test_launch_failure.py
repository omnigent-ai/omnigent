"""Tests for the harness launch-failure classifier."""

from __future__ import annotations

import pytest

from omnigent.runner.launch_failure import (
    FailureDiagnosis,
    classify_terminal_failure,
    describe_failure_code,
)

# The tail Claude Code prints when refusing --dangerously-skip-permissions as
# root — the exact scenario a root container hits.
_ROOT_REFUSAL_OUTPUT = (
    "--dangerously-skip-permissions cannot be run with root privileges for security reasons"
)


def test_classifies_root_permission_failure() -> None:
    diagnosis = classify_terminal_failure(
        command="claude",
        exit_status=1,
        output=_ROOT_REFUSAL_OUTPUT,
    )
    assert diagnosis is not None
    assert diagnosis.title == "Claude Code can't run as root"
    assert "root" in diagnosis.cause.lower()
    assert diagnosis.remediation is not None
    assert "non-root" in diagnosis.remediation.lower()


def test_root_failure_survives_mid_word_truncation() -> None:
    # The pane snapshot may be clipped to "...for secuRITY REASONS" — the
    # matcher keys on "security reasons", which line-boundary trimming keeps.
    diagnosis = classify_terminal_failure(
        command="claude",
        exit_status=1,
        output="root privileges\nfor security reasons",
    )
    assert diagnosis is not None
    assert diagnosis.title == "Claude Code can't run as root"


@pytest.mark.parametrize(
    "output",
    [
        "Not logged in · Please run /login",
        "Error: Invalid API key",
        "authentication_error: 401 Unauthorized",
    ],
)
def test_classifies_auth_failure(output: str) -> None:
    diagnosis = classify_terminal_failure(command="codex", exit_status=1, output=output)
    assert diagnosis is not None
    assert diagnosis.title == "Agent isn't signed in"
    assert diagnosis.remediation is not None


def test_classifies_missing_binary_by_exit_code() -> None:
    diagnosis = classify_terminal_failure(command="qwen", exit_status=127, output="")
    assert diagnosis is not None
    assert diagnosis.title == "Agent command not found"


def test_classifies_missing_binary_by_output() -> None:
    diagnosis = classify_terminal_failure(
        command="qwen",
        exit_status=None,
        output="bash: qwen: command not found",
    )
    assert diagnosis is not None
    assert diagnosis.title == "Agent command not found"


# Pane output proving the CLI was present and running: it self-updated and
# printed a resume hint, then a stray flag hit the shell as command-not-found
# (exit 127). This is a mid-session crash of a present CLI, not a missing
# install.
_PRESENT_CLI_CRASH_OUTPUT = "\n".join(
    (
        "● Unknown command: /restart",
        "  Press Ctrl-C again to exit    ✔ Update installed · Restart to update",
        "Resume this session with:",
        "claude --resume be28caff-2e85-47de-8f17-9346d116106b",
        "zsh:2: command not found: --model",
    )
)


def test_present_cli_crash_with_stray_flag_is_not_missing_binary() -> None:
    # The not-found line blames `--model`, not the claude command itself, and
    # the rest of the pane proves the CLI ran. Must not say "install the
    # harness" — unclassified is the honest answer.
    diagnosis = classify_terminal_failure(
        command="claude",
        exit_status=127,
        output=_PRESENT_CLI_CRASH_OUTPUT,
    )
    assert diagnosis is None


def test_exit_127_with_output_not_blaming_command_is_not_missing_binary() -> None:
    # A present CLI that produced output and then exited 127 for its own
    # reasons is not a missing install.
    diagnosis = classify_terminal_failure(
        command="claude",
        exit_status=127,
        output="Welcome to Claude Code!\nSession crashed unexpectedly",
    )
    assert diagnosis is None


def test_missing_binary_named_without_command_word_still_matches() -> None:
    # dash prints ``claude: not found`` with no "command"; the line names the
    # launched command, so it is still a missing install.
    diagnosis = classify_terminal_failure(
        command="claude",
        exit_status=127,
        output="sh: 1: claude: not found",
    )
    assert diagnosis is not None
    assert diagnosis.title == "Agent command not found"


def test_missing_binary_matches_full_path_not_found_line() -> None:
    diagnosis = classify_terminal_failure(
        command="/usr/local/bin/claude",
        exit_status=127,
        output="bash: /usr/local/bin/claude: No such file or directory",
    )
    assert diagnosis is not None
    assert diagnosis.title == "Agent command not found"


def test_command_name_matches_on_word_boundary_only() -> None:
    # ``sh`` must not match inside ``zsh:``; the not-found line is about a
    # stray flag, not the launched shell.
    diagnosis = classify_terminal_failure(
        command="sh",
        exit_status=127,
        output="zsh:2: command not found: --model",
    )
    assert diagnosis is None


def test_unknown_command_keeps_strict_marker_match() -> None:
    # With no command to cross-check, a shell not-found marker still counts.
    diagnosis = classify_terminal_failure(
        command=None,
        exit_status=None,
        output="bash: qwen: command not found",
    )
    assert diagnosis is not None
    assert diagnosis.title == "Agent command not found"


def test_root_wins_over_generic_auth_when_both_markers_present() -> None:
    # Ordering guard: the root case also reads like a permission problem, so it
    # must be matched before any broader rule.
    diagnosis = classify_terminal_failure(
        command="claude",
        exit_status=1,
        output="not logged in\nroot privileges\nfor security reasons",
    )
    assert diagnosis is not None
    assert diagnosis.title == "Claude Code can't run as root"


def test_unclassified_failure_returns_none() -> None:
    assert (
        classify_terminal_failure(
            command="worker-cli",
            exit_status=1,
            output="startup failed\ncomplete setup first",
        )
        is None
    )


def test_none_inputs_do_not_raise() -> None:
    assert classify_terminal_failure(command=None, exit_status=None, output=None) is None


def test_command_path_is_matched_by_basename() -> None:
    # A full path shouldn't defeat the (currently command-agnostic) matchers.
    diagnosis = classify_terminal_failure(
        command="/usr/local/bin/claude",
        exit_status=1,
        output=_ROOT_REFUSAL_OUTPUT,
    )
    assert isinstance(diagnosis, FailureDiagnosis)


@pytest.mark.parametrize(
    ("code", "expected_substring"),
    [
        ("required_terminal_exited", "terminal exited"),
        ("terminal_launch_failed", "couldn't be started"),
        ("runner_error", "setting up the turn"),
        ("runner_disconnected", "host dropped"),
        ("connection_error", "connection"),
        ("context_length_exceeded", "context window"),
    ],
)
def test_describe_failure_code_known(code: str, expected_substring: str) -> None:
    description = describe_failure_code(code)
    assert description is not None
    assert expected_substring in description


@pytest.mark.parametrize("code", [None, "", "some_unknown_code"])
def test_describe_failure_code_unknown(code: str | None) -> None:
    assert describe_failure_code(code) is None

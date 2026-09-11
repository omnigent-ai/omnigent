"""Tests for the harness launch-failure classifier."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from omnigent.runner.launch_failure import (
    _FAILURE_CODE_DESCRIPTIONS,
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
        ("runner_failed_to_start", "failed to start"),
        ("connection_error", "connection"),
        ("context_length_exceeded", "context window"),
        ("workspace_missing", "workspace"),
    ],
)
def test_describe_failure_code_known(code: str, expected_substring: str) -> None:
    description = describe_failure_code(code)
    assert description is not None
    assert expected_substring in description


@pytest.mark.parametrize("code", [None, "", "some_unknown_code"])
def test_describe_failure_code_unknown(code: str | None) -> None:
    assert describe_failure_code(code) is None


def test_failure_code_descriptions_match_frontend_mirror() -> None:
    # The failure card renders client-side from a hand-mirrored copy of this
    # map; a code present in only one copy silently degrades to the generic
    # "Something went wrong" headline on the other side.
    tsx_path = Path(__file__).resolve().parents[2] / "web/src/components/blocks/StatusBlocks.tsx"
    match = re.search(
        r"const FAILURE_CODE_DESCRIPTIONS: Record<string, string> = \{(.*?)\n\};",
        tsx_path.read_text(encoding="utf-8"),
        re.DOTALL,
    )
    assert match is not None, f"FAILURE_CODE_DESCRIPTIONS not found in {tsx_path}"
    frontend_codes = set(re.findall(r"^\s*(\w+):", match.group(1), re.MULTILINE))
    assert frontend_codes == set(_FAILURE_CODE_DESCRIPTIONS)

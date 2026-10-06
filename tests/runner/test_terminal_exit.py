"""Tests for classifying a required native terminal's exit as the person's own quit."""

from __future__ import annotations

import pytest

from omnigent.runner.resource_registry import TerminalExitEvent, TerminalLifecycle
from omnigent.runner.terminal_exit import CLAUDE_EXIT_BANNER, classify_terminal_exit

_READY = {"terminal_input_ready_at": "1759766400.5"}
# A tmux capture pads the screen with blank rows; this tail has no exit banner.
_PADDED_TAIL = "\n" * 120 + "Pane is dead (status 0, Mon Oct  5 16:37:57 2026)"


def _event(
    terminal_name: str = "claude",
    *,
    exit_status: int | None = None,
    last_output: str | None = None,
    session_end_reason: str | None = None,
    session_end_signal: str | None = None,
    session_end_evidence: str | None = None,
    exit_signal: str | None = None,
    interactive: bool = True,
    command: str | None = None,
) -> TerminalExitEvent:
    context: dict[str, str] = dict(_READY) if interactive else {}
    for key, value in (
        ("claude_session_end_reason", session_end_reason),
        ("claude_session_end_signal", session_end_signal),
        ("claude_session_end_evidence", session_end_evidence),
        ("terminal_exit_signal", exit_signal),
    ):
        if value is not None:
            context[key] = value
    return TerminalExitEvent(
        session_id="conv_exit",
        terminal_id=f"terminal_{terminal_name}_main",
        terminal_name=terminal_name,
        session_key="main",
        lifecycle=TerminalLifecycle.REQUIRED,
        command=command,
        exit_status=exit_status,
        last_output=last_output,
        lifecycle_context=context,
    )


@pytest.mark.parametrize(
    ("event", "rule"),
    [
        pytest.param(
            _event(exit_status=0, last_output=f"bye\n\n{CLAUDE_EXIT_BANNER}\nclaude --resume x"),
            "claude_exit_banner",
            id="claude-banner",
        ),
        pytest.param(
            _event(
                exit_status=0,
                last_output=f"{CLAUDE_EXIT_BANNER}\nclaude --resume x",
                interactive=False,
            ),
            "claude_exit_banner",
            id="claude-banner-needs-no-readiness-signal",
        ),
        pytest.param(
            _event(last_output=_PADDED_TAIL, session_end_reason="prompt_input_exit"),
            "session_end_reason",
            id="claude-prompt-input-exit-padded-tail-no-status",
        ),
        pytest.param(
            _event(
                exit_status=0, last_output=_PADDED_TAIL, session_end_reason="prompt_input_exit"
            ),
            "session_end_reason",
            id="claude-prompt-input-exit-with-status-0",
        ),
        pytest.param(
            _event(session_end_reason="logout"), "session_end_reason", id="claude-logout"
        ),
        pytest.param(_event(session_end_reason="clear"), "session_end_reason", id="claude-clear"),
        pytest.param(_event(exit_status=0), "exit_zero", id="claude-exit-0"),
        pytest.param(_event(exit_status=0, session_end_reason="other"), "exit_zero", id="other"),
        pytest.param(_event("pi", exit_status=0), "exit_zero", id="pi-exit-0"),
        pytest.param(_event(exit_signal="SIGINT"), "user_signal", id="ctrl-c"),
        pytest.param(_event("pi", exit_signal="SIGHUP"), "user_signal", id="closed-terminal"),
        pytest.param(_event(exit_signal="sigint"), "user_signal", id="signal-case-insensitive"),
        pytest.param(
            _event(session_end_reason="signal", session_end_signal="SIGINT"),
            "user_signal",
            id="hook-reports-sigint",
        ),
        pytest.param(
            _event(session_end_reason="signal", session_end_signal="SIGHUP"),
            "user_signal",
            id="hook-reports-sighup",
        ),
    ],
)
def test_person_quitting_is_voluntary(event: TerminalExitEvent, rule: str) -> None:
    decision = classify_terminal_exit(event)

    assert decision.voluntary is True
    assert decision.rule == rule


@pytest.mark.parametrize(
    ("event", "rule"),
    [
        pytest.param(_event("pi", exit_status=1), "nonzero_exit", id="pi-exit-1"),
        pytest.param(_event(exit_status=127), "nonzero_exit", id="exit-127"),
        pytest.param(_event(exit_status=130), "nonzero_exit", id="exit-130-is-not-a-signal"),
        pytest.param(_event(exit_status=129), "nonzero_exit", id="claude-sighup-exits-129"),
        pytest.param(_event(exit_status=143), "nonzero_exit", id="claude-sigterm-exits-143"),
        pytest.param(
            _event(exit_status=1, session_end_reason="prompt_input_exit"),
            "nonzero_exit",
            id="quit-reason-does-not-excuse-a-failing-status",
        ),
        pytest.param(_event(exit_signal="SIGKILL"), "fatal_signal", id="sigkill"),
        pytest.param(_event(exit_signal="SIGSEGV"), "fatal_signal", id="sigsegv"),
        pytest.param(_event(exit_signal="SIGTERM"), "fatal_signal", id="sigterm"),
        pytest.param(_event("pi", exit_signal="SIGABRT"), "fatal_signal", id="sigabrt"),
        pytest.param(
            _event(exit_signal="SIGKILL", session_end_reason="prompt_input_exit"),
            "fatal_signal",
            id="quit-reason-does-not-excuse-sigkill",
        ),
        pytest.param(
            _event(exit_status=0, session_end_signal="SIGTERM"),
            "external_signal",
            id="hook-sigterm-with-exit-0",
        ),
        pytest.param(
            _event(exit_status=0, session_end_signal="SIGQUIT"),
            "external_signal",
            id="hook-sigquit-with-exit-0",
        ),
        pytest.param(
            _event(session_end_reason="signal", session_end_signal="SIGTERM"),
            "external_signal",
            id="hook-sigterm-without-status",
        ),
        pytest.param(
            _event(session_end_reason="prompt_input_exit", session_end_signal="SIGTERM"),
            "external_signal",
            id="quit-reason-does-not-excuse-a-hook-sigterm",
        ),
        pytest.param(
            _event(session_end_reason="session_close"),
            "no_quit_evidence",
            id="session-close-without-status",
        ),
        pytest.param(
            _event(session_end_reason="signal"), "no_quit_evidence", id="signal-without-status"
        ),
        pytest.param(
            _event(session_end_reason="other"), "no_quit_evidence", id="other-without-status"
        ),
        pytest.param(
            _event(exit_status=0, interactive=False, last_output=_PADDED_TAIL),
            "not_interactive",
            id="claude-exit-0-before-interactive",
        ),
        pytest.param(
            _event("pi", exit_status=0, interactive=False), "not_interactive", id="pi-launch-exit"
        ),
        pytest.param(
            _event(session_end_reason="prompt_input_exit", interactive=False),
            "not_interactive",
            id="quit-reason-before-interactive",
        ),
        pytest.param(
            _event(exit_signal="SIGINT", interactive=False),
            "not_interactive",
            id="ctrl-c-before-interactive",
        ),
        pytest.param(
            _event(exit_status=0, session_end_reason="bypass_permissions_disabled"),
            "policy_exit",
            id="policy-ended-the-session",
        ),
        pytest.param(_event(), "no_quit_evidence", id="pane-vanished-without-evidence"),
        pytest.param(
            _event("codex", exit_status=0), "terminal_not_covered", id="codex-tui-is-auxiliary"
        ),
        pytest.param(
            _event("cursor", exit_status=0), "terminal_not_covered", id="other-tui-exit-0"
        ),
        pytest.param(
            _event("worker", exit_signal="SIGINT"), "terminal_not_covered", id="generic-terminal"
        ),
        pytest.param(
            _event("other", exit_status=0, last_output=f"{CLAUDE_EXIT_BANNER}\nother --resume x"),
            "terminal_not_covered",
            id="banner-only-counts-for-claude",
        ),
    ],
)
def test_everything_else_stays_a_failure(event: TerminalExitEvent, rule: str) -> None:
    decision = classify_terminal_exit(event)

    assert decision.voluntary is False
    assert decision.rule == rule


def test_banner_needs_a_clean_exit_status() -> None:
    event = _event(exit_status=1, last_output=f"{CLAUDE_EXIT_BANNER}\nclaude --resume x")

    assert classify_terminal_exit(event).voluntary is False


@pytest.mark.parametrize(
    "event",
    [
        _event(exit_status=0),
        _event(session_end_reason="prompt_input_exit"),
        _event(exit_signal="SIGINT"),
    ],
    ids=["exit-0", "quit-reason", "sigint"],
)
def test_runner_shutdown_takes_the_terminal_down_without_a_quit(event: TerminalExitEvent) -> None:
    decision = classify_terminal_exit(event, runner_shutting_down=True)

    assert decision.voluntary is False
    assert decision.rule == "runner_shutting_down"


def test_banner_quit_stays_voluntary_while_the_runner_shuts_down() -> None:
    event = _event(exit_status=0, last_output=f"{CLAUDE_EXIT_BANNER}\nclaude --resume x")

    assert classify_terminal_exit(event, runner_shutting_down=True).voluntary is True


@pytest.mark.parametrize(
    ("event", "strong"),
    [
        pytest.param(
            _event(exit_status=0, last_output=f"{CLAUDE_EXIT_BANNER}\nx"), True, id="banner"
        ),
        pytest.param(_event(session_end_reason="prompt_input_exit"), True, id="quit-reason"),
        pytest.param(_event(session_end_reason="logout", exit_status=0), True, id="reason-and-0"),
        pytest.param(_event(exit_signal="SIGINT"), True, id="ctrl-c"),
        pytest.param(_event("pi", exit_signal="SIGHUP"), True, id="pi-hangup"),
        pytest.param(_event(exit_status=0), False, id="bare-exit-0"),
        pytest.param(
            _event(exit_status=0, session_end_reason="other", session_end_evidence="claude_hook"),
            False,
            id="exit-0-with-an-unhelpful-hook",
        ),
        pytest.param(_event("pi", exit_status=0), False, id="pi-bare-exit-0"),
        pytest.param(_event(exit_signal="SIGKILL"), False, id="failed-decision"),
    ],
)
def test_only_the_persons_own_action_is_strong_evidence(
    event: TerminalExitEvent, strong: bool
) -> None:
    """A bare zero exit is weak: a launcher wrapper can hide a crash behind exit 0."""
    assert classify_terminal_exit(event).strong_evidence is strong


def test_decision_carries_the_logged_evidence_without_pane_text() -> None:
    event = _event(
        exit_status=0,
        last_output="private pane text\n" + CLAUDE_EXIT_BANNER,
        session_end_reason="prompt_input_exit",
        session_end_signal="SIGHUP",
        session_end_evidence="claude_hook",
        exit_signal="SIGINT",
        command="/home/alice/bin/env",
    )

    attributes = classify_terminal_exit(event).log_attributes()

    assert attributes == {
        "harness": "claude-native",
        # Basename only: the wrapper is the clue, its path is not.
        "command": "env",
        "exit_status": 0,
        "signal": "SIGINT",
        "session_end_reason": "prompt_input_exit",
        "session_end_signal": "SIGHUP",
        "session_end_evidence": "claude_hook",
        "banner_seen": True,
        "interactive": True,
        "decision": "voluntary",
        "rule": "claude_exit_banner",
    }
    assert "private pane text" not in str(attributes)
    assert "alice" not in str(attributes)


def test_failed_decision_is_labelled_failed() -> None:
    attributes = classify_terminal_exit(_event("pi", exit_status=1)).log_attributes()

    assert attributes["decision"] == "failed"
    assert attributes["harness"] == "pi-native"
    assert attributes["interactive"] is True
    assert attributes["banner_seen"] is False
    assert attributes["command"] is None

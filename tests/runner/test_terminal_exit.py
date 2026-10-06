"""Tests for classifying a required native terminal's exit as the person's own quit."""

from __future__ import annotations

import logging

import httpx
import pytest

from omnigent.entities.conversation import ErrorData
from omnigent.runner.resource_registry import TerminalExitEvent, TerminalLifecycle
from omnigent.runner.terminal_exit import (
    CLAUDE_EXIT_BANNER,
    TERMINAL_EXIT_NOTICE_CODE,
    classify_terminal_exit,
    post_terminal_exit_notice,
    terminal_display_name,
)

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
    exit_signal: str | None = None,
    interactive: bool = True,
) -> TerminalExitEvent:
    context: dict[str, str] = dict(_READY) if interactive else {}
    if session_end_reason is not None:
        context["claude_session_end_reason"] = session_end_reason
    if session_end_signal is not None:
        context["claude_session_end_signal"] = session_end_signal
    if exit_signal is not None:
        context["terminal_exit_signal"] = exit_signal
    return TerminalExitEvent(
        session_id="conv_exit",
        terminal_id=f"terminal_{terminal_name}_main",
        terminal_name=terminal_name,
        session_key="main",
        lifecycle=TerminalLifecycle.REQUIRED,
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
        pytest.param(_event("codex", exit_status=0), "exit_zero", id="codex-exit-0"),
        pytest.param(_event(exit_signal="SIGINT"), "user_signal", id="ctrl-c"),
        pytest.param(_event("pi", exit_signal="SIGHUP"), "user_signal", id="closed-terminal"),
        pytest.param(_event(exit_signal="sigint"), "user_signal", id="signal-case-insensitive"),
        pytest.param(
            _event(session_end_reason="signal", session_end_signal="SIGINT"),
            "user_signal",
            id="hook-reports-sigint",
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
            _event(session_end_reason="unknown", session_end_signal="SIGTERM"),
            "no_quit_evidence",
            id="sigterm-reported-by-hook",
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


def test_decision_carries_the_logged_evidence_without_pane_text() -> None:
    event = _event(
        exit_status=0,
        last_output="private pane text\n" + CLAUDE_EXIT_BANNER,
        session_end_reason="prompt_input_exit",
        session_end_signal="SIGHUP",
        exit_signal="SIGINT",
    )

    attributes = classify_terminal_exit(event).log_attributes()

    assert attributes == {
        "harness": "claude-native",
        "exit_status": 0,
        "signal": "SIGINT",
        "session_end_reason": "prompt_input_exit",
        "session_end_signal": "SIGHUP",
        "banner_seen": True,
        "interactive": True,
        "decision": "voluntary",
        "rule": "claude_exit_banner",
    }
    assert "private pane text" not in str(attributes)


def test_failed_decision_is_labelled_failed() -> None:
    attributes = classify_terminal_exit(_event("pi", exit_status=1)).log_attributes()

    assert attributes["decision"] == "failed"
    assert attributes["harness"] == "pi-native"
    assert attributes["interactive"] is True
    assert attributes["banner_seen"] is False


@pytest.mark.parametrize(
    ("terminal_name", "expected"),
    [("claude", "Claude"), ("pi", "Pi"), ("codex", "Codex"), ("not-an-agent", "not-an-agent")],
)
def test_terminal_display_name(terminal_name: str, expected: str) -> None:
    assert terminal_display_name(terminal_name) == expected


class _RecordingClient:
    """Server client that records POSTs and can fail them."""

    def __init__(self, *, fail: Exception | None = None) -> None:
        self.posts: list[tuple[str, dict[str, object]]] = []
        self._fail = fail

    async def post(self, url: str, **kwargs: object) -> httpx.Response:
        self.posts.append((url, kwargs))
        if self._fail is not None:
            raise self._fail
        return httpx.Response(200, request=httpx.Request("POST", url))


@pytest.mark.asyncio
async def test_notice_is_a_neutral_info_item_that_says_how_to_resume() -> None:
    client = _RecordingClient()

    await post_terminal_exit_notice(client, "conv_exit", "pi")  # type: ignore[arg-type]

    [(url, kwargs)] = client.posts
    assert url == "/v1/sessions/conv_exit/events"
    assert kwargs["json"] == {
        "type": "external_conversation_item",
        "data": {
            "item_type": "error",
            "item_data": {
                "source": "harness",
                "code": TERMINAL_EXIT_NOTICE_CODE,
                "title": "Pi exited in its terminal. Send a message to resume.",
                "message": "Pi exited in its terminal. Send a message to resume.",
                "level": "info",
            },
        },
    }
    # The server stores it as an error item; the payload must stay valid for that model.
    ErrorData.model_validate(kwargs["json"]["data"]["item_data"])  # type: ignore[index]


@pytest.mark.asyncio
async def test_a_failed_notice_post_only_loses_the_notice(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = _RecordingClient(fail=httpx.ConnectError("server unreachable"))

    with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
        await post_terminal_exit_notice(client, "conv_exit", "claude")  # type: ignore[arg-type]

    assert "Failed to post the terminal-exit notice for conv_exit" in caplog.text

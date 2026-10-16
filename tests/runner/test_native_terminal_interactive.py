"""Tests for semantic native-terminal interactivity telemetry."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque

import httpx
import pytest

from omnigent.errors import ErrorCategory
from omnigent.runner.native import orchestration
from omnigent.runner.native.orchestration import (
    _observe_terminal_interactive,
    _post_claude_startup_dialog_notice,
    _StartupDialogWatch,
)


class _FakeTerminal:
    """Minimal terminal instance that returns scripted pane captures."""

    diagnostic_id = "terminal-instance-test"

    def __init__(self, screens: list[str], *, running: bool = True) -> None:
        self._screens = deque(screens)
        self._last_screen = screens[-1]
        self.running = running

    async def read(self, *, join_wrapped: bool = False) -> dict[str, object]:
        del join_wrapped
        if self._screens:
            self._last_screen = self._screens.popleft()
        return {"screen": self._last_screen}


@pytest.mark.asyncio
async def test_observer_emits_one_content_free_terminal_interactive_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The first ready frame emits the shared semantic endpoint."""
    terminal = _FakeTerminal(["starting", "composer ready"])

    with caplog.at_level(logging.INFO, logger="omnigent.runner.app"):
        await _observe_terminal_interactive(
            session_id="conv_interactive",
            terminal_id="terminal_claude_main",
            harness="claude-native",
            readiness_signal="claude_composer",
            instance=terminal,  # type: ignore[arg-type]
            is_interactive=lambda pane: pane == "composer ready",
            timeout_s=1.0,
            poll_interval_s=0.0,
        )

    record = next(
        record for record in caplog.records if record.event_name == "terminal_interactive"
    )
    assert record.session_id == "conv_interactive"
    assert record.attributes == {
        "harness": "claude-native",
        "terminal_id": "terminal_claude_main",
        "terminal_instance_id": "terminal-instance-test",
        "readiness_signal": "claude_composer",
    }
    assert "composer ready" not in record.getMessage()


# A launcher wrapper still running its own startup step, with a token on screen.
_SCREEN_TOKEN = "dapi" + "Test1234567890abcd"
_STALLED_SCREEN = f"launcher: refreshing workspace login\nusing token {_SCREEN_TOKEN}\n"


@pytest.mark.asyncio
async def test_observer_records_the_redacted_pane_of_a_stalled_terminal(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pane that never becomes ready is described at the slow mark and at the deadline."""
    monkeypatch.setenv("OMNIGENT_HARNESS_STDERR_ENABLED", "1")
    terminal = _FakeTerminal([_STALLED_SCREEN])

    with caplog.at_level(logging.INFO, logger="omnigent.runner.app"):
        await _observe_terminal_interactive(
            session_id="conv_stalled",
            terminal_id="terminal_claude_main",
            harness="claude-native",
            readiness_signal="claude_composer",
            instance=terminal,  # type: ignore[arg-type]
            is_interactive=lambda _pane: False,
            timeout_s=0.05,
            slow_after_s=0.0,
            poll_interval_s=0.0,
        )

    by_event = {
        record.event_name: record
        for record in caplog.records
        if getattr(record, "event_name", None)
        in {"terminal_interactive_slow", "terminal_interactive_unobserved"}
    }
    assert [getattr(r, "event_name", None) for r in caplog.records].count(
        "terminal_interactive_slow"
    ) == 1
    assert by_event["terminal_interactive_unobserved"].attributes["reason"] == "TimeoutError"
    for record in by_event.values():
        tail = record.attributes["terminal_last_output"]
        assert "refreshing workspace login" in tail
        assert _SCREEN_TOKEN not in tail
        # The local runner log renders only the message, so the tail rides on it too.
        assert "refreshing workspace login" in record.getMessage()
        assert _SCREEN_TOKEN not in record.getMessage()


_SIGN_IN_PROMPT = "Sign in to continue:\n  https://signin.example.com/device\n  code: HQ7M-2KPD\n"


@pytest.mark.parametrize(
    ("capture", "screen", "expected"),
    [
        ("0", _STALLED_SCREEN, {"sign_in_prompt_detected": False}),
        ("1", _SIGN_IN_PROMPT, {"sign_in_prompt_detected": True}),
        # The launcher moved past its prompt, but the code and address are still on screen.
        (
            "1",
            _SIGN_IN_PROMPT + "See https://docs.example.com/oauth/troubleshooting\n",
            {"sign_in_prompt_detected": False},
        ),
    ],
    ids=["capture-disabled", "sign-in-prompt", "sign-in-material-after-prompt"],
)
@pytest.mark.asyncio
async def test_observer_keeps_pane_text_out_unless_captured_and_not_a_sign_in(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    capture: str,
    screen: str,
    expected: dict[str, object],
) -> None:
    """Pane text needs the capture opt-in and stays out while sign-in material is on screen."""
    monkeypatch.setenv("OMNIGENT_HARNESS_STDERR_ENABLED", capture)
    terminal = _FakeTerminal([screen])

    with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
        await _observe_terminal_interactive(
            session_id="conv_pending",
            terminal_id="terminal_codex_main",
            harness="codex-native",
            readiness_signal="codex_composer",
            instance=terminal,  # type: ignore[arg-type]
            is_interactive=lambda _pane: False,
            timeout_s=0.0,
            poll_interval_s=0.0,
        )

    record = next(
        record
        for record in caplog.records
        if record.event_name == "terminal_interactive_unobserved"
    )
    pane_keys = {"terminal_last_output", "sign_in_prompt_detected"}
    pane = {k: v for k, v in record.attributes.items() if k in pane_keys and v is not None}
    assert pane == expected
    logged = record.getMessage() + "\n".join(map(str, record.attributes.values()))
    assert "HQ7M-2KPD" not in logged
    assert "signin.example.com" not in logged


@pytest.mark.asyncio
async def test_observer_cancelled_after_the_slow_mark_keeps_its_record(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A runner lost before the deadline has already described the stalled pane.

    The default slow mark shrinks with a short deadline, so the record lands well
    before the cancellation that stands in for the runner going away.
    """
    monkeypatch.setenv("OMNIGENT_HARNESS_STDERR_ENABLED", "1")
    terminal = _FakeTerminal([_STALLED_SCREEN])

    def slow_records() -> list[logging.LogRecord]:
        return [
            record
            for record in caplog.records
            if getattr(record, "event_name", None) == "terminal_interactive_slow"
        ]

    with caplog.at_level(logging.INFO, logger="omnigent.runner.app"):
        task = asyncio.create_task(
            _observe_terminal_interactive(
                session_id="conv_lost",
                terminal_id="terminal_claude_main",
                harness="claude-native",
                readiness_signal="claude_composer",
                instance=terminal,  # type: ignore[arg-type]
                is_interactive=lambda _pane: False,
                timeout_s=1.5,
                poll_interval_s=0.01,
            )
        )
        while not slow_records() and not task.done():
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    (record,) = slow_records()
    assert "refreshing workspace login" in record.attributes["terminal_last_output"]
    assert "refreshing workspace login" in record.getMessage()
    assert not any(
        getattr(record, "event_name", None)
        in {"terminal_interactive", "terminal_interactive_unobserved"}
        for record in caplog.records
    )


class _UnreadableJoinedTerminal(_FakeTerminal):
    """The readiness polls work, but the joined read for diagnostics fails."""

    async def read(self, *, join_wrapped: bool = False) -> dict[str, object]:
        if join_wrapped:
            raise RuntimeError("tmux capture-pane failed")
        return await super().read()


@pytest.mark.asyncio
async def test_observer_stays_content_free_when_the_pane_cannot_be_read(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed capture leaves the slow and deadline records without pane text."""
    monkeypatch.setenv("OMNIGENT_HARNESS_STDERR_ENABLED", "1")
    terminal = _UnreadableJoinedTerminal([_STALLED_SCREEN])

    with caplog.at_level(logging.INFO, logger="omnigent.runner.app"):
        await _observe_terminal_interactive(
            session_id="conv_unreadable",
            terminal_id="terminal_claude_main",
            harness="claude-native",
            readiness_signal="claude_composer",
            instance=terminal,  # type: ignore[arg-type]
            is_interactive=lambda _pane: False,
            timeout_s=0.05,
            slow_after_s=0.0,
            poll_interval_s=0.0,
        )

    records = [r for r in caplog.records if getattr(r, "event_name", None)]
    assert [r.event_name for r in records] == [
        "terminal_interactive_slow",
        "terminal_interactive_unobserved",
    ]
    for record in records:
        assert record.attributes["sign_in_prompt_detected"] is False
        assert "terminal_last_output" not in record.attributes
        assert "Terminal output:" not in record.getMessage()


@pytest.mark.asyncio
async def test_observer_outlives_a_diagnostics_failure(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken pane diagnostic costs the tail; the observer still reports readiness."""
    monkeypatch.setenv("OMNIGENT_HARNESS_STDERR_ENABLED", "1")
    monkeypatch.setattr(
        orchestration,
        "_redacted_terminal_text",
        lambda _text: (_ for _ in ()).throw(RuntimeError("sanitizer broke")),
    )
    terminal = _FakeTerminal([_STALLED_SCREEN, _STALLED_SCREEN, "composer ready"])

    with caplog.at_level(logging.INFO, logger="omnigent.runner.app"):
        await _observe_terminal_interactive(
            session_id="conv_recovers",
            terminal_id="terminal_claude_main",
            harness="claude-native",
            readiness_signal="claude_composer",
            instance=terminal,  # type: ignore[arg-type]
            is_interactive=lambda pane: pane == "composer ready",
            timeout_s=5.0,
            slow_after_s=0.0,
            poll_interval_s=0.0,
        )

    events = [r.event_name for r in caplog.records if getattr(r, "event_name", None)]
    assert events == ["terminal_interactive_slow", "terminal_interactive"]
    assert "terminal_last_output" not in {
        k for r in caplog.records for k, v in getattr(r, "attributes", {}).items() if v
    }


@pytest.mark.asyncio
async def test_observer_records_unobserved_without_raising(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A stopped pane produces diagnostics without failing session creation."""
    terminal = _FakeTerminal(["starting"], running=False)

    with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
        await _observe_terminal_interactive(
            session_id="conv_stopped",
            terminal_id="terminal_codex_main",
            harness="codex-native",
            readiness_signal="codex_composer",
            instance=terminal,  # type: ignore[arg-type]
            is_interactive=lambda _pane: False,
            timeout_s=1.0,
            poll_interval_s=0.0,
        )

    record = next(
        record
        for record in caplog.records
        if record.event_name == "terminal_interactive_unobserved"
    )
    assert record.attributes["reason"] == "RuntimeError"


_DIALOG_SCREEN = "Settings Error\n❯ 1. Fix it\nEnter to confirm · Esc to cancel\n"


def _dialog_watch(
    notified: list[str],
    *,
    category: ErrorCategory | None = None,
    notify_error: Exception | None = None,
) -> _StartupDialogWatch:
    """A watch that names a dialog by its first line and records each notice."""

    async def notify(headline: str) -> None:
        notified.append(headline)
        if notify_error is not None:
            raise notify_error

    return _StartupDialogWatch(
        headline=lambda pane: pane.splitlines()[0] if "Enter to confirm" in pane else None,
        notify=notify,
        category=lambda _headline: category,
    )


async def _observe(
    terminal: _FakeTerminal, watch: _StartupDialogWatch, *, timeout_s: float = 1.0
) -> None:
    await _observe_terminal_interactive(
        session_id="conv_dialog",
        terminal_id="terminal_claude_main",
        harness="claude-native",
        readiness_signal="claude_composer",
        instance=terminal,  # type: ignore[arg-type]
        is_interactive=lambda pane: pane == "composer ready",
        timeout_s=timeout_s,
        poll_interval_s=0.0,
        startup_dialog=watch,
    )


@pytest.mark.parametrize(
    ("category", "attribution"),
    [
        (
            ErrorCategory.CONFIG,
            {
                "error_category": "config",
                "error_impact": "blocking",
                "error_phase": "harness_startup",
            },
        ),
        (None, {}),
    ],
    ids=["config-dialog", "other-dialog"],
)
@pytest.mark.asyncio
async def test_observer_reports_a_startup_dialog_once_while_it_holds_the_pane(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    category: ErrorCategory | None,
    attribution: dict[str, str],
) -> None:
    """A dialog that stays up is reported once, and the pane still reaches readiness."""
    monkeypatch.setattr(orchestration, "_STARTUP_DIALOG_NOTICE_AFTER_S", 0.0)
    terminal = _FakeTerminal([_DIALOG_SCREEN] * 5 + ["composer ready"])
    notified: list[str] = []

    with caplog.at_level(logging.INFO, logger="omnigent.runner.app"):
        await _observe(terminal, _dialog_watch(notified, category=category))

    assert notified == ["Settings Error"]
    events = [getattr(record, "event_name", None) for record in caplog.records]
    assert events.count("native_startup_dialog") == 1
    assert "terminal_interactive" in events
    record = next(r for r in caplog.records if r.event_name == "native_startup_dialog")
    assert record.session_id == "conv_dialog"
    assert record.attributes["dialog_headline"] == "Settings Error"
    assert {
        key: record.attributes[key]
        for key in ("error_category", "error_impact", "error_phase")
        if key in record.attributes
    } == attribution


@pytest.mark.asyncio
async def test_observer_ignores_a_dialog_that_clears_before_the_notice_mark(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A dialog answered within the notice mark posts nothing."""
    terminal = _FakeTerminal(["starting", _DIALOG_SCREEN, _DIALOG_SCREEN, "composer ready"])
    notified: list[str] = []

    with caplog.at_level(logging.INFO, logger="omnigent.runner.app"):
        await _observe(terminal, _dialog_watch(notified))

    assert notified == []
    assert "native_startup_dialog" not in [getattr(r, "event_name", None) for r in caplog.records]


@pytest.mark.asyncio
async def test_observer_keeps_watching_when_the_dialog_notice_fails(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A notice the server refuses is logged, and readiness is still observed."""
    monkeypatch.setattr(orchestration, "_STARTUP_DIALOG_NOTICE_AFTER_S", 0.0)
    terminal = _FakeTerminal([_DIALOG_SCREEN] * 3 + ["composer ready"])
    notified: list[str] = []
    watch = _dialog_watch(notified, notify_error=httpx.ConnectError("server unreachable"))

    with caplog.at_level(logging.INFO, logger="omnigent.runner.app"):
        await _observe(terminal, watch)

    assert notified == ["Settings Error"]
    assert any("startup dialog notice" in r.getMessage() for r in caplog.records)
    assert "terminal_interactive" in [getattr(r, "event_name", None) for r in caplog.records]


@pytest.mark.asyncio
async def test_claude_startup_dialog_notice_posts_a_neutral_session_item() -> None:
    """The notice is an info item naming the dialog, so the web UI shows it as a notice."""
    posted: list[tuple[str, dict[str, object]]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        posted.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://server"
    ) as client:
        await _post_claude_startup_dialog_notice(
            "Settings Error", session_id="conv_dialog", server_client=client
        )

    [(path, body)] = posted
    assert path == "/v1/sessions/conv_dialog/events"
    assert body["type"] == "external_conversation_item"
    item = body["data"]["item_data"]  # type: ignore[index]
    assert body["data"]["item_type"] == "error"  # type: ignore[index]
    assert item["level"] == "info"
    assert item["code"] == "native_startup_dialog"
    assert "“Settings Error”" in item["message"]

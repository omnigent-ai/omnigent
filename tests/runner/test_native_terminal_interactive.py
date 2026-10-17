"""Tests for semantic native-terminal interactivity telemetry."""

from __future__ import annotations

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

    async def read(self) -> dict[str, object]:
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
    notify_errors: list[Exception | None] | None = None,
    source_ids: list[str] | None = None,
) -> _StartupDialogWatch:
    """A watch that names a dialog by its first line and records each notice attempt.

    *notify_error* fails every post; *notify_errors* scripts one outcome per
    attempt (``None`` succeeds), then the last outcome repeats. *source_ids*
    collects the stable key each attempt carried.
    """

    async def notify(headline: str, source_id: str) -> None:
        attempt = len(notified)
        notified.append(headline)
        if source_ids is not None:
            source_ids.append(source_id)
        if notify_errors is not None:
            error = notify_errors[min(attempt, len(notify_errors) - 1)]
        else:
            error = notify_error
        if error is not None:
            raise error

    return _StartupDialogWatch(
        headline=lambda pane: pane.splitlines()[0] if "Enter to confirm" in pane else None,
        notify=notify,
        category=lambda _headline: category,
    )


async def _observe(
    terminal: _FakeTerminal, watch: _StartupDialogWatch, *, timeout_s: float = 30.0
) -> None:
    # The scripted terminal reaches its last screen after a fixed number of polls,
    # so the budget only has to outlast a loaded CI runner's logging.
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
    terminal = _FakeTerminal([_DIALOG_SCREEN] * 8 + ["composer ready"])
    notified: list[str] = []
    watch = _dialog_watch(notified, notify_error=httpx.ConnectError("server unreachable"))

    with caplog.at_level(logging.INFO, logger="omnigent.runner.app"):
        await _observe(terminal, watch)

    # Retried while the dialog held the pane, but bounded: a lost response may
    # already have persisted the notice.
    assert notified == ["Settings Error"] * orchestration._STARTUP_DIALOG_NOTICE_ATTEMPTS
    events = [getattr(r, "event_name", None) for r in caplog.records]
    assert events.count("native_startup_dialog") == 1
    assert any("startup dialog notice" in r.getMessage() for r in caplog.records)
    assert "terminal_interactive" in events


_SECOND_DIALOG_SCREEN = "Accessing workspace:\n❯ No, exit\nEnter to confirm · Esc to cancel\n"


@pytest.mark.asyncio
async def test_observer_reports_each_distinct_dialog_once(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later, different dialog gets its own record and notice; a returning one is skipped."""
    monkeypatch.setattr(orchestration, "_STARTUP_DIALOG_NOTICE_AFTER_S", 0.0)
    screens = (
        [_DIALOG_SCREEN] * 3
        + ["booting"]
        + [_SECOND_DIALOG_SCREEN] * 3
        + [_DIALOG_SCREEN] * 3
        + ["composer ready"]
    )
    terminal = _FakeTerminal(screens)
    notified: list[str] = []

    with caplog.at_level(logging.INFO, logger="omnigent.runner.app"):
        await _observe(terminal, _dialog_watch(notified))

    assert notified == ["Settings Error", "Accessing workspace:"]
    records = [
        r for r in caplog.records if getattr(r, "event_name", None) == "native_startup_dialog"
    ]
    assert [r.attributes["dialog_headline"] for r in records] == [
        "Settings Error",
        "Accessing workspace:",
    ]


@pytest.mark.asyncio
async def test_observer_retries_the_notice_until_it_is_posted(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient failure does not lose the notice; once posted, no further attempt is made."""
    monkeypatch.setattr(orchestration, "_STARTUP_DIALOG_NOTICE_AFTER_S", 0.0)
    terminal = _FakeTerminal([_DIALOG_SCREEN] * 8 + ["composer ready"])
    notified: list[str] = []
    source_ids: list[str] = []
    watch = _dialog_watch(
        notified,
        notify_errors=[httpx.ConnectError("server unreachable"), None],
        source_ids=source_ids,
    )

    with caplog.at_level(logging.INFO, logger="omnigent.runner.app"):
        await _observe(terminal, watch)

    assert notified == ["Settings Error", "Settings Error"]
    # The retry reuses the key, so a repost of a notice whose response was lost dedupes.
    assert source_ids == ["native_startup_dialog:terminal-instance-test:Settings Error"] * 2
    events = [getattr(r, "event_name", None) for r in caplog.records]
    assert events.count("native_startup_dialog") == 1
    assert "terminal_interactive" in events


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
            "Settings Error",
            session_id="conv_dialog",
            server_client=client,
            source_id="native_startup_dialog:term-1:Settings Error",
        )

    [(path, body)] = posted
    assert path == "/v1/sessions/conv_dialog/events"
    assert body["type"] == "external_conversation_item"
    item = body["data"]["item_data"]  # type: ignore[index]
    assert body["data"]["item_type"] == "error"  # type: ignore[index]
    assert body["data"]["source_id"] == "native_startup_dialog:term-1:Settings Error"  # type: ignore[index]
    assert item["level"] == "info"
    assert item["code"] == "native_startup_dialog"
    # The collapsed notice shows only the title, so it must say what and where.
    assert item["title"] == "Claude Code is waiting for an answer in its terminal (Settings Error)"
    assert "“Settings Error”" in item["message"]

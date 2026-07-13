"""Focused tests for GooseExecutor.interrupt_session (#1748).

Verifies that the web Stop button (interrupt_session) correctly:
1. Returns False when there is no live process.
2. Falls back to SIGTERM when no ACP session has been established yet.
3. Sends ACP session/cancel when a session_id is known.
4. Falls back to SIGTERM when session/cancel errors out.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from omnigent.inner.goose_executor import GooseExecutor


def _make_executor() -> GooseExecutor:
    """Return a GooseExecutor with no real subprocess wired."""
    return GooseExecutor(cwd="/tmp", goose_path="/usr/bin/goose")


def _live_proc(returncode: int | None = None) -> MagicMock:
    """Return a mock subprocess with the given returncode."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.terminate = MagicMock()
    return proc


async def test_interrupt_no_process_returns_false() -> None:
    """interrupt_session returns False when no subprocess is running."""
    ex = _make_executor()
    assert ex._proc is None
    result = await ex.interrupt_session("s1")
    assert result is False


async def test_interrupt_dead_process_returns_false() -> None:
    """interrupt_session returns False when the process has already exited."""
    ex = _make_executor()
    ex._proc = _live_proc(returncode=0)
    result = await ex.interrupt_session("s1")
    assert result is False


async def test_interrupt_no_session_id_sends_sigterm() -> None:
    """Without a session_id, interrupt falls back to SIGTERM immediately."""
    ex = _make_executor()
    ex._proc = _live_proc()  # returncode=None → live
    ex._session_id = None  # no ACP session established yet

    result = await ex.interrupt_session("s1")

    assert result is True
    ex._proc.terminate.assert_called_once()


async def test_interrupt_with_session_sends_cancel_notification() -> None:
    """With a session_id, interrupt sends the session/cancel notification.

    ``session/cancel`` is an ACP notification: it goes out via ``_send`` with no
    ``id`` and no reply is expected (Goose ends the in-flight prompt with a
    cancelled stop reason). Asserting on ``_send`` exercises that real contract.
    """
    ex = _make_executor()
    ex._proc = _live_proc()
    ex._session_id = "goose_session_42"

    with patch.object(ex, "_send", new_callable=AsyncMock) as mock_send:
        result = await ex.interrupt_session("s1")

    assert result is True
    mock_send.assert_awaited_once()
    sent = mock_send.call_args.args[0]
    assert sent["method"] == "session/cancel"
    assert sent["params"]["sessionId"] == "goose_session_42"
    # A notification carries no id — the agent never responds to it.
    assert "id" not in sent
    # SIGTERM should NOT have been sent — the clean cancel was enough.
    ex._proc.terminate.assert_not_called()


async def test_interrupt_cancel_send_error_falls_back_to_sigterm() -> None:
    """When the session/cancel send errors, interrupt falls back to SIGTERM."""
    ex = _make_executor()
    ex._proc = _live_proc()
    ex._session_id = "goose_session_99"

    with patch.object(
        ex, "_send", new_callable=AsyncMock, side_effect=RuntimeError("broken pipe")
    ):
        result = await ex.interrupt_session("s1")

    assert result is True
    ex._proc.terminate.assert_called_once()


async def test_interrupt_process_lookup_error_returns_false() -> None:
    """ProcessLookupError on terminate → process vanished → returns False."""
    ex = _make_executor()
    proc = _live_proc()
    proc.terminate.side_effect = ProcessLookupError
    ex._proc = proc
    ex._session_id = None

    result = await ex.interrupt_session("s1")
    # suppress(ProcessLookupError) means we fall through to `return False`
    assert result is False

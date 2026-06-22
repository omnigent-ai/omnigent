"""Unit tests for the REPL's ``/compact`` slash command."""

from __future__ import annotations

import pytest
from omnigent_ui_sdk import RichBlockFormatter

from omnigent.repl._repl import COMMANDS, handle_slash_command
from tests.unit.repl.helpers import CapturingHost


class _CompactSession:
    """Minimal session stub for ``/compact`` tests."""

    def __init__(self, *, is_streaming: bool = False, fail: Exception | None = None) -> None:
        self.is_streaming = is_streaming
        self.calls = 0
        self._fail = fail

    async def compact(self) -> None:
        """Record the compaction request or raise the configured failure."""
        self.calls += 1
        if self._fail is not None:
            raise self._fail


@pytest.mark.asyncio
async def test_compact_command_registered() -> None:
    """``/compact`` appears in the slash-command registry."""
    assert "/compact" in COMMANDS
    assert "compact" in COMMANDS["/compact"][0].lower()


@pytest.mark.asyncio
async def test_compact_invokes_session_compact() -> None:
    """The command delegates to the session's explicit compaction hook.

    Progress messages ("Compacting…" / "Compaction complete.") are now
    delivered via the session SSE stream (response.compaction.in_progress /
    response.compaction.completed events) rather than emitted directly by
    the command handler, so we only assert that the session hook was called.
    """
    host = CapturingHost()
    session = _CompactSession()

    await handle_slash_command(
        "/compact",
        session,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        host,
        RichBlockFormatter(),  # type: ignore[arg-type]
    )

    assert session.calls == 1
    # No direct output — progress comes from the SSE stream.
    assert host.text == ""


@pytest.mark.asyncio
async def test_compact_refuses_while_streaming() -> None:
    """The command does not request compaction during an active response."""
    host = CapturingHost()
    session = _CompactSession(is_streaming=True)

    await handle_slash_command(
        "/compact",
        session,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        host,
        RichBlockFormatter(),  # type: ignore[arg-type]
    )

    assert session.calls == 0
    assert "Cannot compact while a response is running" in host.text


@pytest.mark.asyncio
async def test_compact_surfaces_failure() -> None:
    """Compaction errors render inline instead of crashing the REPL."""
    host = CapturingHost()
    session = _CompactSession(fail=RuntimeError("boom"))

    await handle_slash_command(
        "/compact",
        session,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        host,
        RichBlockFormatter(),  # type: ignore[arg-type]
    )

    assert session.calls == 1
    assert "Compaction failed: boom" in host.text

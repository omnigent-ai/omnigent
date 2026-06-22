"""Tests for the REPL ``/fork`` slash command.

Exercises every branch of ``_cmd_fork``: happy path (prints old
session id + switches in-place), legacy-mode rejection, server
error, title forwarding, and empty-item forks. Uses the same
stub-and-capture pattern as ``test_repl.py``'s ``/clear`` and
``/new`` command tests.
"""

from __future__ import annotations

from io import StringIO
from typing import Any

import pytest

from omnigent.repl import _repl as repl_mod

pytestmark = pytest.mark.asyncio


# ── Stubs ────────────────────────────────────────────────


class _StubSessionWithId:
    """
    Sessions-API adapter stub that exposes ``session_id``.

    Mimics :class:`_SessionsChatReplAdapter` for the fork
    command's ``getattr(session, "session_id", None)`` check
    and ``switch_session()`` call.

    :param session_id: The durable session ID to expose, e.g.
        ``"conv_src123"``.
    """

    def __init__(self, session_id: str) -> None:
        self.session_id: str | None = session_id
        self.model = "test-agent"
        self.switch_session_calls: list[str] = []

    def switch_session(self, new_session_id: str) -> None:
        self.switch_session_calls.append(new_session_id)
        self.session_id = new_session_id


class _LegacyStubSession:
    """
    Legacy-mode session stub that has no ``session_id``.

    When ``getattr(session, "session_id", None)`` returns ``None``,
    the ``/fork`` command should reject with an error message.
    """

    model = "legacy-agent"


class _CapturingHost:
    """
    Minimal host stub that records ``output()`` calls.

    Identical to ``test_repl.py``'s ``_StubHost`` but adds
    ``render_plain()`` for content assertions.
    """

    def __init__(self) -> None:
        self.outputs: list[object] = []

    def output(self, item: object) -> None:
        self.outputs.append(item)

    def render_plain(self) -> str:
        """
        Project captured renderables to plain text.

        :returns: Concatenated plain-text rendering of all outputs.
        """
        from rich.console import Console

        buf = StringIO()
        console = Console(
            file=buf,
            force_terminal=False,
            width=200,
            color_system=None,
        )
        for item in self.outputs:
            console.print(item)
        return buf.getvalue()


class _StubFmt:
    """
    Minimal formatter stub carrying style names the ``/fork``
    handler reads (``fmt.muted``, ``fmt.accent``).
    """

    muted = "dim"
    accent = "bold"

    def welcome(self, name: str, hints: object) -> str:
        return f"<welcome:{name}>"


class _StubClient:
    """
    Agent-plane client stub with a controllable ``sessions.fork()``
    method.

    Raises ``AssertionError`` if any unexpected method is called,
    so regressions that add new client calls surface loudly.

    :param fork_result: The dict to return from ``sessions.fork()``.
    :param fork_error: If set, ``sessions.fork()`` raises this
        instead of returning.
    """

    def __init__(
        self,
        fork_result: dict[str, Any] | None = None,
        fork_error: Exception | None = None,
    ) -> None:
        self.sessions = _StubSessionsNamespace(
            fork_result=fork_result,
            fork_error=fork_error,
        )


class _StubSessionsNamespace:
    """
    ``client.sessions`` namespace stub.

    Records the ``fork()`` call arguments for assertion.

    :param fork_result: Dict to return from ``fork()``.
    :param fork_error: Exception to raise from ``fork()``.
    """

    def __init__(
        self,
        fork_result: dict[str, Any] | None = None,
        fork_error: Exception | None = None,
    ) -> None:
        self._fork_result = fork_result
        self._fork_error = fork_error
        self.fork_calls: list[dict[str, Any]] = []

    async def fork(
        self,
        source_session_id: str,
        *,
        title: str | None = None,
    ) -> dict[str, Any]:
        """
        Record the call and return the configured result.

        :param source_session_id: Source session ID.
        :param title: Optional fork title.
        :returns: The configured fork result.
        :raises: The configured fork error, if set.
        """
        self.fork_calls.append(
            {
                "source_session_id": source_session_id,
                "title": title,
            }
        )
        if self._fork_error is not None:
            raise self._fork_error
        assert self._fork_result is not None
        return self._fork_result


# ── Tests ────────────────────────────────────────────────


async def test_fork_command_registered() -> None:
    """``/fork`` is in the COMMANDS registry so ``/help`` lists it."""
    assert "/fork" in repl_mod.COMMANDS, "/fork missing from COMMANDS registry"
    help_text, _ = repl_mod.COMMANDS["/fork"]
    assert "fork" in help_text.lower(), (
        f"/fork help text should mention forking; got {help_text!r}"
    )


async def test_fork_happy_path_switches_in_place() -> None:
    """
    ``/fork`` calls ``client.sessions.fork()``, prints the old
    session id for recovery, and calls ``switch_session()`` on
    the adapter to continue in the fork — no screen clear, no
    transcript repaint.

    Claim: if ``switch_session`` is never called, the REPL
    stays on the source session — the user thinks they forked
    but subsequent messages go to the original conversation.
    If the old session id isn't shown, the user has no way to
    recover the original.
    """
    fork_result = {
        "id": "conv_fork_abc123",
        "agent_id": "ag_cloned",
        "status": "idle",
        "created_at": 1234,
        "title": "Fork of My Chat",
        "items": [
            {"id": "msg_1", "type": "message"},
            {"id": "msg_2", "type": "message"},
        ],
    }
    client = _StubClient(fork_result=fork_result)
    host = _CapturingHost()
    session = _StubSessionWithId("conv_src_999")

    await repl_mod.handle_slash_command(
        "/fork",
        session,  # type: ignore[arg-type]
        client,  # type: ignore[arg-type]
        host,  # type: ignore[arg-type]
        _StubFmt(),  # type: ignore[arg-type]
    )

    # fork() was called with the correct source session ID.
    assert len(client.sessions.fork_calls) == 1, (
        f"Expected exactly 1 fork() call, got {len(client.sessions.fork_calls)}. "
        "If 0, the handler never reached the SDK call."
    )
    assert client.sessions.fork_calls[0]["source_session_id"] == "conv_src_999", (
        f"Fork was called with wrong source ID: "
        f"{client.sessions.fork_calls[0]['source_session_id']!r}."
    )

    # switch_session was called with the fork's conversation ID.
    assert session.switch_session_calls == ["conv_fork_abc123"], (
        f"Expected switch_session('conv_fork_abc123'), "
        f"got {session.switch_session_calls!r}. If empty, the REPL "
        "never switched — subsequent messages go to the original."
    )

    # Output shows actionable recovery instructions with the old session id.
    plain = host.render_plain()
    assert "conversation forked" in plain.lower(), (
        f"Confirmation should say 'Conversation forked', got: {plain!r}."
    )
    assert "/switch conv_src_999" in plain, (
        f"Output should show '/switch <previous id>' for recovery, got: {plain!r}."
    )

    # Only one output line — no screen clear, no welcome banner,
    # no transcript re-render.
    assert len(host.outputs) == 1, (
        f"Expected exactly 1 output (the confirmation line), got "
        f"{len(host.outputs)}. Extra outputs mean the screen was "
        "repainted or a banner was drawn."
    )


async def test_fork_with_title_passes_title_to_sdk() -> None:
    """
    ``/fork my experiment`` passes ``"my experiment"`` as the title
    to ``client.sessions.fork()``.

    Claim: if the title is ``None`` instead of the user's string,
    the fork gets an auto-derived name instead of the user's choice.
    """
    fork_result = {
        "id": "conv_fork_titled",
        "agent_id": "ag_cloned",
        "status": "idle",
        "created_at": 1234,
        "title": "my experiment",
        "items": [],
    }
    client = _StubClient(fork_result=fork_result)
    host = _CapturingHost()
    session = _StubSessionWithId("conv_src_1")

    await repl_mod.handle_slash_command(
        "/fork my experiment",
        session,  # type: ignore[arg-type]
        client,  # type: ignore[arg-type]
        host,  # type: ignore[arg-type]
        _StubFmt(),  # type: ignore[arg-type]
    )

    assert len(client.sessions.fork_calls) == 1
    assert client.sessions.fork_calls[0]["title"] == "my experiment", (
        f"Expected title='my experiment', got "
        f"{client.sessions.fork_calls[0]['title']!r}. "
        "The /fork command should forward the argument as the fork title."
    )


async def test_fork_without_title_passes_none() -> None:
    """
    ``/fork`` with no argument passes ``title=None`` so the server
    derives a default title.

    Claim: if title is an empty string instead of None, the server
    may create a fork with a blank title rather than deriving one.
    """
    fork_result = {
        "id": "conv_fork_notitle",
        "agent_id": "ag_cloned",
        "status": "idle",
        "created_at": 1234,
        "title": "Fork of Original",
        "items": [],
    }
    client = _StubClient(fork_result=fork_result)
    host = _CapturingHost()
    session = _StubSessionWithId("conv_src_2")

    await repl_mod.handle_slash_command(
        "/fork",
        session,  # type: ignore[arg-type]
        client,  # type: ignore[arg-type]
        host,  # type: ignore[arg-type]
        _StubFmt(),  # type: ignore[arg-type]
    )

    assert len(client.sessions.fork_calls) == 1
    assert client.sessions.fork_calls[0]["title"] is None, (
        f"Expected title=None when no argument given, got "
        f"{client.sessions.fork_calls[0]['title']!r}. "
        "An empty string title would prevent server-side derivation."
    )


async def test_fork_legacy_mode_renders_error() -> None:
    """
    ``/fork`` in legacy mode (no ``session_id`` attribute) renders
    an inline error and does NOT call ``sessions.fork()``.

    Claim: if the error is missing, the handler tried to fork
    without a session ID, which would crash or send a malformed
    request. If fork() was called, the guard was bypassed.
    """
    client = _StubClient(fork_result={"id": "should_not_reach"})
    host = _CapturingHost()
    session = _LegacyStubSession()

    await repl_mod.handle_slash_command(
        "/fork",
        session,  # type: ignore[arg-type]
        client,  # type: ignore[arg-type]
        host,  # type: ignore[arg-type]
        _StubFmt(),  # type: ignore[arg-type]
    )

    plain = host.render_plain()
    # Error message must mention the sessions API requirement.
    assert "sessions API" in plain.lower() or "legacy" in plain.lower(), (
        f"Expected legacy-mode error mentioning 'sessions API' or 'legacy', got: {plain!r}."
    )

    # fork() must NOT have been called.
    assert client.sessions.fork_calls == [], (
        f"fork() should not be called in legacy mode, but got "
        f"{len(client.sessions.fork_calls)} call(s)."
    )


async def test_fork_server_error_renders_inline_error() -> None:
    """
    When ``client.sessions.fork()`` raises, the error is rendered
    inline instead of crashing the REPL.

    Claim: if the exception escapes, the REPL's background task
    dies and the prompt stops responding. The handler must catch
    and render.
    """
    client = _StubClient(
        fork_error=RuntimeError("server returned 500"),
    )
    host = _CapturingHost()
    session = _StubSessionWithId("conv_src_3")

    await repl_mod.handle_slash_command(
        "/fork",
        session,  # type: ignore[arg-type]
        client,  # type: ignore[arg-type]
        host,  # type: ignore[arg-type]
        _StubFmt(),  # type: ignore[arg-type]
    )

    plain = host.render_plain()
    # The error must be rendered, not swallowed or raised.
    assert "fork failed" in plain.lower() or "server returned 500" in plain.lower(), (
        f"Expected inline error message about the fork failure, got: {plain!r}. "
        "If empty, the exception was swallowed without rendering feedback."
    )

    # Ensure at least one output was captured (the error message).
    assert len(host.outputs) >= 1, (
        f"Expected at least 1 output (the error message), got {len(host.outputs)}."
    )

    # switch_session must NOT have been called — fork failed.
    assert session.switch_session_calls == [], (
        f"switch_session should not be called after a fork error, "
        f"got {session.switch_session_calls!r}."
    )


async def test_fork_empty_items_still_switches() -> None:
    """
    Forking an empty session still switches to the fork and shows
    the previous session id.

    Claim: an empty fork is valid (e.g. forking before any messages).
    The switch must still happen so subsequent messages go to the fork.
    """
    fork_result = {
        "id": "conv_fork_empty",
        "agent_id": "ag_cloned",
        "status": "idle",
        "created_at": 1234,
        "title": "Fork of empty",
        "items": [],
    }
    client = _StubClient(fork_result=fork_result)
    host = _CapturingHost()
    session = _StubSessionWithId("conv_src_5")

    await repl_mod.handle_slash_command(
        "/fork",
        session,  # type: ignore[arg-type]
        client,  # type: ignore[arg-type]
        host,  # type: ignore[arg-type]
        _StubFmt(),  # type: ignore[arg-type]
    )

    assert session.switch_session_calls == ["conv_fork_empty"], (
        f"Expected switch_session for empty fork, got {session.switch_session_calls!r}."
    )

    plain = host.render_plain()
    assert "/switch conv_src_5" in plain, (
        f"Expected '/switch <previous id>' in output, got: {plain!r}."
    )

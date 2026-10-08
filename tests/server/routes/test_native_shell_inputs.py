"""Shell-mode transcript settlement and its pending-input boundaries."""

from typing import Any

import pytest

from omnigent.runtime import session_stream
from omnigent.server.schemas import SessionEventInput
from tests.server.routes.test_session_resources import _ConversationStore, _FailOnceStore


def _mirror_event(item_type: str, item_data: dict[str, Any], source_id: str) -> SessionEventInput:
    """Build the ``external_conversation_item`` event a transcript forwarder posts."""
    return SessionEventInput(
        type="external_conversation_item",
        data={
            "item_type": item_type,
            "item_data": item_data,
            "response_id": f"resp_{source_id}",
            "source_id": source_id,
        },
    )


def _user_mirror(text: str, source_id: str) -> SessionEventInput:
    """A user message the transcript mirrored back."""
    content = [{"type": "input_text", "text": text}]
    return _mirror_event("message", {"role": "user", "content": content}, source_id)


def _shell_mirror(kind: str, source_id: str, **fields: Any) -> SessionEventInput:
    """One half (``"input"`` or ``"output"``) of a mirrored ``!cmd`` shell command."""
    return _mirror_event("terminal_command", {"kind": kind, **fields}, source_id)


def _consumed_receipts(published: list[tuple[str, dict[str, Any]]]) -> list[str | None]:
    """The ``cleared_pending_id`` of every ``session.input.consumed`` event, in order."""
    return [
        event["data"]["cleared_pending_id"]
        for _conversation_id, event in published
        if event.get("type") == "session.input.consumed"
    ]


@pytest.mark.asyncio
async def test_claude_native_mirrored_shell_command_drains_its_queued_entry() -> None:
    """A web ``!cmd`` mirrored as terminal_command input clears its own queued entry.

    Claude Code runs the message as a shell command and records it as
    terminal-command items, never as a user message, so without this the entry
    outlives the command and the next ordinary message would skip it, persisting
    a false "not delivered" error for a command that ran. Older entries stay.
    """
    from omnigent.runtime import pending_inputs
    from omnigent.server.routes.sessions import _persist_external_conversation_item

    pending_inputs.reset_for_tests()
    store = _ConversationStore()
    sid = "64a784c3aa907d1774f44313546947c6"
    conv = store.get_conversation(sid)
    assert conv is not None
    older = pending_inputs.record(sid, [{"type": "input_text", "text": "still on its way"}])
    pending_inputs.record(sid, [{"type": "input_text", "text": "!ls -la"}])

    try:
        await _persist_external_conversation_item(
            sid,
            conv,
            _shell_mirror("input", "claude:ls:0", input="ls -la"),
            store,  # type: ignore[arg-type]
        )

        assert [item.type for item in store.appended_items] == ["terminal_command"]
        assert [entry["pending_id"] for entry in pending_inputs.snapshot_for(sid)] == [older]
    finally:
        pending_inputs.reset_for_tests()


@pytest.mark.asyncio
async def test_claude_native_message_after_a_shell_command_reports_no_lost_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The next message after a web ``!cmd`` persists cleanly, with no error item.

    The user sends ``!echo hi`` from the web composer, Claude runs it, and the
    forwarder mirrors the command and its output. The following ordinary
    message used to skip the still-queued ``!echo hi`` entry and persist it as
    undelivered next to an error, although the command had run and shown.
    """
    from omnigent.runtime import pending_inputs
    from omnigent.server.routes.sessions import _persist_external_conversation_item

    published: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        session_stream,
        "publish",
        lambda conversation_id, event: published.append((conversation_id, event)),
    )
    pending_inputs.reset_for_tests()
    store = _ConversationStore()
    sid = "64a784c3aa907d1774f44313546947c6"
    conv = store.get_conversation(sid)
    assert conv is not None
    pending_inputs.record(sid, [{"type": "input_text", "text": "!echo hi"}])

    try:
        for event in (
            _shell_mirror("input", "claude:echo:0", input="echo hi"),
            _shell_mirror("output", "claude:echo:1", stdout="hi", stderr=""),
        ):
            await _persist_external_conversation_item(
                sid,
                conv,
                event,
                store,  # type: ignore[arg-type]
            )
        assert pending_inputs.snapshot_for(sid) == []
        following = pending_inputs.record(sid, [{"type": "input_text", "text": "thanks"}])
        await _persist_external_conversation_item(
            sid,
            conv,
            _user_mirror("thanks", "claude:thanks:0"),
            store,  # type: ignore[arg-type]
        )

        assert [item.type for item in store.appended_items] == [
            "terminal_command",
            "terminal_command",
            "message",
        ]
        assert store.appended_items[-1].data.content == [{"type": "input_text", "text": "thanks"}]
        # Only the ordinary message produced a receipt, and nothing was skipped.
        assert _consumed_receipts(published) == [following]
        assert pending_inputs.snapshot_for(sid) == []
    finally:
        pending_inputs.reset_for_tests()


@pytest.mark.asyncio
async def test_claude_native_shell_command_output_drains_nothing() -> None:
    """Only the input half of a shell command settles its queued entry."""
    from omnigent.runtime import pending_inputs
    from omnigent.server.routes.sessions import _persist_external_conversation_item

    pending_inputs.reset_for_tests()
    store = _ConversationStore()
    sid = "64a784c3aa907d1774f44313546947c6"
    conv = store.get_conversation(sid)
    assert conv is not None
    command = pending_inputs.record(sid, [{"type": "input_text", "text": "!ls"}])

    try:
        await _persist_external_conversation_item(
            sid,
            conv,
            _shell_mirror("output", "claude:ls:1", stdout="a.txt", stderr=""),
            store,  # type: ignore[arg-type]
        )

        assert [item.type for item in store.appended_items] == ["terminal_command"]
        assert [entry["pending_id"] for entry in pending_inputs.snapshot_for(sid)] == [command]
    finally:
        pending_inputs.reset_for_tests()


@pytest.mark.asyncio
async def test_claude_native_shell_command_typed_in_the_terminal_leaves_the_queue_alone() -> None:
    """A command run in the TUI, with no web entry of its own, consumes nothing queued."""
    from omnigent.runtime import pending_inputs
    from omnigent.server.routes.sessions import _persist_external_conversation_item

    pending_inputs.reset_for_tests()
    store = _ConversationStore()
    sid = "64a784c3aa907d1774f44313546947c6"
    conv = store.get_conversation(sid)
    assert conv is not None
    queued = pending_inputs.record(sid, [{"type": "input_text", "text": "plain message"}])

    try:
        await _persist_external_conversation_item(
            sid,
            conv,
            _shell_mirror("input", "claude:pwd:0", input="pwd"),
            store,  # type: ignore[arg-type]
        )

        assert [item.type for item in store.appended_items] == ["terminal_command"]
        assert [entry["pending_id"] for entry in pending_inputs.snapshot_for(sid)] == [queued]
    finally:
        pending_inputs.reset_for_tests()


@pytest.mark.asyncio
async def test_claude_native_shell_command_leaves_older_uncertain_entries_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An older entry a positional drain made uncertain stays in play after a shell command.

    A shell command drains only its own entry. An older entry queued when an
    unmatched mirror drained by position is uncertain: neither declared lost nor
    consumed, so the persist must hand it back. Left held, no later mirror could
    match it: its own message would persist without a receipt and its bubble
    would stay queued until the TTL.
    """
    from omnigent.runtime import pending_inputs
    from omnigent.server.routes.sessions import _persist_external_conversation_item

    published: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        session_stream,
        "publish",
        lambda conversation_id, event: published.append((conversation_id, event)),
    )
    pending_inputs.reset_for_tests()
    store = _ConversationStore()
    sid = "64a784c3aa907d1774f44313546947c6"
    conv = store.get_conversation(sid)
    assert conv is not None
    first = pending_inputs.record(sid, [{"type": "input_text", "text": "reformatted by the TUI"}])
    second = pending_inputs.record(sid, [{"type": "input_text", "text": "still on its way"}])
    pending_inputs.record(sid, [{"type": "input_text", "text": "!ls"}])
    # A mirror that matched nothing drained the oldest entry by position, which
    # leaves everything queued behind it uncertain.
    positional = pending_inputs.resolve_oldest(sid, hold=True)
    assert positional is not None and positional.pending_id == first
    pending_inputs.mark_uncertain(sid)
    pending_inputs.release(sid, positional)

    try:
        await _persist_external_conversation_item(
            sid,
            conv,
            _shell_mirror("input", "claude:ls:0", input="ls"),
            store,  # type: ignore[arg-type]
        )

        # Not persisted as undelivered, and still queued.
        assert [item.type for item in store.appended_items] == ["terminal_command"]
        assert [entry["pending_id"] for entry in pending_inputs.snapshot_for(sid)] == [second]

        await _persist_external_conversation_item(
            sid,
            conv,
            _user_mirror("still on its way", "claude:still-on-its-way:0"),
            store,  # type: ignore[arg-type]
        )

        # Its own mirror matches it and names it in the receipt.
        assert [item.type for item in store.appended_items] == ["terminal_command", "message"]
        assert _consumed_receipts(published) == [second]
        assert pending_inputs.snapshot_for(sid) == []
    finally:
        pending_inputs.reset_for_tests()


@pytest.mark.asyncio
async def test_claude_native_failed_shell_command_append_restores_its_entry() -> None:
    """A shell-command mirror whose append fails puts its queued entry back, in order."""
    from omnigent.runtime import pending_inputs
    from omnigent.server.routes.sessions import _persist_external_conversation_item

    pending_inputs.reset_for_tests()
    store = _FailOnceStore()
    sid = "64a784c3aa907d1774f44313546947c6"
    conv = store.get_conversation(sid)
    assert conv is not None
    older = pending_inputs.record(sid, [{"type": "input_text", "text": "still on its way"}])
    command = pending_inputs.record(sid, [{"type": "input_text", "text": "!ls"}])
    event = _shell_mirror("input", "claude:ls:0", input="ls")

    try:
        with pytest.raises(RuntimeError, match="database unavailable"):
            await _persist_external_conversation_item(
                sid,
                conv,
                event,
                store,  # type: ignore[arg-type]
            )
        assert store.appended_items == []
        assert [entry["pending_id"] for entry in pending_inputs.snapshot_for(sid)] == [
            older,
            command,
        ]

        await _persist_external_conversation_item(
            sid,
            conv,
            event,
            store,  # type: ignore[arg-type]
        )

        assert [item.type for item in store.appended_items] == ["terminal_command"]
        assert [entry["pending_id"] for entry in pending_inputs.snapshot_for(sid)] == [older]
    finally:
        pending_inputs.reset_for_tests()


@pytest.mark.asyncio
async def test_claude_native_retried_shell_command_mirror_leaves_the_queue_alone() -> None:
    """A forwarder retry of a persisted shell input must not drain a newer identical entry."""
    from omnigent.runtime import pending_inputs
    from omnigent.server.routes.sessions import _persist_external_conversation_item

    pending_inputs.reset_for_tests()
    store = _ConversationStore()
    sid = "64a784c3aa907d1774f44313546947c6"
    conv = store.get_conversation(sid)
    assert conv is not None
    pending_inputs.record(sid, [{"type": "input_text", "text": "!ls"}])
    event = _shell_mirror("input", "claude:ls:0", input="ls")

    try:
        first_id = await _persist_external_conversation_item(
            sid,
            conv,
            event,
            store,  # type: ignore[arg-type]
        )
        assert pending_inputs.snapshot_for(sid) == []
        # The person runs the same command again before the retry arrives.
        again = pending_inputs.record(sid, [{"type": "input_text", "text": "!ls"}])

        retried_id = await _persist_external_conversation_item(
            sid,
            conv,
            event,
            store,  # type: ignore[arg-type]
        )

        assert retried_id == first_id
        assert [item.type for item in store.appended_items] == ["terminal_command"]
        assert [entry["pending_id"] for entry in pending_inputs.snapshot_for(sid)] == [again]
    finally:
        pending_inputs.reset_for_tests()


@pytest.mark.asyncio
async def test_claude_native_lost_shell_message_is_still_reported_as_not_recorded() -> None:
    """A shell message with no matching input mirror still reports delivery failure."""
    from omnigent.runtime import pending_inputs
    from omnigent.server.routes.sessions import _persist_external_conversation_item

    pending_inputs.reset_for_tests()
    store = _ConversationStore()
    sid = "64a784c3aa907d1774f44313546947c6"
    conv = store.get_conversation(sid)
    assert conv is not None
    pending_inputs.record(sid, [{"type": "input_text", "text": "!never ran"}])
    pending_inputs.record(sid, [{"type": "input_text", "text": "still here?"}])

    try:
        await _persist_external_conversation_item(
            sid,
            conv,
            _user_mirror("still here?", "claude:still-here:0"),
            store,  # type: ignore[arg-type]
        )

        assert [item.type for item in store.appended_items] == ["message", "error", "message"]
        lost_error = store.appended_items[1].data
        assert lost_error.code == "native_prompt_not_recorded"
        assert lost_error.level is None
        assert store.appended_items[0].data.content == [
            {"type": "input_text", "text": "!never ran"}
        ]
        assert pending_inputs.snapshot_for(sid) == []
    finally:
        pending_inputs.reset_for_tests()

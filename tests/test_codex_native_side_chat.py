"""Unit tests for Codex ``/side`` ephemeral side-chat support.

Each test verifies one piece of the mechanism against the real forwarder code
and the real ``side_chat`` helpers, with the app-server and Omnigent HTTP calls
faked. No live Codex is needed.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from omnigent.harnesses.codex_native import forwarder as fwd
from omnigent.harnesses.codex_native import side_chat

_JsonObject = dict[str, Any]


class _FakeCodexClient:
    """Records app-server ``request`` calls and returns canned envelopes."""

    def __init__(self, responses: dict[str, _JsonObject]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, _JsonObject]] = []

    async def request(self, method: str, params: _JsonObject) -> _JsonObject:
        self.calls.append((method, params))
        return self.responses.get(method, {"result": {}})


def _text_items(text: str) -> list[_JsonObject]:
    return [{"type": "text", "text": text}]


def _fork_started(
    *, thread_id: str, forked_from: str | None, ephemeral: bool = True
) -> _JsonObject:
    thread: _JsonObject = {"id": thread_id, "ephemeral": ephemeral}
    if forked_from is not None:
        thread["forkedFromId"] = forked_from
    return {"method": "thread/started", "params": {"thread": thread}}


# --------------------------------------------------------------------------- #
# side_chat_question — /side detection on normalized turn input
# --------------------------------------------------------------------------- #
def test_side_chat_question_extracts_question() -> None:
    assert side_chat.side_chat_question(_text_items("/side why is the sky blue?")) == (
        "why is the sky blue?"
    )


def test_side_chat_question_ignores_non_side_and_empty() -> None:
    assert side_chat.side_chat_question(_text_items("hello")) is None
    assert side_chat.side_chat_question(_text_items("/sidebar thing")) is None  # needs the space
    assert side_chat.side_chat_question(_text_items("/side   ")) is None  # empty question
    assert side_chat.side_chat_question([]) is None
    assert side_chat.side_chat_question(_text_items("a") + _text_items("b")) is None  # multi-item


# --------------------------------------------------------------------------- #
# fork_ephemeral_side_thread / submit_side_turn / open_side_chat_on_client
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_fork_sends_ephemeral_fork_rpc_and_returns_child_id() -> None:
    client = _FakeCodexClient({"thread/fork": {"result": {"thread": {"id": "thread_side"}}}})

    child = await side_chat.fork_ephemeral_side_thread(client, "thread_parent")

    assert child == "thread_side"
    method, params = client.calls[0]
    assert method == "thread/fork"
    assert params["threadId"] == "thread_parent"
    assert params["ephemeral"] is True
    assert "side conversation" in params["developerInstructions"]


@pytest.mark.asyncio
async def test_fork_returns_none_when_response_has_no_thread_id() -> None:
    client = _FakeCodexClient({"thread/fork": {"result": {}}})
    assert await side_chat.fork_ephemeral_side_thread(client, "thread_parent") is None


@pytest.mark.asyncio
async def test_submit_side_turn_targets_child_thread() -> None:
    client = _FakeCodexClient({"turn/start": {"result": {"turn": {"id": "turn_1"}}}})

    turn_id = await side_chat.submit_side_turn(client, "thread_side", "why is the sky blue?")

    assert turn_id == "turn_1"
    method, params = client.calls[0]
    assert method == "turn/start"
    assert params["threadId"] == "thread_side"
    assert params["input"] == [{"type": "text", "text": "why is the sky blue?"}]
    assert "collaborationMode" not in params


@pytest.mark.asyncio
async def test_open_side_chat_forks_then_submits_first_turn() -> None:
    client = _FakeCodexClient(
        {
            "thread/fork": {"result": {"thread": {"id": "thread_side"}}},
            "turn/start": {"result": {"turn": {"id": "turn_1"}}},
        }
    )

    child = await side_chat.open_side_chat_on_client(
        client, parent_thread_id="thread_parent", question="what does this repo do?"
    )

    assert child == "thread_side"
    # order: fork first, then the first turn on the CHILD (not the main thread)
    assert [m for m, _ in client.calls] == ["thread/fork", "turn/start"]
    assert client.calls[1][1]["threadId"] == "thread_side"


@pytest.mark.asyncio
async def test_open_side_chat_aborts_when_fork_fails() -> None:
    client = _FakeCodexClient({"thread/fork": {"result": {}}})  # no thread id
    child = await side_chat.open_side_chat_on_client(
        client, parent_thread_id="thread_parent", question="q"
    )
    assert child is None
    assert [m for m, _ in client.calls] == ["thread/fork"]  # no turn submitted


# --------------------------------------------------------------------------- #
# is_omnigent_side_fork — discriminator
# --------------------------------------------------------------------------- #
def test_is_omnigent_side_fork_discriminates_fork_from_system_ephemeral() -> None:
    assert side_chat.is_omnigent_side_fork(
        _fork_started(thread_id="s", forked_from="thread_parent")
    )
    # system/housekeeping ephemeral (no forkedFromId) must NOT match
    assert not side_chat.is_omnigent_side_fork(_fork_started(thread_id="sys", forked_from=None))
    # non-thread/started
    assert not side_chat.is_omnigent_side_fork({"method": "turn/started", "params": {}})


# --------------------------------------------------------------------------- #
# register_side_fork_child — forwarder auto-surface
# --------------------------------------------------------------------------- #
def _ensure_stub(child_session_id: str | None) -> AsyncMock:
    """Stand in for ``_ensure_child_session``, mapping the child like the real one."""

    async def _ensure(_client, **kwargs: Any) -> None:
        if child_session_id is not None:
            kwargs["forwarder_state"].note_child_thread(
                kwargs["child_thread_id"], child_session_id
            )

    return AsyncMock(side_effect=_ensure)


@pytest.mark.asyncio
async def test_register_side_fork_child_registers_matching_fork(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure = _ensure_stub("conv_side")
    monkeypatch.setattr(fwd, "_ensure_child_session", ensure)
    state = fwd._CodexForwarderState()

    child_session = await side_chat.register_side_fork_child(
        object(),  # ap_client unused: the pipeline is stubbed
        forwarder_state=state,
        parent_session_id="conv_parent",
        parent_thread_id="thread_parent",
        event=_fork_started(thread_id="thread_side", forked_from="thread_parent"),
    )

    assert child_session == "conv_side"
    assert state.session_for_child_thread("thread_side") == "conv_side"
    kwargs = ensure.await_args.kwargs
    assert kwargs["parent_session_id"] == "conv_parent"
    assert kwargs["child_thread_id"] == "thread_side"
    # a display name, so the rail/composer tray never shows the raw thread id
    assert kwargs["item"]["agent_nickname"] == side_chat.SIDE_CHAT_DISPLAY_NAME


@pytest.mark.asyncio
async def test_register_side_fork_child_backfills_via_the_full_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The side fork must go through ``_ensure_child_session``.

    Registering the child alone leaves the side chat an empty session: only the
    pipeline's backfill replays the forked thread, so a bare
    ``_register_child_session`` call is the bug this guards.
    """
    ensure = _ensure_stub("conv_side")
    register = AsyncMock(return_value="conv_side")
    monkeypatch.setattr(fwd, "_ensure_child_session", ensure)
    monkeypatch.setattr(fwd, "_register_child_session", register)

    await side_chat.register_side_fork_child(
        object(),
        forwarder_state=fwd._CodexForwarderState(),
        parent_session_id="conv_parent",
        parent_thread_id="thread_parent",
        event=_fork_started(thread_id="thread_side", forked_from="thread_parent"),
    )

    ensure.assert_awaited_once()
    register.assert_not_awaited()  # never bypass the pipeline


@pytest.mark.asyncio
async def test_register_side_fork_child_ignores_system_ephemeral_and_wrong_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure = _ensure_stub("conv_side")
    monkeypatch.setattr(fwd, "_ensure_child_session", ensure)
    state = fwd._CodexForwarderState()

    # system ephemeral (no forkedFromId) -> ignored
    assert (
        await side_chat.register_side_fork_child(
            object(),
            forwarder_state=state,
            parent_session_id="conv_parent",
            parent_thread_id="thread_parent",
            event=_fork_started(thread_id="sys", forked_from=None),
        )
        is None
    )
    # a fork of a DIFFERENT thread (not our parent) -> ignored
    assert (
        await side_chat.register_side_fork_child(
            object(),
            forwarder_state=state,
            parent_session_id="conv_parent",
            parent_thread_id="thread_parent",
            event=_fork_started(thread_id="other", forked_from="thread_elsewhere"),
        )
        is None
    )
    ensure.assert_not_awaited()


@pytest.mark.asyncio
async def test_register_side_fork_child_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    ensure = _ensure_stub("conv_side")
    monkeypatch.setattr(fwd, "_ensure_child_session", ensure)
    state = fwd._CodexForwarderState()
    state.note_child_thread("thread_side", "conv_side")  # already known

    result = await side_chat.register_side_fork_child(
        object(),
        forwarder_state=state,
        parent_session_id="conv_parent",
        parent_thread_id="thread_parent",
        event=_fork_started(thread_id="thread_side", forked_from="thread_parent"),
    )

    assert result == "conv_side"
    ensure.assert_not_awaited()  # no duplicate registration


@pytest.mark.asyncio
async def test_register_child_session_forwards_the_display_nickname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``agent_nickname`` must reach the server, else the child keeps a UUID title."""
    posted: dict[str, Any] = {}

    async def _post(_client, session_id: str, *, event_type: str, data: _JsonObject):
        posted.update({"session_id": session_id, "event_type": event_type, "data": data})

        class _R:
            status_code = 200

            @staticmethod
            def json() -> dict[str, str]:
                return {"child_session_id": "conv_side"}

        return _R()

    monkeypatch.setattr(fwd, "_post_session_event", _post)

    child = await fwd._register_child_session(
        object(),
        parent_session_id="conv_parent",
        parent_thread_id="thread_parent",
        child_thread_id="thread_side",
        item={"agent_nickname": "Side chat"},
    )

    assert child == "conv_side"
    assert posted["data"]["agent_nickname"] == "Side chat"
    assert posted["data"]["thread_id"] == "thread_side"


# --------------------------------------------------------------------------- #
# forwarder routing / isolation (real forwarder code) — once the child is mapped
# --------------------------------------------------------------------------- #
def _item_event(thread_id: str) -> _JsonObject:
    return {"threadId": thread_id, "item": {"id": "item_1", "type": "agentMessage"}}


def test_registered_side_fork_routes_to_child_and_parent_stays_isolated() -> None:
    state = fwd._CodexForwarderState()
    state.note_child_thread("thread_side", "conv_side")

    def resolve(thread_id: str) -> tuple[str | None, bool]:
        return fwd._resolve_event_session(
            _item_event(thread_id),
            "item/completed",
            "thread_parent",
            state,
            fallback_session_id="conv_parent",
        )

    assert resolve("thread_side") == ("conv_side", True)  # side -> child
    assert resolve("thread_parent") == ("conv_parent", False)  # main untouched
    assert resolve("thread_rando") == (None, False)  # unknown thread dropped, no leak


def test_side_fork_thread_started_is_rotation_ignored() -> None:
    # the fork is ephemeral, so the rotation path skips it -> can't hijack main
    event = _fork_started(thread_id="thread_side", forked_from="thread_parent")
    assert fwd._thread_started_is_ephemeral(event) is True


# --------------------------------------------------------------------------- #
# bridge-dir handoff: the executor records, the forwarder forks
# --------------------------------------------------------------------------- #
def test_side_chat_requests_peek_does_not_consume_until_discarded(tmp_path: Path) -> None:
    assert side_chat.peek_side_chat_requests(tmp_path) == []  # nothing pending

    side_chat.request_side_chat(tmp_path, "why is the sky blue?")
    side_chat.request_side_chat(tmp_path, "and the sea?")

    pending = side_chat.peek_side_chat_requests(tmp_path)
    assert sorted(r.question for r in pending) == ["and the sea?", "why is the sky blue?"]
    # Peek does NOT consume — a re-peek still sees them, so a fork failure or a
    # not-yet-ready parent thread can retry instead of losing the question.
    assert len(side_chat.peek_side_chat_requests(tmp_path)) == 2

    for request in pending:
        side_chat.discard_side_chat_request(request.path)
    # Discarded, so a later drain never re-forks them.
    assert side_chat.peek_side_chat_requests(tmp_path) == []


def test_peek_side_chat_requests_discards_corrupt_payloads(tmp_path: Path) -> None:
    (tmp_path / "side_chat_requests").mkdir()
    (tmp_path / "side_chat_requests" / "bad.json").write_text("{not json", encoding="utf-8")
    side_chat.request_side_chat(tmp_path, "good one")

    pending = side_chat.peek_side_chat_requests(tmp_path)
    assert [r.question for r in pending] == ["good one"]
    # A corrupt request can never fork, so it is removed rather than retried forever.
    assert not (tmp_path / "side_chat_requests" / "bad.json").exists()


@pytest.mark.asyncio
async def test_drive_side_chat_requests_forks_on_the_forwarder_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The drain loop must fork on the connection it was handed.

    A fork made on the executor's client streams its answer into a connection
    that closes immediately, which is why this runs on the forwarder's client.
    """
    client = _FakeCodexClient(
        {
            "thread/fork": {"result": {"thread": {"id": "thread_side"}}},
            "turn/start": {"result": {"turn": {"id": "turn_1"}}},
        }
    )
    side_chat.request_side_chat(tmp_path, "what does this repo do?")

    async def _stop(_seconds: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(fwd, "_sleep", _stop)
    with pytest.raises(asyncio.CancelledError):
        await fwd._drive_side_chat_requests(
            client,
            ap_client=AsyncMock(),
            bridge_dir=tmp_path,
            target=SimpleNamespace(thread_id="thread_parent", session_id="conv_parent"),
        )

    assert [m for m, _ in client.calls] == ["thread/fork", "turn/start"]
    assert client.calls[0][1]["threadId"] == "thread_parent"
    assert client.calls[0][1]["ephemeral"] is True
    assert client.calls[1][1]["threadId"] == "thread_side"  # first turn on the fork
    assert side_chat.peek_side_chat_requests(tmp_path) == []  # request consumed after forking


@pytest.mark.asyncio
async def test_drive_side_chat_requests_keeps_the_request_until_the_parent_thread_is_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression for the "dropped when the thread isn't ready yet" bug: the
    # request must SURVIVE (not be consumed) so a later cycle forks it.
    client = _FakeCodexClient({})
    side_chat.request_side_chat(tmp_path, "q")

    async def _stop(_seconds: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(fwd, "_sleep", _stop)
    with pytest.raises(asyncio.CancelledError):
        await fwd._drive_side_chat_requests(
            client,
            ap_client=AsyncMock(),
            bridge_dir=tmp_path,
            target=SimpleNamespace(thread_id=None, session_id="conv_parent"),
        )

    assert client.calls == []  # no thread to fork from yet
    # The question is NOT lost — it waits for a cycle where the thread is ready.
    assert [r.question for r in side_chat.peek_side_chat_requests(tmp_path)] == ["q"]


@pytest.mark.asyncio
async def test_drive_side_chat_requests_surfaces_a_notice_and_discards_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A fork that fails must not vanish silently: surface a visible notice, then
    # discard the request so the same failure isn't retried forever.
    client = _FakeCodexClient({"thread/fork": {"result": {}}})  # no thread id -> failure
    side_chat.request_side_chat(tmp_path, "doomed")

    notices: list[tuple[str, str]] = []

    async def _fake_notice(_ap: object, session_id: str, key: str) -> None:
        notices.append((session_id, key))

    async def _stop(_seconds: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(fwd, "_post_side_chat_failed_notice", _fake_notice)
    monkeypatch.setattr(fwd, "_sleep", _stop)
    with pytest.raises(asyncio.CancelledError):
        await fwd._drive_side_chat_requests(
            client,
            ap_client=AsyncMock(),
            bridge_dir=tmp_path,
            target=SimpleNamespace(thread_id="thread_parent", session_id="conv_parent"),
        )

    assert notices and notices[0][0] == "conv_parent"  # user-visible failure notice
    assert side_chat.peek_side_chat_requests(tmp_path) == []  # discarded, not retried forever


# --------------------------------------------------------------------------- #
# parent-wake suppression for codex sub-agents
# --------------------------------------------------------------------------- #
def test_codex_native_subagent_wrapper_is_recognized() -> None:
    """A codex sub-agent must not wake the parent with an inbox notice.

    Its result is consumed inside the parent's own app-server thread tree, so a
    wake would inject "[System: sub-agent … finished …]" into the chat the user
    is reading.
    """
    from omnigent.runner.app import is_codex_native_subagent_wrapper

    assert is_codex_native_subagent_wrapper("codex-native-ui-subagent") is True
    # other harnesses' sub-agents still deliver through the inbox
    assert is_codex_native_subagent_wrapper("claude-code-native-ui-subagent") is False
    assert is_codex_native_subagent_wrapper("something-else") is False
    assert is_codex_native_subagent_wrapper(None) is False

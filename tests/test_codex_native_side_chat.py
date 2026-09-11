"""Unit tests for Codex ``/side`` ephemeral side-chat support.

Each test verifies one piece of the mechanism against the real forwarder code
and the real ``side_chat`` helpers, with the app-server and Omnigent HTTP calls
faked. No live Codex is needed.
"""

from __future__ import annotations

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
@pytest.mark.asyncio
async def test_register_side_fork_child_registers_matching_fork(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    register = AsyncMock(return_value="conv_side")
    monkeypatch.setattr(fwd, "_register_child_session", register)
    state = fwd._CodexForwarderState()

    child_session = await side_chat.register_side_fork_child(
        object(),  # ap_client unused: registration mocked
        forwarder_state=state,
        parent_session_id="conv_parent",
        parent_thread_id="thread_parent",
        event=_fork_started(thread_id="thread_side", forked_from="thread_parent"),
    )

    assert child_session == "conv_side"
    assert state.session_for_child_thread("thread_side") == "conv_side"
    kwargs = register.await_args.kwargs
    assert kwargs["parent_session_id"] == "conv_parent"
    assert kwargs["child_thread_id"] == "thread_side"
    assert kwargs["item"]["sub_agent_name"] == side_chat.SIDE_CHAT_SUBAGENT_NAME


@pytest.mark.asyncio
async def test_register_side_fork_child_ignores_system_ephemeral_and_wrong_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    register = AsyncMock(return_value="conv_side")
    monkeypatch.setattr(fwd, "_register_child_session", register)
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
    register.assert_not_awaited()


@pytest.mark.asyncio
async def test_register_side_fork_child_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    register = AsyncMock(return_value="conv_side")
    monkeypatch.setattr(fwd, "_register_child_session", register)
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
    register.assert_not_awaited()  # no duplicate registration


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

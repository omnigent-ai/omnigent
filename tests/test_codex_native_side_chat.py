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


# --------------------------------------------------------------------------- #
# fork_ephemeral_side_thread
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
    # reference-only boundary instructions ride along so inherited history is inert
    assert "developerInstructions" in params
    assert "side conversation" in params["developerInstructions"]


@pytest.mark.asyncio
async def test_fork_returns_none_when_response_has_no_thread_id() -> None:
    client = _FakeCodexClient({"thread/fork": {"result": {}}})
    assert await side_chat.fork_ephemeral_side_thread(client, "thread_parent") is None


# --------------------------------------------------------------------------- #
# submit_side_turn
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_submit_side_turn_targets_child_thread() -> None:
    client = _FakeCodexClient({"turn/start": {"result": {"turn": {"id": "turn_1"}}}})

    turn_id = await side_chat.submit_side_turn(client, "thread_side", "why is the sky blue?")

    assert turn_id == "turn_1"
    method, params = client.calls[0]
    assert method == "turn/start"
    assert params["threadId"] == "thread_side"
    assert params["input"] == [{"type": "text", "text": "why is the sky blue?"}]
    assert "collaborationMode" not in params  # omitted when not provided


@pytest.mark.asyncio
async def test_submit_side_turn_includes_collaboration_mode_when_given() -> None:
    client = _FakeCodexClient({"turn/start": {"result": {"turn": {"id": "t"}}}})
    mode = {"mode": "default", "settings": {}}

    await side_chat.submit_side_turn(client, "thread_side", "hi", collaboration_mode=mode)

    _, params = client.calls[0]
    assert params["collaborationMode"] == mode


# --------------------------------------------------------------------------- #
# start_side_chat (fork -> register -> first turn)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_start_side_chat_forks_registers_and_submits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    register = AsyncMock(return_value="conv_side")
    monkeypatch.setattr(fwd, "_register_child_session", register)

    client = _FakeCodexClient(
        {
            "thread/fork": {"result": {"thread": {"id": "thread_side"}}},
            "turn/start": {"result": {"turn": {"id": "turn_1"}}},
        }
    )
    state = fwd._CodexForwarderState()

    result = await side_chat.start_side_chat(
        ap_client=object(),  # unused: registration is mocked
        codex_client=client,
        parent_session_id="conv_parent",
        parent_thread_id="thread_parent",
        question="what does this repo do?",
        forwarder_state=state,
    )

    assert result == ("conv_side", "thread_side")
    # child registered with the right parent/child linkage
    register.assert_awaited_once()
    kwargs = register.await_args.kwargs
    assert kwargs["parent_session_id"] == "conv_parent"
    assert kwargs["parent_thread_id"] == "thread_parent"
    assert kwargs["child_thread_id"] == "thread_side"
    # routing map updated so the fork's events reach the child session
    assert state.session_for_child_thread("thread_side") == "conv_side"
    # order: fork first, then the first turn on the child thread
    methods = [m for m, _ in client.calls]
    assert methods == ["thread/fork", "turn/start"]
    assert client.calls[1][1]["threadId"] == "thread_side"


@pytest.mark.asyncio
async def test_start_side_chat_aborts_when_fork_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    register = AsyncMock(return_value="conv_side")
    monkeypatch.setattr(fwd, "_register_child_session", register)
    client = _FakeCodexClient({"thread/fork": {"result": {}}})  # no thread id

    result = await side_chat.start_side_chat(
        ap_client=object(),
        codex_client=client,
        parent_session_id="conv_parent",
        parent_thread_id="thread_parent",
        question="q",
        forwarder_state=fwd._CodexForwarderState(),
    )

    assert result is None
    register.assert_not_awaited()
    assert [m for m, _ in client.calls] == ["thread/fork"]  # no turn submitted


@pytest.mark.asyncio
async def test_start_side_chat_aborts_when_registration_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fwd, "_register_child_session", AsyncMock(return_value=None))
    client = _FakeCodexClient({"thread/fork": {"result": {"thread": {"id": "thread_side"}}}})
    state = fwd._CodexForwarderState()

    result = await side_chat.start_side_chat(
        ap_client=object(),
        codex_client=client,
        parent_session_id="conv_parent",
        parent_thread_id="thread_parent",
        question="q",
        forwarder_state=state,
    )

    assert result is None
    assert state.session_for_child_thread("thread_side") is None
    assert [m for m, _ in client.calls] == ["thread/fork"]  # no turn on an unregistered child


# --------------------------------------------------------------------------- #
# forwarder routing / isolation / rotation-safety (real forwarder code)
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
    event = {
        "method": "thread/started",
        "params": {
            "thread": {"id": "thread_side", "ephemeral": True, "forkedFromId": "thread_parent"}
        },
    }
    assert fwd._thread_started_is_ephemeral(event) is True


def test_is_omnigent_side_fork_discriminates_fork_from_system_ephemeral() -> None:
    fork = {
        "method": "thread/started",
        "params": {"thread": {"id": "s", "ephemeral": True, "forkedFromId": "thread_parent"}},
    }
    system = {
        "method": "thread/started",
        "params": {"thread": {"id": "sys", "ephemeral": True, "threadSource": "system"}},
    }
    assert side_chat.is_omnigent_side_fork(fork) is True
    assert side_chat.is_omnigent_side_fork(system) is False
    assert side_chat.is_omnigent_side_fork({"method": "turn/started", "params": {}}) is False

"""Attribution of the runner relay's ``TurnEndEvent`` telemetry.

The relay is often started outside a request (runner reconnect, child-session
recovery), so the ambient user it inherits is frequently unbound. Turn ends
must still carry the session owner's anonymous id, or usage dashboards count
the server's installation as an extra user.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from types import TracebackType
from typing import Any

import pytest

from omnigent.debug_logging import USER_ID_ENV_VAR, current_user_id_scope
from omnigent.server.auth import LEVEL_OWNER
from omnigent.server.routes._sessions import orchestration
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.telemetry.anon import anon_user_id
from omnigent.telemetry.events import TurnEndEvent

_INSTALLATION_ID = "6f1c2d3e-4b5a-4c6d-8e7f-9a0b1c2d3e4f"


def _turn(response_id: str) -> list[dict[str, Any]]:
    """
    Build one relayed turn's SSE events.

    :param response_id: The turn's response id, e.g. ``"turn-1"``.
    :returns: ``response.in_progress`` followed by ``response.completed``.
    """
    return [
        {"type": "response.in_progress", "response": {"id": response_id, "model": "test"}},
        {"type": "response.completed", "response": {"id": response_id}},
    ]


class _ScriptedStream:
    """
    Async context manager mimicking ``httpx.AsyncClient.stream``.

    :param events: SSE payload dicts replayed after the ready heartbeat.
    """

    def __init__(self, events: list[dict[str, Any]]) -> None:
        """
        Initialize the scripted stream.

        :param events: SSE payload dicts to replay, in order.
        """
        self._events = events

    async def __aenter__(self) -> _ScriptedStream:
        """
        Enter the stream context.

        :returns: This fake response.
        """
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """
        Exit the stream context.

        :param exc_type: Exception type, if any.
        :param exc: Exception instance, if any.
        :param traceback: Exception traceback, if any.
        """
        del exc_type, exc, traceback

    def raise_for_status(self) -> None:
        """The scripted stream is always a successful response."""

    async def aiter_text(self) -> AsyncIterator[str]:
        """
        Yield the heartbeat, the scripted events, then ``[DONE]``.

        :yields: SSE data lines as the runner sends them.
        """
        yield 'data: {"type": "session.heartbeat"}\n\n'
        for event in self._events:
            yield f"data: {json.dumps(event)}\n\n"
        yield "data: [DONE]\n\n"


class _ScriptedRunner:
    """
    Fake runner client whose session stream replays fixed events.

    :param events: SSE payload dicts to replay.
    """

    def __init__(self, events: list[dict[str, Any]]) -> None:
        """
        Initialize the fake runner.

        :param events: SSE payload dicts to replay.
        """
        self._events = events

    def stream(self, method: str, path: str, *, timeout: Any) -> _ScriptedStream:
        """
        Return the scripted stream.

        :param method: HTTP method, e.g. ``"GET"``.
        :param path: Request path, e.g. ``"/v1/sessions/conv_abc/stream"``.
        :param timeout: Timeout passed by the relay.
        :returns: The scripted stream.
        """
        del method, path, timeout
        return _ScriptedStream(self._events)


@pytest.fixture()
def turn_ends(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[TurnEndEvent]]:
    """
    Capture emitted ``TurnEndEvent`` records with telemetry enabled.

    Pytest itself opts out of telemetry, so force it on and pin the
    installation id that salts the anonymous user hash.

    :param monkeypatch: Pytest monkeypatch fixture.
    :yields: The captured turn-end events, in emit order.
    """
    from omnigent.telemetry import installation_id

    events: list[TurnEndEvent] = []

    def _capture(event: object) -> None:
        if isinstance(event, TurnEndEvent):
            events.append(event)

    monkeypatch.delenv(USER_ID_ENV_VAR, raising=False)
    monkeypatch.setattr(orchestration, "_tel_emit", _capture)
    monkeypatch.setattr(orchestration, "_tel_is_disabled", lambda: False)
    monkeypatch.setattr(orchestration, "_get_installation_id", lambda: _INSTALLATION_ID)
    monkeypatch.setattr(installation_id, "get_installation_id", lambda: _INSTALLATION_ID)
    yield events


def _owned_session(db_uri: str, owner: str) -> tuple[SqlAlchemyConversationStore, str]:
    """
    Create a session with an owner grant.

    :param db_uri: Test database URI.
    :param owner: The owning user id, e.g. ``"alice@example.com"``.
    :returns: The conversation store and the new session id.
    """
    store = SqlAlchemyConversationStore(db_uri)
    session_id = store.create_conversation().id
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.ensure_user(owner)
    permissions.grant(owner, session_id, LEVEL_OWNER)
    return store, session_id


@pytest.mark.asyncio
async def test_turn_end_without_request_user_is_attributed_to_owner(
    db_uri: str, turn_ends: list[TurnEndEvent], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, session_id = _owned_session(db_uri, "alice@example.com")
    lookups: list[str] = []
    get_owner = store.get_session_owner

    def _counting_get_owner(conversation_id: str, *, owner_only: bool = False) -> str | None:
        lookups.append(conversation_id)
        return get_owner(conversation_id, owner_only=owner_only)

    monkeypatch.setattr(store, "get_session_owner", _counting_get_owner)

    with current_user_id_scope(None):
        await orchestration._relay_runner_stream_once(
            session_id,
            _ScriptedRunner(_turn("turn-1") + _turn("turn-2")),  # type: ignore[arg-type]
            store,
        )

    expected = anon_user_id("alice@example.com", _INSTALLATION_ID)
    assert [e.anon_user_id for e in turn_ends] == [expected, expected]
    # One owner read per relay attempt, not per turn end.
    assert lookups == [session_id]


@pytest.mark.asyncio
async def test_subagent_turn_end_is_attributed_to_root_owner(
    db_uri: str, turn_ends: list[TurnEndEvent]
) -> None:
    store, parent_id = _owned_session(db_uri, "alice@example.com")
    child = store.create_conversation(parent_conversation_id=parent_id, title="researcher")
    assert store.get_session_owner(child.id) is None

    with current_user_id_scope(None):
        await orchestration._relay_runner_stream_once(
            child.id,
            _ScriptedRunner(_turn("turn-1")),  # type: ignore[arg-type]
            store,
        )

    assert [e.anon_user_id for e in turn_ends] == [
        anon_user_id("alice@example.com", _INSTALLATION_ID)
    ]


@pytest.mark.asyncio
async def test_turn_end_keeps_the_bound_request_user(
    db_uri: str, turn_ends: list[TurnEndEvent]
) -> None:
    store, session_id = _owned_session(db_uri, "alice@example.com")

    with current_user_id_scope("bob@example.com"):
        await orchestration._relay_runner_stream_once(
            session_id,
            _ScriptedRunner(_turn("turn-1")),  # type: ignore[arg-type]
            store,
        )

    assert [e.anon_user_id for e in turn_ends] == [
        anon_user_id("bob@example.com", _INSTALLATION_ID)
    ]


@pytest.mark.asyncio
async def test_turn_end_without_any_owner_stays_unattributed(
    db_uri: str, turn_ends: list[TurnEndEvent]
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    session_id = store.create_conversation().id

    with current_user_id_scope(None):
        await orchestration._relay_runner_stream_once(
            session_id,
            _ScriptedRunner(_turn("turn-1")),  # type: ignore[arg-type]
            store,
        )

    assert [e.anon_user_id for e in turn_ends] == [None]

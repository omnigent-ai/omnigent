"""Tests for server-owned runner session initialization coordination."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from omnigent.db.utils import generate_agent_id
from omnigent.entities import Conversation
from omnigent.runner.session_init_protocol import (
    build_runner_session_init_payload,
    parse_runner_session_init_envelope,
)
from omnigent.server.runner_session_init import RunnerSessionInitializer
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store import (
    FORK_CARRY_HISTORY_LABEL_KEY,
    FORK_SOURCE_EXTERNAL_SESSION_LABEL_KEY,
    FORK_SOURCE_LABEL_KEY,
)
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


class _Registry:
    def __init__(self) -> None:
        self.connection: object | None = object()

    def get(self, _runner_id: str) -> object | None:
        return self.connection


class _Client:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.status_code = 201

    async def post(self, _path: str, **kwargs: Any) -> httpx.Response:
        self.calls.append(kwargs["json"])
        self.entered.set()
        await self.release.wait()
        return httpx.Response(self.status_code, json={"status": "initialized"})


def _conversation() -> Conversation:
    return Conversation(
        id="conv_init",
        created_at=10,
        updated_at=11,
        root_conversation_id="conv_init",
        agent_id="agent_init",
        runner_id="runner_init",
        workspace="/tmp/workspace",
        labels={"example": "value"},
    )


@pytest.mark.asyncio
async def test_initializer_shares_result_for_one_tunnel_generation() -> None:
    registry = _Registry()
    client = _Client()
    initializer = RunnerSessionInitializer(  # type: ignore[arg-type]
        registry,
        server_version="0.6.0.dev0",
    )
    conversation = _conversation()

    first = asyncio.create_task(initializer.initialize(conversation, client, timeout=10))  # type: ignore[arg-type]
    await client.entered.wait()
    second = asyncio.create_task(initializer.initialize(conversation, client, timeout=10))  # type: ignore[arg-type]
    await asyncio.sleep(0)
    client.release.set()
    first_response, second_response = await asyncio.gather(first, second)

    assert first_response is second_response
    assert len(client.calls) == 1
    assert client.calls[0]["session_init"]["snapshot"]["workspace"] == "/tmp/workspace"

    cached = await initializer.initialize(conversation, client, timeout=10)  # type: ignore[arg-type]
    assert cached is first_response
    assert len(client.calls) == 1

    initializer.invalidate_runner("runner_init")
    await initializer.initialize(conversation, client, timeout=10)  # type: ignore[arg-type]
    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_initializer_evicts_rejected_result_for_retry() -> None:
    registry = _Registry()
    client = _Client()
    client.release.set()
    client.status_code = 503
    initializer = RunnerSessionInitializer(  # type: ignore[arg-type]
        registry,
        server_version="0.6.0.dev0",
    )
    conversation = _conversation()

    first = await initializer.initialize(conversation, client, timeout=10)  # type: ignore[arg-type]
    second = await initializer.initialize(conversation, client, timeout=10)  # type: ignore[arg-type]

    assert first.status_code == second.status_code == 503
    assert len(client.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "expected_ready"),
    [
        ({"session_init_protocol_version": 2, "terminal_ready": True}, True),
        ({}, False),
    ],
    ids=["current-runner", "legacy-runner"],
)
async def test_session_init_readiness_is_explicit_and_backward_compatible(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any],
    expected_ready: bool,
) -> None:
    """Only a current runner response suppresses the terminal ensure."""
    from omnigent.server.routes import sessions as sessions_routes

    async def _noop_recovered(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(sessions_routes, "_publish_runner_recovered_status", _noop_recovered)

    class _Initializer:
        async def initialize(self, *_args: Any, **_kwargs: Any) -> httpx.Response:
            return httpx.Response(
                201,
                json=payload,
                request=httpx.Request("POST", "http://runner/v1/sessions"),
            )

    ready = await sessions_routes._ensure_runner_session_initialized(
        "conv_init",
        _conversation(),
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        initializer=_Initializer(),  # type: ignore[arg-type]
    )

    assert ready is expected_ready


def test_reconnect_init_envelope_carries_fork_history_directives(db_uri: str) -> None:
    """A forked native session's fork directives survive to the runner envelope.

    End-to-end regression guard for the exact seam that dropped a forked
    claude-native session's history: the runner reconnect path
    (``_on_runner_connect``) sources its conversations from
    ``list_conversations_by_runner_id`` and hands each straight to
    ``build_runner_session_init_payload``, which projects
    ``conversation.labels`` into the init envelope the runner reads to decide
    whether to clone/rebuild the vendor transcript. When that store lookup
    returned label-less conversations, the envelope shipped no ``omnigent.fork.*``
    directives, so the runner skipped its clone/rebuild branch and launched the
    TUI fresh -- history lost -- even though the fork copied the history into the
    store.

    This drives the real store (fork included), not a hand-built envelope, so it
    fails if any layer between the by-runner-id lookup and the envelope stops
    carrying labels. The label-to-launch-metadata projection is covered
    separately by ``test_claude_launch_metadata_envelope_never_calls_server``.
    """
    agent_store = SqlAlchemyAgentStore(db_uri)
    conversation_store = SqlAlchemyConversationStore(db_uri)

    # A claude-native SOURCE with a captured native session id and a bound
    # workspace -- the two preconditions fork_conversation needs to stamp the
    # source-transcript directive.
    agent = agent_store.create(generate_agent_id(), "claude-native-ui", "bundle/loc")
    source = conversation_store.create_conversation(agent_id=agent.id, workspace="/tmp/ws")
    conversation_store.set_external_session_id(source.id, "src-claude-sid")

    # Fork it the way the route does for a same-family native target: carry
    # history and resume the source's native transcript.
    fork = conversation_store.fork_conversation(
        source.id,
        carry_history_into_native=True,
        resume_source_native_session=True,
    )
    # Bind the fork to a runner so the reconnect lookup returns it.
    assert conversation_store.set_runner_id(fork.id, "runner_fork")

    # The reconnect path: by-runner-id lookup -> init payload.
    bound = conversation_store.list_conversations_by_runner_id("runner_fork")
    assert [c.id for c in bound] == [fork.id]

    payload = build_runner_session_init_payload(bound[0], server_version="0.6.0.dev0")
    envelope = parse_runner_session_init_envelope(payload)
    assert envelope is not None

    # The directives that select the runner's clone/rebuild branch must be
    # present in the envelope the runner actually reads.
    labels = envelope.snapshot.labels
    assert labels.get(FORK_CARRY_HISTORY_LABEL_KEY) == "1"
    assert labels.get(FORK_SOURCE_EXTERNAL_SESSION_LABEL_KEY) == "src-claude-sid"
    assert labels.get(FORK_SOURCE_LABEL_KEY) == source.id

    # And the runner's own projection reads them as launch directives -- the
    # boolean the clone/rebuild branch gates on.
    from omnigent.runner.app import _claude_launch_metadata_from_envelope

    metadata = _claude_launch_metadata_from_envelope(envelope)
    assert metadata.fork_carry_history is True
    assert metadata.fork_source_external_id == "src-claude-sid"

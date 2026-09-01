"""Automatic titles on the native-terminal message path.

A native-terminal session takes the single-writer bypass: the web message
is injected into the pane and only round-trips back through the transcript
forwarder, so nothing persists it in-request. These tests pin the two
consequences that used to leave such a session showing its harness
fallback ("Claude Code" / "Codex") instead of a name:

* the dispatch seeds the deterministic title from the typed prompt, which
  is also the compare-and-swap baseline the background titler waits for;
* the seed never replaces a title a human already chose.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from omnigent.server.routes._sessions import orchestration as orchestration_module
from omnigent.server.schemas import SessionEventInput
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio


async def _native_session(
    client: httpx.AsyncClient,
    db_uri: str,
    *,
    agent_name: str,
    title: str | None = None,
) -> tuple[Any, SqlAlchemyConversationStore]:
    """Create a top-level claude-native session, optionally already titled."""
    from omnigent.harness_plugins import CLAUDE_NATIVE_CODING_AGENT

    agent = await create_test_agent(client, name=agent_name)
    conv_store = SqlAlchemyConversationStore(db_uri)
    conv = conv_store.create_conversation(
        kind="default",
        agent_id=agent["id"],
        title=title,
    )
    conv_store.set_labels(
        conv.id,
        {
            "omnigent.ui": "terminal",
            "omnigent.wrapper": CLAUDE_NATIVE_CODING_AGENT.wrapper_label,
        },
    )
    refreshed = conv_store.get_conversation(conv.id)
    assert refreshed is not None
    return refreshed, conv_store


async def _dispatch_web_message(
    conv: Any,
    conv_store: SqlAlchemyConversationStore,
    *,
    text: str,
) -> list[dict[str, Any]]:
    """Send one web message into the pane, returning what reached the runner."""
    forwarded: list[dict[str, Any]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/events"):
            forwarded.append(json.loads(request.content))
        return httpx.Response(202, json={"queued": True})

    body = SessionEventInput(
        type="message",
        data={"role": "user", "content": [{"type": "input_text", "text": text}]},
    )
    async with httpx.AsyncClient(
        base_url="http://runner.test", transport=httpx.MockTransport(_handler)
    ) as runner_client:
        await orchestration_module._dispatch_session_event_to_runner_impl(
            conv.id,
            conv,
            body,
            conv_store,
            runner_client,
            agent_name="native",
            file_store=None,
            artifact_store=None,
            native_terminal_ready=True,
        )
    return forwarded


async def test_native_web_message_seeds_the_session_title(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """The first prompt names the session, without waiting for the transcript.

    The bypass persists no item, so the row used to stay untitled for the whole
    first turn and the sidebar rendered "Claude Code".
    """
    conv, conv_store = await _native_session(client, db_uri, agent_name="native-title-seed")
    assert conv.title is None

    forwarded = await _dispatch_web_message(conv, conv_store, text="refactor the auth module")

    assert len(forwarded) == 1
    refreshed = conv_store.get_conversation(conv.id)
    assert refreshed is not None
    assert refreshed.title == "refactor the auth module"


async def test_native_web_message_does_not_rename_a_user_titled_session(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A session a human named keeps that name."""
    conv, conv_store = await _native_session(
        client,
        db_uri,
        agent_name="native-title-explicit",
        title="Auth work",
    )

    await _dispatch_web_message(conv, conv_store, text="refactor the auth module")

    refreshed = conv_store.get_conversation(conv.id)
    assert refreshed is not None
    assert refreshed.title == "Auth work"


async def test_seed_loses_to_a_rename_that_lands_first(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A UI rename racing the seed wins.

    ``conv`` is read at the route boundary, so it still reads untitled here;
    the compare-and-swap is what keeps the human's name.
    """
    conv, conv_store = await _native_session(client, db_uri, agent_name="native-title-race")
    assert conv.title is None

    # The rename the user typed after the request was accepted.
    conv_store.update_conversation(conv.id, title="Auth work")

    await _dispatch_web_message(conv, conv_store, text="refactor the auth module")

    refreshed = conv_store.get_conversation(conv.id)
    assert refreshed is not None
    assert refreshed.title == "Auth work"

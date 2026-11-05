"""Side chats release their own resources on a shared runner."""

from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from omnigent.db.utils import generate_agent_id
from omnigent.entities import NewConversationItem
from omnigent.entities.conversation import MessageData
from omnigent.runtime import session_stream
from omnigent.server.routes import sessions
from omnigent.server.routes.sessions import routes_events
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store import SIDE_CHAT_CLOSED_LABEL_KEY, SIDE_CHAT_LABEL_KEY
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.util.session_lifecycle import CLOSED_LABEL_KEY


@pytest.mark.parametrize("close_tab", [True, False])
async def test_close_side_chat_preserves_shared_runner_and_transcript(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    close_tab: bool,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    agent_id = generate_agent_id()
    SqlAlchemyAgentStore(db_uri).create(agent_id, name="test", bundle_location="test:///bundle")
    parent = store.create_conversation(agent_id=agent_id)
    side = store.fork_conversation(parent.id, extra_labels={SIDE_CHAT_LABEL_KEY: "1"})
    sibling = store.fork_conversation(parent.id, extra_labels={SIDE_CHAT_LABEL_KEY: "1"})
    for row in (parent, side, sibling):
        store.set_runner_id(row.id, "runner-shared")
    store.set_host_id(parent.id, "1234567890abcdef1234567890abcdef", workspace="/workspace")
    transcript = store.append(
        side.id,
        [
            NewConversationItem(
                type="message",
                response_id="saved-turn",
                data=MessageData(
                    role="assistant",
                    agent="test",
                    content=[{"type": "output_text", "text": "Saved"}],
                ),
            )
        ],
    )
    requests: list[tuple[str, str]] = []
    closed_before_stop: list[bool] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.method == "POST":
            saved = store.get_conversation(side.id)
            closed_before_stop.append(
                SIDE_CHAT_CLOSED_LABEL_KEY in (saved.labels if saved else {})
            )
        return httpx.Response(204)

    host_stop = AsyncMock(return_value=True)
    monkeypatch.setattr(routes_events, "_stop_host_runner_intentionally", host_stop)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://runner"
    ) as runner_client:
        monkeypatch.setattr(sessions, "_get_runner_client", AsyncMock(return_value=runner_client))
        # Closing the tab says so; any other stop (the CLI, an agent tool) doesn't.
        response = await client.post(
            f"/v1/sessions/{side.id}/events",
            json={"type": "stop_session", "data": {"close_tab": True} if close_tab else {}},
        )
        assert response.status_code == 202, response.text
        assert requests == [
            ("POST", f"/v1/sessions/{side.id}/events"),
            ("DELETE", f"/v1/sessions/{side.id}"),
        ]
        host_stop.assert_not_awaited()
        assert store.list_items(side.id).data == transcript
        for row in (parent, side, sibling):
            saved = store.get_conversation(row.id)
            assert saved is not None and saved.runner_id == "runner-shared"
            # A closed tab is recorded for the user's other devices, without sealing.
            assert (SIDE_CHAT_CLOSED_LABEL_KEY in saved.labels) == (close_tab and row is side)
            assert CLOSED_LABEL_KEY not in saved.labels
        # Recorded before the stop reaches the runner, so another device never
        # drains a queued follow-up into the chat on the stop's idle edge.
        assert closed_before_stop[:1] == [close_tab]

        response = await client.post(
            f"/v1/sessions/{parent.id}/events", json={"type": "stop_session"}
        )
        assert response.status_code == 202, response.text
        assert requests[-1] == ("POST", f"/v1/sessions/{parent.id}/events")
        host_stop.assert_awaited_once()
        assert host_stop.await_args.args[:3] == (
            parent.id,
            "1234567890abcdef1234567890abcdef",
            "runner-shared",
        )


@pytest.mark.parametrize("failure", ["stop", "cleanup", "disconnect"])
async def test_side_chat_close_surfaces_runner_failure(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    side = store.create_conversation(labels={SIDE_CHAT_LABEL_KEY: "1"})
    store.set_runner_id(side.id, "runner-shared")
    methods: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method == "POST":
            return httpx.Response(503 if failure == "stop" else 204)
        if request.method == "GET":
            return httpx.Response(
                200,
                text='data: {"type":"response.output_text.delta","delta":"Trailing output"}\n\n'
                'data: {"type":"response.incomplete","response":{"id":"stopped-turn"}}\n\n'
                "data: [DONE]\n\n",
            )
        if failure == "disconnect":
            raise httpx.ConnectError("runner disconnected", request=request)
        return httpx.Response(500)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://runner"
    ) as runner_client:
        monkeypatch.setattr(sessions, "_get_runner_client", AsyncMock(return_value=runner_client))
        response = await client.post(
            f"/v1/sessions/{side.id}/events",
            json={"type": "stop_session", "data": {"close_tab": True}},
        )

        assert response.status_code == 503, response.text
        assert methods == (["POST"] if failure == "stop" else ["POST", "DELETE"])
        publish = Mock(wraps=session_stream.publish)
        monkeypatch.setattr(session_stream, "publish", publish)
        await sessions._relay_runner_stream(side.id, runner_client, store)

    deltas = [
        call.args[1]["delta"]
        for call in publish.call_args_list
        if call.args[0] == side.id and call.args[1].get("type") == "response.output_text.delta"
    ]
    assert deltas == (["Trailing output"] if failure == "stop" else [])
    messages = [item for item in store.list_items(side.id).data if item.type == "message"]
    assert bool(messages) == (failure == "stop")
    assert side.id not in routes_events._interrupt_fenced_sessions
    saved = store.get_conversation(side.id)
    assert saved is not None and SIDE_CHAT_CLOSED_LABEL_KEY not in saved.labels


async def test_closing_an_ended_codex_side_chat_tab_drops_it_everywhere(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    """A Codex side chat sealed when its fork died still closes its tab everywhere."""
    store = SqlAlchemyConversationStore(db_uri)
    agent_id = generate_agent_id()
    SqlAlchemyAgentStore(db_uri).create(agent_id, name="test", bundle_location="test:///bundle")
    parent = store.create_conversation(agent_id=agent_id)
    side = store.create_conversation(
        kind="sub_agent",
        parent_conversation_id=parent.id,
        agent_id=agent_id,
        labels={
            "omnigent.wrapper": "codex-native-ui-subagent",
            "omnigent.codex_native.agent_nickname": "Side chat",
            CLOSED_LABEL_KEY: "true",
        },
        side_chat_parent_id=parent.id,
    )

    response = await client.post(
        f"/v1/sessions/{side.id}/events",
        json={"type": "stop_session", "data": {"close_tab": True}},
    )

    assert response.status_code == 202, response.text
    saved = store.get_conversation(side.id)
    assert saved is not None and saved.labels.get(SIDE_CHAT_CLOSED_LABEL_KEY) == "1"


async def test_a_message_reopens_a_closed_side_chat_tab(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    """Sending to a side chat whose tab was closed brings the tab back on every device."""
    store = SqlAlchemyConversationStore(db_uri)
    agent_id = generate_agent_id()
    SqlAlchemyAgentStore(db_uri).create(agent_id, name="test", bundle_location="test:///bundle")
    side = store.create_conversation(
        agent_id=agent_id, labels={SIDE_CHAT_LABEL_KEY: "1", SIDE_CHAT_CLOSED_LABEL_KEY: "1"}
    )

    response = await client.post(
        f"/v1/sessions/{side.id}/events",
        json={
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": "one more"}]},
        },
    )

    assert response.status_code == 202, response.text
    saved = store.get_conversation(side.id)
    assert saved is not None and SIDE_CHAT_CLOSED_LABEL_KEY not in saved.labels


async def test_a_rejected_message_leaves_a_closed_codex_side_chat_closed(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    """A stale follow-up to a sealed Codex side chat is refused and its tab stays closed."""
    store = SqlAlchemyConversationStore(db_uri)
    agent_id = generate_agent_id()
    SqlAlchemyAgentStore(db_uri).create(agent_id, name="test", bundle_location="test:///bundle")
    parent = store.create_conversation(agent_id=agent_id)
    side = store.create_conversation(
        kind="sub_agent",
        parent_conversation_id=parent.id,
        agent_id=agent_id,
        labels={
            "omnigent.wrapper": "codex-native-ui-subagent",
            "omnigent.codex_native.agent_nickname": "Side chat",
            CLOSED_LABEL_KEY: "true",
            SIDE_CHAT_CLOSED_LABEL_KEY: "1",
        },
        side_chat_parent_id=parent.id,
    )

    response = await client.post(
        f"/v1/sessions/{side.id}/events",
        json={
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": "stale"}]},
        },
    )

    assert response.status_code == 409, response.text
    saved = store.get_conversation(side.id)
    assert saved is not None and SIDE_CHAT_CLOSED_LABEL_KEY in saved.labels
    listed = await client.get(f"/v1/sessions/{parent.id}/side_chats")
    assert [(row["id"], row["closed"]) for row in listed.json()["data"]] == [(side.id, True)]

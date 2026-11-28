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
from omnigent.stores.conversation_store import SIDE_CHAT_LABEL_KEY
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


async def test_close_side_chat_preserves_shared_runner_and_transcript(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
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

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        return httpx.Response(204)

    host_stop = AsyncMock(return_value=True)
    monkeypatch.setattr(routes_events, "_stop_host_runner_intentionally", host_stop)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://runner"
    ) as runner_client:
        monkeypatch.setattr(sessions, "_get_runner_client", AsyncMock(return_value=runner_client))
        response = await client.post(
            f"/v1/sessions/{side.id}/events", json={"type": "stop_session"}
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
            f"/v1/sessions/{side.id}/events", json={"type": "stop_session"}
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
    assert store.get_conversation(side.id) is not None

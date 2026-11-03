"""A user's side chats are listed on their parent session for their other clients."""

import httpx
import pytest

from omnigent.db.utils import generate_agent_id
from omnigent.entities import NewConversationItem
from omnigent.entities.conversation import MessageData
from omnigent.server.auth import LEVEL_OWNER, LEVEL_READ
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store import (
    SIDE_CHAT_ENTRY_LABEL_PREFIX,
    SIDE_CHAT_HISTORY_LABEL_KEY,
)
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

_OWNER = "alice@example.com"
_VIEWER = "bob@example.com"


def _session(store: SqlAlchemyConversationStore, db_uri: str, *, messages: int = 0) -> str:
    agent_id = generate_agent_id()
    SqlAlchemyAgentStore(db_uri).create(agent_id, name="test", bundle_location="test:///bundle")
    session_id = store.create_conversation(agent_id=agent_id).id
    store.append(
        session_id,
        [
            NewConversationItem(
                type="message",
                response_id=f"turn-{index}",
                data=MessageData(role="user", content=[{"type": "input_text", "text": "hi"}]),
            )
            for index in range(messages)
        ],
    )
    return session_id


async def _side_chats(client: httpx.AsyncClient, session_id: str, **params: object) -> dict:
    response = await client.get(f"/v1/sessions/{session_id}/side_chats", params=params)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("messages", [0, 2])
async def test_side_chat_is_listed_with_the_history_it_copied(
    client: httpx.AsyncClient, db_uri: str, messages: int
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    source_id = _session(store, db_uri, messages=messages)

    fork = await client.post(f"/v1/sessions/{source_id}/fork", json={"side_chat": True})

    assert fork.status_code == 201, fork.text
    side_chat_id = fork.json()["id"]
    listed = await _side_chats(client, source_id)
    assert [entry["id"] for entry in listed["data"]] == [side_chat_id]
    assert listed["has_more"] is False
    side_chat = await client.get(f"/v1/sessions/{side_chat_id}")
    copied = store.list_items(side_chat_id, limit=1, order="desc").data
    history_item_id = copied[0].id if messages else ""
    assert side_chat.json()["labels"][SIDE_CHAT_HISTORY_LABEL_KEY] == history_item_id
    assert fork.json()["labels"][SIDE_CHAT_HISTORY_LABEL_KEY] == history_item_id
    source = await client.get(f"/v1/sessions/{source_id}")
    assert not any(key.startswith(SIDE_CHAT_ENTRY_LABEL_PREFIX) for key in source.json()["labels"])


async def test_side_chat_list_pages_and_skips_deleted_side_chats(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    source_id = _session(store, db_uri)
    created = []
    for _ in range(3):
        fork = await client.post(f"/v1/sessions/{source_id}/fork", json={"side_chat": True})
        assert fork.status_code == 201, fork.text
        created.append(fork.json()["id"])

    first = await _side_chats(client, source_id, limit=2)
    rest = await _side_chats(client, source_id, limit=2, after=first["last_id"])

    assert first["has_more"] is True
    assert rest["has_more"] is False
    assert sorted(entry["id"] for entry in first["data"] + rest["data"]) == sorted(created)
    assert await store.delete_conversation(created[0])
    remaining = await _side_chats(client, source_id)
    assert sorted(entry["id"] for entry in remaining["data"]) == sorted(created[1:])


async def test_closed_side_chat_is_no_longer_listed(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    source_id = _session(store, db_uri)
    fork = await client.post(f"/v1/sessions/{source_id}/fork", json={"side_chat": True})
    assert fork.status_code == 201, fork.text
    side_chat_id = fork.json()["id"]

    for _ in range(2):
        closed = await client.delete(f"/v1/sessions/{source_id}/side_chats/{side_chat_id}")
        assert closed.status_code == 204, closed.text

    assert (await _side_chats(client, source_id))["data"] == []
    assert store.get_conversation(side_chat_id) is not None


async def test_side_chats_are_private_to_their_creator(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    source_id = _session(store, db_uri)
    for user in (_OWNER, _VIEWER):
        permissions.ensure_user(user)
    permissions.grant(_OWNER, source_id, LEVEL_OWNER)
    permissions.grant(_VIEWER, source_id, LEVEL_READ)
    as_owner = {"X-Forwarded-Email": _OWNER}
    as_viewer = {"X-Forwarded-Email": _VIEWER}
    fork = await auth_client.post(
        f"/v1/sessions/{source_id}/fork", json={"side_chat": True}, headers=as_owner
    )
    assert fork.status_code == 201, fork.text
    side_chat_id = fork.json()["id"]
    path = f"/v1/sessions/{source_id}/side_chats"

    viewer_list = await auth_client.get(path, headers=as_viewer)
    viewer_close = await auth_client.delete(f"{path}/{side_chat_id}", headers=as_viewer)
    owner_list = await auth_client.get(path, headers=as_owner)

    assert viewer_list.status_code == 200, viewer_list.text
    assert viewer_list.json()["data"] == []
    assert viewer_close.status_code == 204, viewer_close.text
    assert [entry["id"] for entry in owner_list.json()["data"]] == [side_chat_id]
    for headers in (as_owner, as_viewer):
        source = await auth_client.get(f"/v1/sessions/{source_id}", headers=headers)
        assert not any(
            key.startswith(SIDE_CHAT_ENTRY_LABEL_PREFIX) for key in source.json()["labels"]
        )


@pytest.mark.parametrize(
    "key", [f"{SIDE_CHAT_ENTRY_LABEL_PREFIX}conv_forged", SIDE_CHAT_HISTORY_LABEL_KEY]
)
async def test_clients_cannot_write_side_chat_labels(
    client: httpx.AsyncClient, db_uri: str, key: str
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    source_id = _session(store, db_uri)

    response = await client.patch(f"/v1/sessions/{source_id}", json={"labels": {key: "local"}})

    assert response.status_code == 400, response.text
    saved = store.get_conversation(source_id)
    assert saved is not None and key not in saved.labels


async def test_normal_fork_drops_side_chat_bookkeeping(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    source_id = _session(store, db_uri, messages=1)
    side = await client.post(f"/v1/sessions/{source_id}/fork", json={"side_chat": True})
    assert side.status_code == 201, side.text
    side_chat_id = side.json()["id"]

    for forked_from in (source_id, side_chat_id):
        fork = await client.post(f"/v1/sessions/{forked_from}/fork", json={})
        assert fork.status_code == 201, fork.text
        saved = store.get_conversation(fork.json()["id"])
        assert saved is not None
        assert SIDE_CHAT_HISTORY_LABEL_KEY not in saved.labels
        assert not any(key.startswith(SIDE_CHAT_ENTRY_LABEL_PREFIX) for key in saved.labels)

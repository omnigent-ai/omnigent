"""Route tests for thumbs up / thumbs down reactions on assistant responses."""

from __future__ import annotations

import httpx
import pytest

from omnigent.server.auth import LEVEL_READ
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.reaction_store.sqlalchemy_store import SqlAlchemyReactionStore

pytestmark = pytest.mark.asyncio

_ALICE = {"X-Forwarded-Email": "alice@example.com"}
_BOB = {"X-Forwarded-Email": "bob@example.com"}


def _seed_session_with_grants(db_uri: str, grants: dict[str, int]) -> str:
    conversation = SqlAlchemyConversationStore(db_uri).create_conversation()
    perm_store = SqlAlchemyPermissionStore(db_uri)
    for user_email, level in grants.items():
        perm_store.ensure_user(user_email)
        perm_store.grant(user_email, conversation.id, level)
    return conversation.id


async def test_set_list_and_clear_round_trip(auth_client: httpx.AsyncClient, db_uri: str) -> None:
    """A reader can rate a response, see it listed, replace it, and clear it."""
    session_id = _seed_session_with_grants(db_uri, {"alice@example.com": LEVEL_READ})

    put = await auth_client.put(
        f"/v1/sessions/{session_id}/reactions/resp_1",
        json={"rating": "down", "note": "  Missed the second file  "},
        headers=_ALICE,
    )
    assert put.status_code == 200, put.text
    body = put.json()
    assert body["rating"] == "down"
    assert body["note"] == "Missed the second file"
    assert body["created_by"] == "alice@example.com"

    listed = await auth_client.get(f"/v1/sessions/{session_id}/reactions", headers=_ALICE)
    assert [r["response_id"] for r in listed.json()] == ["resp_1"]

    again = await auth_client.put(
        f"/v1/sessions/{session_id}/reactions/resp_1", json={"rating": "up"}, headers=_ALICE
    )
    assert again.json()["rating"] == "up"
    assert again.json()["note"] is None

    cleared = await auth_client.delete(
        f"/v1/sessions/{session_id}/reactions/resp_1", headers=_ALICE
    )
    assert cleared.json() == {"removed": True}
    listed = await auth_client.get(f"/v1/sessions/{session_id}/reactions", headers=_ALICE)
    assert listed.json() == []


async def test_each_user_sees_only_their_own_reaction(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    session_id = _seed_session_with_grants(
        db_uri, {"alice@example.com": LEVEL_READ, "bob@example.com": LEVEL_READ}
    )
    await auth_client.put(
        f"/v1/sessions/{session_id}/reactions/resp_1", json={"rating": "up"}, headers=_ALICE
    )
    await auth_client.put(
        f"/v1/sessions/{session_id}/reactions/resp_1", json={"rating": "down"}, headers=_BOB
    )
    alice = await auth_client.get(f"/v1/sessions/{session_id}/reactions", headers=_ALICE)
    bob = await auth_client.get(f"/v1/sessions/{session_id}/reactions", headers=_BOB)
    assert [r["rating"] for r in alice.json()] == ["up"]
    assert [r["rating"] for r in bob.json()] == ["down"]


async def test_user_without_access_is_refused(auth_client: httpx.AsyncClient, db_uri: str) -> None:
    session_id = _seed_session_with_grants(db_uri, {"alice@example.com": LEVEL_READ})
    resp = await auth_client.put(
        f"/v1/sessions/{session_id}/reactions/resp_1", json={"rating": "up"}, headers=_BOB
    )
    assert resp.status_code in (403, 404), resp.text
    assert SqlAlchemyReactionStore(db_uri).list_for_user(session_id, "bob@example.com") == []


async def test_unknown_session_is_404(auth_client: httpx.AsyncClient) -> None:
    resp = await auth_client.put(
        "/v1/sessions/0123456789abcdef0123456789abcdef/reactions/resp_1",
        json={"rating": "up"},
        headers=_ALICE,
    )
    assert resp.status_code == 404


async def test_invalid_rating_and_oversized_note_are_422(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    session_id = _seed_session_with_grants(db_uri, {"alice@example.com": LEVEL_READ})
    bad_rating = await auth_client.put(
        f"/v1/sessions/{session_id}/reactions/resp_1", json={"rating": "meh"}, headers=_ALICE
    )
    assert bad_rating.status_code == 422
    long_note = await auth_client.put(
        f"/v1/sessions/{session_id}/reactions/resp_1",
        json={"rating": "down", "note": "x" * 1001},
        headers=_ALICE,
    )
    assert long_note.status_code == 422


async def test_single_user_mode_records_no_author(client: httpx.AsyncClient, db_uri: str) -> None:
    """Without auth the sentinel user maps to ``created_by: None``, like comments."""
    session_id = SqlAlchemyConversationStore(db_uri).create_conversation().id
    put = await client.put(f"/v1/sessions/{session_id}/reactions/resp_1", json={"rating": "up"})
    assert put.status_code == 200, put.text
    assert put.json()["created_by"] is None
    listed = await client.get(f"/v1/sessions/{session_id}/reactions")
    assert [r["rating"] for r in listed.json()] == ["up"]

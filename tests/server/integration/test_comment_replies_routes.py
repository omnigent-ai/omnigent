"""Comment reply REST boundaries, authorization and runner identity."""

from __future__ import annotations

import uuid

import httpx
import pytest
from fastapi import FastAPI

from omnigent.runner.identity import (
    ORCHESTRATION_SOURCE_HEADER,
    RUNNER_TUNNEL_TOKEN_HEADER,
    token_bound_runner_id,
)
from omnigent.server.auth import LEVEL_EDIT, LEVEL_OWNER, LEVEL_READ
from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.server.integration.test_comments_routes import _seed_session_with_grants

pytestmark = pytest.mark.asyncio
OWNER = "owner@example.test"
EDITOR = "editor@example.test"
READER = "reader@example.test"


async def test_reply_routes_permissions_and_pagination(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    session_id = _seed_session_with_grants(
        db_uri, {OWNER: LEVEL_OWNER, EDITOR: LEVEL_EDIT, READER: LEVEL_READ}
    )
    store = SqlAlchemyCommentStore(db_uri)
    comment = store.add(session_id, "file.py", "Question?", 0, 1)
    base = f"/v1/sessions/{session_id}/comments/{comment.id}/replies"
    empty = (
        await auth_client.get(
            f"/v1/sessions/{session_id}/comments", headers={"X-Forwarded-Email": OWNER}
        )
    ).json()[0]
    assert empty["reply_count"] == 0 and empty["last_reply"] is None
    owner = {"X-Forwarded-Email": OWNER}
    editor = {"X-Forwarded-Email": EDITOR}
    reader = {"X-Forwarded-Email": READER}
    response = await auth_client.post(
        base, headers=owner, json={"body": "Answer", "author_kind": "agent"}
    )
    assert response.status_code == 200
    reply = response.json()
    assert reply["author_kind"] == "user" and reply["created_by"] == OWNER
    assert (await auth_client.get(base, headers=reader)).status_code == 200
    assert (await auth_client.post(base, headers=reader, json={"body": "No"})).status_code == 403
    assert (await auth_client.delete(f"{base}/{reply['id']}", headers=reader)).status_code == 403
    assert (await auth_client.delete(f"{base}/{reply['id']}", headers=editor)).status_code == 403
    for body in ("", " ", "é" * 6001):
        response = await auth_client.post(base, headers=owner, json={"body": body})
        assert response.status_code == 400
        assert "12,000" in response.text
    assert (await auth_client.get(base + "?after_created_at=1", headers=owner)).status_code == 400
    for index in range(199):
        store.add_reply(comment.id, session_id, f"Answer {index}")
    response = await auth_client.post(base, headers=owner, json={"body": "Full"})
    assert response.status_code == 409
    response = await auth_client.get(base + "?limit=100", headers=reader)
    assert len(response.json()["replies"]) == 100
    cursor = response.json()["next_cursor"]
    response = await auth_client.get(
        base,
        headers=reader,
        params={"after_created_at": cursor["created_at"], "after_id": cursor["id"], "limit": 100},
    )
    assert len(response.json()["replies"]) == 100 and response.json()["next_cursor"] is None
    listed = (await auth_client.get(f"/v1/sessions/{session_id}/comments", headers=reader)).json()
    assert listed[0]["reply_count"] == 200
    patched = await auth_client.patch(
        f"/v1/sessions/{session_id}/comments/{comment.id}",
        headers=editor,
        json={"status": "addressed"},
    )
    assert patched.json()["reply_count"] == 200
    assert patched.json()["last_reply"] == listed[0]["last_reply"]
    descending = await auth_client.get(base, headers=reader, params={"order": "desc", "limit": 1})
    assert descending.json()["replies"][0]["id"] == response.json()["replies"][-1]["id"]
    assert listed[0]["last_reply"] == {
        "author_kind": "user",
        "created_by": None,
        "created_at": response.json()["replies"][-1]["created_at"],
    }
    assert (await auth_client.delete(f"{base}/{reply['id']}", headers=owner)).json() == {
        "deleted": True
    }
    other = _seed_session_with_grants(db_uri, {OWNER: LEVEL_OWNER})
    wrong = f"/v1/sessions/{other}/comments/{comment.id}/replies"
    assert (await auth_client.get(wrong, headers=owner)).status_code == 404
    assert (
        await auth_client.post(wrong, headers=owner, json={"body": "Wrong"})
    ).status_code == 404
    assert (await auth_client.delete(f"{wrong}/{reply['id']}", headers=owner)).status_code == 404


async def test_agent_reply_requires_matching_runner_origin(
    auth_client: httpx.AsyncClient,
    auth_app: FastAPI,
    db_uri: str,
) -> None:
    session_id = _seed_session_with_grants(db_uri, {OWNER: LEVEL_OWNER, EDITOR: LEVEL_EDIT})
    store = SqlAlchemyCommentStore(db_uri)
    comment = store.add(session_id, "file.py", "Question?", 0, 1)
    base = f"/v1/sessions/{session_id}/comments/{comment.id}/replies"
    user = {"X-Forwarded-Email": OWNER}
    assert (
        await auth_client.post(base + "/agent", headers=user, json={"body": "Spoof"})
    ).status_code == 403
    token = "comment-reply-test-runner"
    auth_app.state.runner_tunnel_tokens = {token}
    conversations = SqlAlchemyConversationStore(db_uri)
    conversations.set_runner_id(session_id, token_bound_runner_id(token))
    headers = {
        **user,
        ORCHESTRATION_SOURCE_HEADER: str(uuid.UUID(session_id)),
        RUNNER_TUNNEL_TOKEN_HEADER: token,
    }
    assert (
        await auth_client.post(
            base + "/agent",
            headers={**headers, "X-Forwarded-Email": EDITOR},
            json={"body": "Non-owner spoof"},
        )
    ).status_code == 403
    response = await auth_client.post(
        base + "/agent", headers=headers, json={"body": "Agent answer"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["author_kind"] == "agent" and response.json()["created_by"] is None
    listed = (await auth_client.get(f"/v1/sessions/{session_id}/comments", headers=user)).json()[0]
    assert listed["reply_count"] == 1
    assert listed["last_reply"] == {
        key: response.json()[key] for key in ("author_kind", "created_by", "created_at")
    }
    other = _seed_session_with_grants(db_uri, {OWNER: LEVEL_OWNER})
    conversations.set_runner_id(other, token_bound_runner_id(token))
    headers[ORCHESTRATION_SOURCE_HEADER] = other
    assert (
        await auth_client.post(base + "/agent", headers=headers, json={"body": "Wrong source"})
    ).status_code == 403
    headers[ORCHESTRATION_SOURCE_HEADER] = session_id
    headers[RUNNER_TUNNEL_TOKEN_HEADER] = "wrong-token"
    assert (
        await auth_client.post(base + "/agent", headers=headers, json={"body": "Wrong runner"})
    ).status_code == 403
    reply_id = response.json()["id"]
    assert (
        await auth_client.delete(f"{base}/{reply_id}", headers={"X-Forwarded-Email": EDITOR})
    ).status_code == 200

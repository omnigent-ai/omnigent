"""Shared document routes through real auth, permissions, and SQL state."""

from __future__ import annotations

import asyncio
import json
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text

from omnigent.db.db_models import SqlPreference
from omnigent.db.utils import get_or_create_engine
from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER, RUNNER_TUNNEL_TOKEN_HEADER
from omnigent.server.auth import LEVEL_EDIT, LEVEL_OWNER, LEVEL_READ
from omnigent.server.feature_flags import resolve_feature_flags
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.session_document_store import SessionDocumentStore
from tests.server.routes.test_user_settings_routes import multi_user_app as multi_user_app

OWNER = "owner@example.test"
READER = "reader@example.test"
EDITOR = "editor@example.test"
OUTSIDER = "outsider@example.test"
URL = "https://docs.google.com/document/d/example/edit"
PRIVATE_URL = "https://docs.google.com/document/d/private_example/edit"


def headers(user: str = OWNER) -> dict[str, str]:
    return {"X-Forwarded-Email": user}


@pytest.fixture
def session_id(multi_user_app: FastAPI, db_uri: str) -> str:
    permissions = SqlAlchemyPermissionStore(db_uri)
    for user in (OWNER, READER, EDITOR, OUTSIDER):
        permissions.ensure_user(user)
    agent = SqlAlchemyAgentStore(db_uri).create(
        agent_id="a" * 32, name="documents-test", bundle_location="documents-test/bundle"
    )
    conversations = SqlAlchemyConversationStore(db_uri)
    conv = conversations.create_conversation(agent_id=agent.id)
    conversations.set_labels(conv.id, {"ui.google_docs_url": PRIVATE_URL, "unrelated": "keep"})
    for user, level in [(OWNER, LEVEL_OWNER), (READER, LEVEL_READ), (EDITOR, LEVEL_EDIT)]:
        permissions.grant(user, conv.id, level)
    multi_user_app.state.feature_flags = resolve_feature_flags(
        {"OMNIGENT_FEATURES": "google_docs"}
    )
    return conv.id


@pytest.fixture
async def documents_client(multi_user_app: FastAPI):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=multi_user_app), base_url="http://server.example"
    ) as client:
        yield client


def path(session_id: str) -> str:
    return f"/v1/sessions/{session_id}/session-documents"


async def test_shared_read_edit_permissions_and_original_url(
    documents_client, session_id, db_uri
) -> None:
    client = documents_client
    route = path(session_id)
    initial = await client.get(route, headers=headers(READER))
    assert initial.status_code == 200
    assert initial.json() == {"session_id": session_id, "entries": []}
    assert initial.headers["Cache-Control"] == "no-store"
    assert (
        await client.post(route, json={"url": URL}, headers=headers(READER))
    ).status_code == 403
    assert (await client.delete(route + "/example", headers=headers(READER))).status_code == 403
    original = URL + "?authuser=2&usp=sharing#tab=t.one"
    added = await client.post(route, json={"url": original}, headers=headers(EDITOR))
    assert added.status_code == 200, added.text
    assert added.headers["Cache-Control"] == "no-store"
    first = added.json()
    assert first["entries"][0]["document_url"] == original
    assert first["entries"][0]["added_at"] > 1_000_000_000_000_000
    for user in (OWNER, READER, EDITOR):
        assert (await client.get(route, headers=headers(user))).json() == first
    assert (
        await client.post(route, json={"url": URL + "?tab=t.two"}, headers=headers())
    ).json() == first
    assert (await client.delete(route + "/example", headers=headers(EDITOR))).json()[
        "entries"
    ] == []
    assert (await client.delete(route + "/example", headers=headers(EDITOR))).json()[
        "entries"
    ] == []
    assert (await client.get(route, headers=headers(READER))).json()["entries"] == []
    assert SqlAlchemyConversationStore(db_uri).get_conversation(session_id).labels == {
        "ui.google_docs_url": PRIVATE_URL,
        "unrelated": "keep",
    }


async def test_auth_and_session_boundaries(documents_client, session_id, db_uri) -> None:
    client = documents_client
    other = SqlAlchemyConversationStore(db_uri).create_conversation()
    cases = [
        ({}, path(session_id), 401),
        (headers(OUTSIDER), path(session_id), 404),
        (headers(), path(other.id), 404),
    ]
    cases += [
        ({**headers(), header: session_id}, path(session_id), 403)
        for header in (ORCHESTRATION_SOURCE_HEADER, RUNNER_TUNNEL_TOKEN_HEADER)
    ]
    for supplied, route, status in cases:
        for method, suffix, body in [
            ("GET", "", None),
            ("POST", "", {"url": URL}),
            ("DELETE", "/example", None),
        ]:
            response = await client.request(
                method, route + suffix, headers=supplied, **({"json": body} if body else {})
            )
            assert response.status_code == status, response.text
            assert response.headers["Cache-Control"] == "no-store"
    assert SessionDocumentStore(db_uri).read(session_id) == []


async def test_no_private_preferences_are_promoted_or_changed(
    documents_client, session_id, db_uri
) -> None:
    engine = get_or_create_engine(db_uri)
    private_value = json.dumps(
        {
            "entries": [{"document_url": PRIVATE_URL, "last_opened_at": 123}],
            "high_water_stamp": 123,
        }
    )
    with engine.begin() as conn:
        conn.execute(
            SqlPreference.__table__.insert(),
            [
                {
                    "workspace_id": 0,
                    "user_id": user,
                    "key": "google_docs.history." + session_id,
                    "value": private_value,
                }
                for user in (OWNER, READER)
            ],
        )
    with engine.connect() as conn:
        before = conn.execute(
            text("SELECT user_id, key, value FROM preferences ORDER BY user_id, key")
        ).all()
    for user in (OWNER, READER):
        result = await documents_client.get(path(session_id), headers=headers(user))
        assert result.json()["entries"] == []
        assert PRIVATE_URL not in result.text
    await documents_client.post(path(session_id), json={"url": URL}, headers=headers())
    await documents_client.delete(path(session_id) + "/example", headers=headers())
    with engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT user_id, key, value FROM preferences ORDER BY user_id, key")
            ).all()
            == before
        )


async def test_default_off_and_capability_are_consistent(
    documents_client, multi_user_app, session_id, db_uri
) -> None:
    client = documents_client
    assert (await client.get("/v1/info", headers=headers())).json()["features"][
        "google_docs"
    ] is True
    multi_user_app.state.feature_flags = resolve_feature_flags({})
    assert (await client.get("/v1/info", headers=headers())).json()["features"][
        "google_docs"
    ] is False
    for method, suffix, body in [
        ("GET", "", None),
        ("POST", "", {"url": URL}),
        ("DELETE", "/example", None),
    ]:
        response = await client.request(
            method,
            path(session_id) + suffix,
            headers=headers(),
            **({"json": body} if body else {}),
        )
        assert response.status_code == 403
    assert SessionDocumentStore(db_uri).read(session_id) == []


async def test_validation_and_canonical_session_ids(documents_client, session_id) -> None:
    client = documents_client
    for url in [
        URL + "#heading=h.one",
        URL + "?resourcekey=example",
        "http://docs.google.com/document/d/example/edit",
        URL + "?usp=" + "x" * 2100,
    ]:
        response = await client.post(path(session_id), json={"url": url}, headers=headers())
        assert response.status_code == 400, response.text
    for extra in [{"user_id": READER}, {"added_at": 1}, {"selected": URL}]:
        response = await client.post(
            path(session_id), json={"url": URL, **extra}, headers=headers()
        )
        assert response.status_code == 422
        assert response.headers["Cache-Control"] == "no-store"
    alternate_id = str(UUID(session_id)).upper()
    added = await client.post(path(alternate_id), json={"url": URL}, headers=headers())
    assert added.status_code == 200, added.text
    assert added.json()["session_id"] == session_id
    assert (await client.get(path(session_id), headers=headers(READER))).json() == added.json()


async def test_concurrent_api_adds_restore_from_new_store(
    documents_client, session_id, db_uri
) -> None:
    route = path(session_id)
    replies = await asyncio.gather(
        *[
            documents_client.post(
                route,
                json={"url": URL.replace("example", f"doc_{index}")},
                headers=headers(EDITOR),
            )
            for index in range(6)
        ]
    )
    assert all(reply.status_code == 200 for reply in replies), [reply.text for reply in replies]
    final = (await documents_client.get(route, headers=headers(READER))).json()["entries"]
    assert len(final) == 6
    assert final == [entry.model_dump() for entry in SessionDocumentStore(db_uri).read(session_id)]


async def test_capacity_rejection_is_visible_and_preserves_membership(
    documents_client, session_id, monkeypatch
) -> None:
    from omnigent.stores import session_document_store

    monkeypatch.setattr(session_document_store, "MAX_DOCUMENTS_PER_SESSION", 1)
    first = await documents_client.post(path(session_id), json={"url": URL}, headers=headers())
    rejected = await documents_client.post(
        path(session_id), json={"url": URL.replace("example", "other")}, headers=headers()
    )
    assert rejected.status_code == 409
    assert (
        await documents_client.get(path(session_id), headers=headers(READER))
    ).json() == first.json()

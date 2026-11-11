"""Tests for the ``/v1/proactive-agent/settings`` routes and the per-user gate."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.server.app import create_app
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.feature_flags import resolve_feature_flags
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.inbox_item_store.sqlalchemy_store import SqlAlchemyInboxItemStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.proactive_agent_settings_store import (
    WORKSPACE_SETTINGS_USER,
    ProactiveAgentSettingsStore,
)

_ALICE = {"X-Forwarded-Email": "alice@example.com"}
_BOB = {"X-Forwarded-Email": "bob@example.com"}
_ADMIN = {"X-Forwarded-Email": "admin@example.com"}


@asynccontextmanager
async def _client(
    db_uri: str, tmp_path: Path, *, offered: bool = True, **extra: Any
) -> AsyncIterator[httpx.AsyncClient]:
    conversations = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.ensure_user("admin@example.com")
    permissions.set_admin("admin@example.com", True)
    app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=conversations,
        artifact_store=LocalArtifactStore(str(tmp_path / "artifacts")),
        agent_cache=None,  # type: ignore[arg-type]
        inbox_item_store=SqlAlchemyInboxItemStore(db_uri),
        proactive_agent_settings_store=ProactiveAgentSettingsStore(db_uri),
        auth_provider=UnifiedAuthProvider(source="header", local_single_user=False),
        permission_store=permissions,
        feature_flags=resolve_feature_flags(
            {"OMNIGENT_FEATURES": "proactive_agent" if offered else ""}
        ),
        **extra,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


async def _settings(client: httpx.AsyncClient, headers: dict[str, str]) -> dict[str, Any]:
    resp = await client.get("/v1/proactive-agent/settings", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_settings_are_hidden_where_the_deployment_does_not_offer_the_agent(
    db_uri: str, tmp_path: Path
) -> None:
    async with _client(db_uri, tmp_path, offered=False) as client:
        assert (
            await client.get("/v1/proactive-agent/settings", headers=_ALICE)
        ).status_code == 404
        put = await client.put(
            "/v1/proactive-agent/settings/me", json={"enabled": True}, headers=_ALICE
        )
        assert put.status_code == 404
        assert (await client.get("/v1/info", headers=_ALICE)).json()[
            "proactive_agent_available"
        ] is False


async def test_both_settings_start_on(db_uri: str, tmp_path: Path) -> None:
    async with _client(db_uri, tmp_path) as client:
        assert await _settings(client, _ALICE) == {
            "workspace_enabled": True,
            "user_enabled": True,
            "enabled": True,
            "can_manage_workspace": False,
        }
        assert (await _settings(client, _ADMIN))["can_manage_workspace"] is True
        assert (await client.get("/v1/info", headers=_ALICE)).json()[
            "proactive_agent_available"
        ] is True


async def test_a_user_can_turn_it_off_for_themselves_only(db_uri: str, tmp_path: Path) -> None:
    async with _client(db_uri, tmp_path) as client:
        off = await client.put(
            "/v1/proactive-agent/settings/me", json={"enabled": False}, headers=_ALICE
        )

        assert off.status_code == 200, off.text
        assert (off.json()["user_enabled"], off.json()["enabled"]) == (False, False)
        assert (await client.get("/v1/inbox/items", headers=_ALICE)).status_code == 404
        assert (await client.get("/v1/inbox/items", headers=_BOB)).status_code == 200

        await client.put("/v1/proactive-agent/settings/me", json={"enabled": True}, headers=_ALICE)
        assert (await client.get("/v1/inbox/items", headers=_ALICE)).status_code == 200


async def test_the_workspace_setting_overrides_every_user(db_uri: str, tmp_path: Path) -> None:
    async with _client(db_uri, tmp_path) as client:
        denied = await client.put(
            "/v1/proactive-agent/settings/workspace", json={"enabled": False}, headers=_ALICE
        )
        assert denied.status_code == 403

        off = await client.put(
            "/v1/proactive-agent/settings/workspace", json={"enabled": False}, headers=_ADMIN
        )
        assert off.status_code == 200, off.text
        await client.put("/v1/proactive-agent/settings/me", json={"enabled": True}, headers=_ALICE)

        alice = await _settings(client, _ALICE)
        assert (alice["workspace_enabled"], alice["user_enabled"], alice["enabled"]) == (
            False,
            True,
            False,
        )
        for headers in (_ALICE, _BOB, _ADMIN):
            assert (await client.get("/v1/inbox/items", headers=headers)).status_code == 404


async def test_a_deployment_gate_that_is_off_wins_over_both_settings(
    db_uri: str, tmp_path: Path
) -> None:
    async with _client(db_uri, tmp_path, offered=False, proactive_agent=lambda: False) as client:
        assert (await client.get("/v1/inbox/items", headers=_ALICE)).status_code == 404


@pytest.mark.parametrize("raw", ["not json", '"yes"', "null"])
def test_an_unreadable_stored_value_reads_as_on(db_uri: str, raw: str) -> None:
    from sqlalchemy.orm import Session

    from omnigent.db.db_models import SqlPreference
    from omnigent.db.utils import get_or_create_engine

    store = ProactiveAgentSettingsStore(db_uri)
    store.set_workspace_enabled(False)
    with Session(get_or_create_engine(db_uri)) as session:
        row = session.get(
            SqlPreference, (0, WORKSPACE_SETTINGS_USER, "proactive_agent.v1.enabled")
        )
        assert row is not None
        row.value = raw
        session.commit()

    assert store.read("alice@example.com") == (True, True)


async def test_the_bundle_download_tells_the_runner_whether_to_offer_the_inbox_tools(
    db_uri: str, tmp_path: Path
) -> None:
    from omnigent.automations_availability import USER_INBOX_HEADER
    from omnigent.server.auth import LEVEL_OWNER, LEVEL_READ
    from tests.server.helpers import build_agent_bundle

    agent = SqlAlchemyAgentStore(db_uri).create("a" * 32, "inbox-agent", "agents/inbox.tar.gz")
    LocalArtifactStore(str(tmp_path / "artifacts")).put(
        agent.bundle_location, build_agent_bundle("inbox-agent", tool_groups={"user_inbox": True})
    )
    session_id = SqlAlchemyConversationStore(db_uri).create_conversation(agent_id=agent.id).id
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.grant("alice@example.com", session_id, LEVEL_OWNER)
    permissions.grant("bob@example.com", session_id, LEVEL_READ)
    url = f"/v1/sessions/{session_id}/agent/contents"

    async with _client(db_uri, tmp_path) as client:
        assert (await client.get(url, headers=_ALICE)).headers[USER_INBOX_HEADER] == "1"
        await client.put(
            "/v1/proactive-agent/settings/me", json={"enabled": False}, headers=_ALICE
        )
        assert (await client.get(url, headers=_ALICE)).headers[USER_INBOX_HEADER] == "0"
        # The owner's setting decides, whoever downloads the bundle.
        assert (await client.get(url, headers=_BOB)).headers[USER_INBOX_HEADER] == "0"
        # A child session has no grant of its own; its root's owner decides.
        child = SqlAlchemyConversationStore(db_uri).create_conversation(
            agent_id=agent.id, parent_conversation_id=session_id
        )
        child_url = f"/v1/sessions/{child.id}/agent/contents"
        assert (await client.get(child_url, headers=_ALICE)).headers[USER_INBOX_HEADER] == "0"
        await client.put("/v1/proactive-agent/settings/me", json={"enabled": True}, headers=_ALICE)
        assert (await client.get(child_url, headers=_ALICE)).headers[USER_INBOX_HEADER] == "1"

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
from omnigent.server.proactive_agent import PROACTIVE_AGENT_ID
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


@pytest.fixture(autouse=True)
def _multi_user_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """The suite runs as a local single-user server by default; these routes gate on admins."""
    monkeypatch.delenv("OMNIGENT_LOCAL_SINGLE_USER", raising=False)


@asynccontextmanager
async def _client(
    db_uri: str,
    tmp_path: Path,
    *,
    offered: bool = True,
    settings_store: bool = True,
    local_single_user: bool = False,
    **extra: Any,
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
        proactive_agent_settings_store=(
            ProactiveAgentSettingsStore(db_uri) if settings_store else None
        ),
        auth_provider=UnifiedAuthProvider(source="header", local_single_user=local_single_user),
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
    body = resp.json()
    # The web client tells the agent's Inbox items apart by this id.
    assert body.pop("agent_id") == PROACTIVE_AGENT_ID
    return body


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
        assert (await _settings(client, _BOB))["enabled"] is True
        # The setting stops the agent, not the Inbox other agents post to.
        assert (await client.get("/v1/inbox/items", headers=_ALICE)).status_code == 200

        await client.put("/v1/proactive-agent/settings/me", json={"enabled": True}, headers=_ALICE)
        assert (await _settings(client, _ALICE))["enabled"] is True


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
        # An external identity spelled like the reserved workspace owner cannot
        # reach the workspace row through its own setting.
        sentinel = {"X-Forwarded-Email": WORKSPACE_SETTINGS_USER}
        for enabled in (True, False):
            spoof = await client.put(
                "/v1/proactive-agent/settings/me", json={"enabled": enabled}, headers=sentinel
            )
            assert spoof.status_code in (401, 403), spoof.text
        assert (await _settings(client, _ADMIN))["workspace_enabled"] is False

        alice = await _settings(client, _ALICE)
        assert (alice["workspace_enabled"], alice["user_enabled"], alice["enabled"]) == (
            False,
            True,
            False,
        )
        for headers in (_BOB, _ADMIN):
            assert (await _settings(client, headers))["enabled"] is False
            assert (await client.get("/v1/inbox/items", headers=headers)).status_code == 200


async def test_a_deployment_gate_that_is_off_wins_over_both_settings(
    db_uri: str, tmp_path: Path
) -> None:
    # Offered, both settings on, but the explicit gate says no: the gate wins.
    async with _client(db_uri, tmp_path, offered=True, proactive_agent=lambda: False) as client:
        info = (await client.get("/v1/info", headers=_ALICE)).json()
        assert info["proactive_agent_available"] is False
        assert (
            await client.get("/v1/proactive-agent/settings", headers=_ALICE)
        ).status_code == 404
        assert (await client.get("/v1/inbox/items", headers=_ALICE)).status_code == 200


async def test_the_bundle_download_offers_the_inbox_tools_whatever_the_advisor_setting(
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
        # Omni Advisor's setting is about that agent only; every agent keeps its tools.
        await client.put(
            "/v1/proactive-agent/settings/me", json={"enabled": False}, headers=_ALICE
        )
        assert (await client.get(url, headers=_ALICE)).headers[USER_INBOX_HEADER] == "1"
        assert (await client.get(url, headers=_BOB)).headers[USER_INBOX_HEADER] == "1"
        child = SqlAlchemyConversationStore(db_uri).create_conversation(
            agent_id=agent.id, parent_conversation_id=session_id
        )
        child_url = f"/v1/sessions/{child.id}/agent/contents"
        good = await client.get(child_url, headers=_ALICE)
        assert good.headers[USER_INBOX_HEADER] == "1"
        # The Inbox is optional: a failing check turns its tools off without
        # failing the download.
        app = client._transport.app  # type: ignore[attr-defined]

        def failing_check(user_id: str) -> bool:
            raise RuntimeError("gate down")

        app.state.inbox_items_enabled_for = failing_check
        degraded = await client.get(child_url, headers=_ALICE)
        assert (degraded.status_code, degraded.headers[USER_INBOX_HEADER]) == (200, "0")
        assert degraded.content == good.content

    # With the Inbox off for the deployment, no agent gets the tools.
    async with _client(db_uri, tmp_path, user_inbox=False) as client:
        assert (await client.get(url, headers=_ALICE)).headers[USER_INBOX_HEADER] == "0"


async def test_without_a_settings_store_the_agent_is_not_offered(
    db_uri: str, tmp_path: Path
) -> None:
    """The web client reads the settings before it shows the agent's tab."""
    async with _client(db_uri, tmp_path, settings_store=False) as client:
        info = await client.get("/v1/info", headers=_ALICE)
        assert info.json()["proactive_agent_available"] is False
        settings = await client.get("/v1/proactive-agent/settings", headers=_ALICE)
        assert settings.status_code == 404
        # Other agents' Inbox items do not need the agent's settings.
        assert (await client.get("/v1/inbox/items", headers=_ALICE)).status_code == 200


async def test_the_local_single_user_manages_the_workspace_setting(
    db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A local single-user server has a permission store too; its one user is in charge."""
    monkeypatch.setenv("OMNIGENT_LOCAL_SINGLE_USER", "1")
    async with _client(db_uri, tmp_path, local_single_user=True) as client:
        settings = (await client.get("/v1/proactive-agent/settings")).json()
        assert (settings["can_manage_workspace"], settings["workspace_enabled"]) == (True, True)
        changed = await client.put(
            "/v1/proactive-agent/settings/workspace", json={"enabled": False}
        )
        assert changed.status_code == 200
        assert changed.json()["workspace_enabled"] is False

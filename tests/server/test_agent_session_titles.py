"""Agent renames use server title policy without affecting manual renames."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.entities import Conversation
from omnigent.runner.tool_dispatch import execute_tool
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import LEVEL_EDIT, LEVEL_OWNER, LEVEL_READ, UnifiedAuthProvider
from omnigent.server.background_session_titles import BackgroundTitleRequest
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio

_TITLE_INSTRUCTIONS = (
    "Prefix titles with the current date as lowercase mon-dd. "
    "For pull requests use mon-dd-PR-number-short-kebab-case-name. Do not use spaces."
)


@pytest.fixture
def title_app(runtime_init: None, db_uri: str, tmp_path: Path) -> FastAPI:
    artifact_store = LocalArtifactStore(str(tmp_path / "title-artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "title-cache"),
        permission_store=SqlAlchemyPermissionStore(db_uri),
        auth_provider=UnifiedAuthProvider(source="header", local_single_user=False),
        server_config={"session_title_instructions": _TITLE_INSTRUCTIONS},
    )


@pytest_asyncio.fixture
async def title_client(title_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=title_app),
        base_url="http://test",
        headers={"X-Forwarded-Email": "owner@example.test"},
    ) as client:
        yield client
    await title_app.state.background_title_coordinator.shutdown()


@pytest_asyncio.fixture
async def title_session(db_uri: str, title_client: httpx.AsyncClient) -> Conversation:
    agent = await create_test_agent(title_client)
    session = SqlAlchemyConversationStore(db_uri).create_conversation(
        kind="default", title="sep-13-PR-123-investigate-failure", agent_id=agent["id"]
    )
    SqlAlchemyPermissionStore(db_uri).grant("owner@example.test", session.id, LEVEL_OWNER)
    return session


async def test_repeated_agent_renames_apply_configured_policy(
    title_app: FastAPI,
    title_client: httpx.AsyncClient,
    title_session: Conversation,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    formatted_titles = ["sep-13-PR-123-resolve-conflict", "sep-13-PR-123-verify-fix"]
    generator = AsyncMock(side_effect=formatted_titles)
    monkeypatch.setattr(title_app.state.background_title_coordinator, "_generator", generator)

    for proposal, formatted in zip(
        ["Resolve PR 123 merge conflict", "Verify PR 123 fix"], formatted_titles, strict=True
    ):
        output = await execute_tool(
            tool_name="sys_session_rename",
            arguments=json.dumps({"title": proposal}),
            server_client=title_client,
            conversation_id=title_session.id,
        )
        assert json.loads(output) == {"renamed": True, "title": formatted, "reason": None}
        snapshot = SqlAlchemyConversationStore(db_uri).get_conversation(title_session.id)
        assert snapshot is not None and snapshot.title == formatted
        request = generator.call_args.args[0]
        assert request.prompt == proposal
        assert request.additional_instructions == _TITLE_INSTRUCTIONS
        assert request.session_id == title_session.id
        assert request.agent_id == title_session.agent_id
        assert request.harness_override == title_session.harness_override
        assert request.model_override == title_session.model_override

    assert generator.await_count == 2


@pytest.mark.parametrize("instructions", [None, "", "  "])
async def test_no_policy_uses_proposal_without_inference(
    title_app: FastAPI,
    title_client: httpx.AsyncClient,
    title_session: Conversation,
    monkeypatch: pytest.MonkeyPatch,
    instructions: str | None,
) -> None:
    coordinator = title_app.state.background_title_coordinator
    monkeypatch.setattr(coordinator, "_additional_instructions", instructions)
    generator = AsyncMock()
    monkeypatch.setattr(coordinator, "_generator", generator)
    response = await title_client.post(
        f"/v1/sessions/{title_session.id}/agent-title",
        json={"title": "  Resolve PR 123 conflict  "},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"renamed": True, "title": "Resolve PR 123 conflict", "reason": None}
    generator.assert_not_awaited()


@pytest.mark.parametrize("failure", [None, "", "?", RuntimeError("offline"), TimeoutError()])
async def test_failed_generation_preserves_existing_title(
    title_app: FastAPI,
    title_client: httpx.AsyncClient,
    title_session: Conversation,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    failure: str | Exception | None,
) -> None:
    generator = (
        AsyncMock(side_effect=failure)
        if isinstance(failure, Exception)
        else AsyncMock(return_value=failure)
    )
    monkeypatch.setattr(title_app.state.background_title_coordinator, "_generator", generator)
    response = await title_client.post(
        f"/v1/sessions/{title_session.id}/agent-title", json={"title": "Resolve PR 123 conflict"}
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"renamed": False, "title": None, "reason": "generation_failed"}
    snapshot = SqlAlchemyConversationStore(db_uri).get_conversation(title_session.id)
    assert snapshot is not None and snapshot.title == title_session.title


async def test_manual_rename_wins_over_in_flight_agent_formatting(
    title_app: FastAPI,
    title_client: httpx.AsyncClient,
    title_session: Conversation,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def generator(_request: BackgroundTitleRequest) -> str:
        started.set()
        await release.wait()
        return "sep-13-PR-123-resolve-conflict"

    monkeypatch.setattr(title_app.state.background_title_coordinator, "_generator", generator)
    pending = asyncio.create_task(
        title_client.post(
            f"/v1/sessions/{title_session.id}/agent-title",
            json={"title": "Resolve PR 123 conflict"},
        )
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        manual = await title_client.patch(
            f"/v1/sessions/{title_session.id}", json={"title": "My exact manual title"}
        )
        assert manual.status_code == 200, manual.text
    finally:
        release.set()
        response = await asyncio.wait_for(pending, timeout=5)

    assert response.json() == {"renamed": False, "title": None, "reason": "title_changed"}
    snapshot = SqlAlchemyConversationStore(db_uri).get_conversation(title_session.id)
    assert snapshot is not None and snapshot.title == "My exact manual title"


@pytest.mark.parametrize("length", [150, 250])
async def test_formatted_agent_title_uses_custom_title_limit(
    title_app: FastAPI,
    title_client: httpx.AsyncClient,
    title_session: Conversation,
    monkeypatch: pytest.MonkeyPatch,
    length: int,
) -> None:
    formatted = "sep-13-PR-123-" + "description" * length
    formatted = formatted[:length]
    generator = AsyncMock(return_value=formatted)
    monkeypatch.setattr(title_app.state.background_title_coordinator, "_generator", generator)
    response = await title_client.post(
        f"/v1/sessions/{title_session.id}/agent-title", json={"title": "Resolve PR 123 conflict"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["title"] == (formatted if length <= 200 else formatted[:199] + "…")


@pytest.mark.parametrize(
    "proposal", ["a", "  ", "title\nsecond line", "title\rsecond line", "a" * 101]
)
async def test_invalid_agent_proposal_never_reaches_generator(
    title_app: FastAPI,
    title_client: httpx.AsyncClient,
    title_session: Conversation,
    monkeypatch: pytest.MonkeyPatch,
    proposal: str,
) -> None:
    generator = AsyncMock()
    monkeypatch.setattr(title_app.state.background_title_coordinator, "_generator", generator)
    response = await title_client.post(
        f"/v1/sessions/{title_session.id}/agent-title", json={"title": proposal}
    )
    assert response.status_code in (400, 422), response.text
    generator.assert_not_awaited()


async def test_child_session_cannot_be_renamed_via_agent_endpoint(
    title_app: FastAPI,
    title_client: httpx.AsyncClient,
    title_session: Conversation,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child = SqlAlchemyConversationStore(db_uri).create_conversation(
        kind="default", title="worker:task", parent_conversation_id=title_session.id
    )
    generator = AsyncMock()
    monkeypatch.setattr(title_app.state.background_title_coordinator, "_generator", generator)
    response = await title_client.post(
        f"/v1/sessions/{child.id}/agent-title", json={"title": "Change child address"}
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"renamed": False, "title": None, "reason": "not_top_level"}
    generator.assert_not_awaited()


@pytest.mark.parametrize(
    "level,expected_status", [(None, 404), (LEVEL_READ, 403), (LEVEL_EDIT, 200)]
)
async def test_agent_rename_requires_edit_access(
    title_app: FastAPI,
    title_client: httpx.AsyncClient,
    title_session: Conversation,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    level: int | None,
    expected_status: int,
) -> None:
    if level is not None:
        SqlAlchemyPermissionStore(db_uri).grant(
            "collaborator@example.test", title_session.id, level
        )
    generator = AsyncMock(return_value="sep-13-PR-123-resolve-conflict")
    monkeypatch.setattr(title_app.state.background_title_coordinator, "_generator", generator)
    response = await title_client.post(
        f"/v1/sessions/{title_session.id}/agent-title",
        json={"title": "Resolve PR 123 conflict"},
        headers={"X-Forwarded-Email": "collaborator@example.test"},
    )
    assert response.status_code == expected_status, response.text
    assert generator.await_count == (1 if expected_status == 200 else 0)

"""The routing relay endpoints are mutating routes, so they gate on write.

Both hook relays (``route-turn`` and ``route-subagent``) change the session:
route-turn pins ``model_override`` for every remaining turn, and both persist a
``routing_decision`` transcript item. They were gated ``LEVEL_READ``, which let
anyone holding a read-only share repin somebody else's model. The level they
must require is the one ``POST /v1/sessions/{id}/events`` requires.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import LEVEL_EDIT, LEVEL_READ
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests.server.conftest import ControllableMockClient

pytestmark = pytest.mark.asyncio

READER = "reader@example.com"
EDITOR = "editor@example.com"


def _session_with_grants(db_uri: str) -> str:
    """Create a conversation with one read-only and one editing grantee.

    :param db_uri: SQLite URI for the per-test database.
    :returns: The conversation id.
    """
    conversation = SqlAlchemyConversationStore(db_uri).create_conversation()
    perm_store = SqlAlchemyPermissionStore(db_uri)
    for user, level in ((EDITOR, LEVEL_EDIT), (READER, LEVEL_READ)):
        perm_store.ensure_user(user)
        perm_store.grant(user, conversation.id, level)
    return conversation.id


@pytest.fixture()
def auth_app(runtime_init: None, db_uri: str, tmp_path: Path) -> FastAPI:
    """App with a permission store, so ``X-Forwarded-Email`` is authoritative.

    :param runtime_init: Runtime bootstrap fixture.
    :param db_uri: Per-test SQLite URI.
    :param tmp_path: Pytest temp dir for artifacts.
    :returns: The auth-enabled app.
    """
    from omnigent.server.auth import UnifiedAuthProvider

    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        permission_store=SqlAlchemyPermissionStore(db_uri),
        auth_provider=UnifiedAuthProvider(source="header"),
    )


@pytest_asyncio.fixture()
async def auth_client(
    auth_app: FastAPI,
    mock_llm: ControllableMockClient,
    tmp_path: Path,
) -> AsyncIterator[httpx.AsyncClient]:
    """Async client wired to the auth-enabled app.

    :param auth_app: The auth-enabled app.
    :param mock_llm: Controllable mock LLM, released on teardown.
    :param tmp_path: Pytest temp dir for the harness process manager.
    :yields: A ready-to-use client.
    """
    from omnigent.runtime import set_harness_process_manager
    from omnigent.runtime.harnesses.process_manager import HarnessProcessManager

    pm = HarnessProcessManager(tmp_parent=tmp_path / "harness_pm")
    await pm.start()
    set_harness_process_manager(pm)
    transport = httpx.ASGITransport(app=auth_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    mock_llm.release_all()
    set_harness_process_manager(None)
    await pm.shutdown()


@pytest.mark.parametrize(
    ("route", "body"),
    [
        (
            "route-turn",
            {"harness": "codex-native", "prompt": "audit the router", "model": "gpt-5.6-sol"},
        ),
        ("route-subagent", {"harness": "codex-native", "prompt": "review the diff"}),
    ],
)
async def test_a_read_only_grantee_cannot_reach_a_routing_relay(
    auth_client: httpx.AsyncClient,
    db_uri: str,
    route: str,
    body: dict[str, str],
) -> None:
    """Read access is not enough to repin a session's model."""
    session_id = _session_with_grants(db_uri)

    resp = await auth_client.post(
        f"/v1/sessions/{session_id}/hooks/{route}",
        json=body,
        headers={"X-Forwarded-Email": READER},
    )

    assert resp.status_code == 403, (
        f"{route} accepted a LEVEL_READ caller ({resp.status_code}); it writes "
        "model_override and a decision item, so it must require LEVEL_EDIT."
    )


@pytest.mark.parametrize(
    ("route", "body"),
    [
        (
            "route-turn",
            {"harness": "codex-native", "prompt": "audit the router", "model": "gpt-5.6-sol"},
        ),
        ("route-subagent", {"harness": "codex-native", "prompt": "review the diff"}),
    ],
)
async def test_an_editing_grantee_still_reaches_a_routing_relay(
    auth_client: httpx.AsyncClient,
    db_uri: str,
    route: str,
    body: dict[str, str],
) -> None:
    """The tightened gate is exactly the events path's, not stricter.

    The session here has routing off, so both relays answer with an unrouted
    verdict — which is the point: the level check passed.
    """
    session_id = _session_with_grants(db_uri)

    resp = await auth_client.post(
        f"/v1/sessions/{session_id}/hooks/{route}",
        json=body,
        headers={"X-Forwarded-Email": EDITOR},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["action"] == "allow"


async def test_the_route_turn_relay_keeps_the_rationale_off_info(
    auth_client: httpx.AsyncClient,
    db_uri: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    The rationale paraphrases the user's prompt, so it stays off INFO.

    ``omnigent.server.smart_routing`` states that invariant at each of its own
    three log sites and demotes the rationale to DEBUG; this relay logged it at
    INFO, putting a paraphrase of the prompt into any deployment running at the
    default level. The non-prompt fields stay at INFO.
    """
    import logging

    session_id = _session_with_grants(db_uri)
    logger_name = "omnigent.server.routes.sessions"

    with caplog.at_level(logging.INFO, logger=logger_name):
        resp = await auth_client.post(
            f"/v1/sessions/{session_id}/hooks/route-turn",
            json={
                "harness": "codex-native",
                "prompt": "rewrite the billing reconciliation job",
                "model": "gpt-5.6-sol",
            },
            headers={"X-Forwarded-Email": EDITOR},
        )
    assert resp.status_code == 200, resp.text
    rationale = resp.json()["rationale"]
    assert rationale

    messages = [
        r.getMessage()
        for r in caplog.records
        if r.name == logger_name and r.levelno >= logging.INFO and "route-turn" in r.getMessage()
    ]
    assert messages, "the relay must still log the decision at INFO"
    assert not any(rationale in message for message in messages)
    # The non-prompt fields are still there, so the decision stays traceable.
    assert any("codex-native" in message and "action=allow" in message for message in messages)

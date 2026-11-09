"""Integration tests: only the owner may mutate a session-scoped agent.

Exercises the full request → store pipeline against real SQLite-backed
stores with a header auth provider, so the owner-only gate on the agent
bundle PUT and the MCP-server CRUD routes is verified end to end. The core
regression: a user the session was *shared* with (editor), or who *reused*
the agent in their own session, must not be able to replace agent code that
later runs with the runner's authority.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import LEVEL_EDIT, LEVEL_MANAGE, LEVEL_READ
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests.server.helpers import build_agent_bundle, create_test_agent

pytestmark = pytest.mark.asyncio

ALICE = "alice@example.com"
BOB = "bob@example.com"
ADMIN = "root@example.com"


@pytest.fixture(autouse=True)
def _multi_user(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force multi-user semantics for the owner gate.

    The shared test harness sets ``OMNIGENT_LOCAL_SINGLE_USER`` (loopback
    single-user), which intentionally bypasses the owner check because there
    is no second identity. These tests model a deployed multi-user server, so
    turn the marker off for the modules that read it.
    """
    from omnigent.server.routes import _auth_helpers

    monkeypatch.setattr(_auth_helpers, "local_single_user_enabled", lambda: False)


@pytest.fixture()
def auth_app(runtime_init: None, db_uri: str, tmp_path: Path) -> FastAPI:
    """App with a permission store + header auth provider enabled."""
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


async def _share_editor(
    client: httpx.AsyncClient,
    session_id: str,
    owner: str,
    grantee: str,
    level: int = LEVEL_EDIT,
) -> None:
    """Owner shares a session with ``grantee`` at the requested level."""
    resp = await client.put(
        f"/v1/sessions/{session_id}/permissions",
        json={"user_id": grantee, "level": level},
        headers={"X-Forwarded-Email": owner},
    )
    assert resp.status_code in (200, 204), resp.text


async def test_owner_can_update_bundle_and_mcp(auth_client: httpx.AsyncClient) -> None:
    """The creating user may replace the bundle and edit MCP servers."""
    agent = await create_test_agent(auth_client, name="owned-agent", user=ALICE)
    session_id = agent["_session_id"]
    headers = {"X-Forwarded-Email": ALICE}

    config_path = f"/v1/agents/{agent['id']}/config"
    config = await auth_client.get(config_path, headers=headers)
    assert config.status_code == 200, config.text
    patched = await auth_client.patch(
        f"/v1/agents/{agent['id']}",
        json={"description": "PATCH override"},
        headers={**headers, "If-Match": config.headers["etag"]},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["description"] == "PATCH override"

    put = await auth_client.put(
        f"/v1/sessions/{session_id}/agent",
        files={
            "bundle": (
                "agent.tar.gz",
                build_agent_bundle(name="owned-agent", description="v2"),
                "application/gzip",
            )
        },
        headers=headers,
    )
    assert put.status_code == 200, put.text
    assert put.json()["description"] == "v2"
    saved = await auth_client.get(config_path, headers=headers)
    assert saved.status_code == 200, saved.text
    assert saved.json()["description"] == "v2"

    mcp = await auth_client.post(
        f"/v1/sessions/{session_id}/agent/mcp-servers",
        json={"name": "gh", "transport": "http", "url": "https://example.com/sse"},
        headers=headers,
    )
    assert mcp.status_code == 200, mcp.text


@pytest.mark.parametrize("level", [LEVEL_READ, LEVEL_EDIT, LEVEL_MANAGE])
@pytest.mark.parametrize(
    "change",
    [
        {"terminal_launch_args": []},
        {"permission_mode": "default"},
        {"labels": {"omnigent.codex_native.bypass_sandbox": "1"}},
        {"labels": {"omnigent.codex_native.bypass_sandbox": "0"}},
    ],
    ids=["launch-args", "permission-mode", "sandbox-bypass", "sandbox-enforced"],
)
async def test_only_owner_can_update_terminal_launch_args(
    auth_client: httpx.AsyncClient, level: int, change: dict[str, object]
) -> None:
    agent = await create_test_agent(auth_client, name="owned-terminal", user=ALICE)
    session_id = agent["_session_id"]
    path = f"/v1/sessions/{session_id}"
    owner = {"X-Forwarded-Email": ALICE}
    shared = {"X-Forwarded-Email": BOB}
    response = await auth_client.put(
        f"{path}/permissions", json={"user_id": BOB, "level": level}, headers=owner
    )
    assert response.status_code in (200, 204), response.text
    response = await auth_client.patch(
        path, json={"terminal_launch_args": ["--verbose"], "title": "Owner title"}, headers=owner
    )
    assert response.status_code == 200, response.text
    response = await auth_client.patch(
        path, json={**change, "title": "Changed title"}, headers=shared
    )
    assert response.status_code == 403, response.text
    response = await auth_client.get(path, headers=owner)
    assert response.status_code == 200, response.text
    assert response.json()["terminal_launch_args"] == ["--verbose"]
    assert response.json()["title"] == "Owner title"
    assert "omnigent.codex_native.bypass_sandbox" not in response.json()["labels"]
    response = await auth_client.patch(path, json={"title": "Changed title"}, headers=shared)
    assert response.status_code == (403 if level == LEVEL_READ else 200), response.text
    response = await auth_client.patch(
        path, json={"title": "Null preserves args", "terminal_launch_args": None}, headers=shared
    )
    assert response.status_code == (403 if level == LEVEL_READ else 200), response.text
    response = await auth_client.get(path, headers=owner)
    assert response.json()["terminal_launch_args"] == ["--verbose"]
    response = await auth_client.patch(path, json={"terminal_launch_args": []}, headers=owner)
    assert response.status_code == 200, response.text
    response = await auth_client.get(path, headers=owner)
    assert response.json()["terminal_launch_args"] == []
    for value in ("1", "0"):
        response = await auth_client.patch(
            path,
            json={"labels": {"omnigent.codex_native.bypass_sandbox": value}},
            headers=owner,
        )
        assert response.status_code == 200, response.text
        response = await auth_client.get(path, headers=owner)
        assert response.json()["labels"]["omnigent.codex_native.bypass_sandbox"] == value


@pytest.mark.parametrize(
    "level", [LEVEL_READ, LEVEL_EDIT, LEVEL_MANAGE], ids=["read", "edit", "manage"]
)
async def test_only_owner_can_change_codex_approval_mode(
    auth_client: httpx.AsyncClient,
    db_uri: str,
    level: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Approval mode needs ownership even in a mixed PATCH on a Codex session."""
    from omnigent.server.routes._sessions.helpers import _RunnerForwardResult

    forwarded: list[dict[str, object]] = []

    async def forward(session_id: str, router: object, event: dict[str, object], **kwargs: object):
        forwarded.append(event)
        return _RunnerForwardResult(200, '{"approval_mode": "full-access"}')

    # Isolate the external runner; keep authorization, persistence, and mode confirmation real.
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._forward_session_change_to_runner", forward
    )
    agent = await create_test_agent(auth_client, name="shared-approval", user=ALICE)
    session_id = agent["_session_id"]
    path = f"/v1/sessions/{session_id}"
    # Stamp the persisted labels used by the native Codex wrapper, as the
    # create route does for its built-in agent.
    SqlAlchemyConversationStore(db_uri).set_labels(
        session_id,
        {"omnigent.ui": "terminal", "omnigent.wrapper": "codex-native-ui"},
    )
    await _share_editor(auth_client, session_id, ALICE, BOB, level)
    owner = {"X-Forwarded-Email": ALICE}
    shared = {"X-Forwarded-Email": BOB}
    before = await auth_client.get(path, headers=owner)
    assert before.status_code == 200, before.text
    assert before.json()["labels"]["omnigent.wrapper"] == "codex-native-ui"
    original_title = before.json()["title"]

    response = await auth_client.patch(
        path,
        json={"approval_mode": "full-access", "title": "Unauthorized title"},
        headers=shared,
    )
    assert response.status_code == 403, response.text

    after_denial = await auth_client.get(path, headers=owner)
    assert after_denial.status_code == 200, after_denial.text
    assert after_denial.json()["title"] == original_title
    assert "omnigent.codex_native.approval_mode" not in after_denial.json()["labels"]
    assert forwarded == []

    owner_update = await auth_client.patch(
        path,
        json={"approval_mode": "full-access", "title": "Owner title"},
        headers=owner,
    )
    assert owner_update.status_code == 200, owner_update.text
    assert owner_update.json()["title"] == "Owner title"
    assert owner_update.json()["labels"]["omnigent.codex_native.approval_mode"] == "full-access"
    assert forwarded == [{"type": "codex_approval_mode_change", "approval_mode": "full-access"}]


async def test_shared_editor_cannot_update_bundle(auth_client: httpx.AsyncClient) -> None:
    """An editor the session was shared with is refused (403), not allowed."""
    agent = await create_test_agent(auth_client, name="shared-agent", user=ALICE)
    session_id = agent["_session_id"]
    await _share_editor(auth_client, session_id, ALICE, BOB)

    put = await auth_client.put(
        f"/v1/sessions/{session_id}/agent",
        files={
            "bundle": (
                "agent.tar.gz",
                build_agent_bundle(name="shared-agent", description="pwn"),
                "application/gzip",
            )
        },
        headers={"X-Forwarded-Email": BOB},
    )
    assert put.status_code == 403, put.text


async def test_shared_editor_cannot_edit_mcp_servers(auth_client: httpx.AsyncClient) -> None:
    """An editor cannot inject an MCP server (arbitrary spawn command)."""
    agent = await create_test_agent(auth_client, name="shared-mcp", user=ALICE)
    session_id = agent["_session_id"]
    await _share_editor(auth_client, session_id, ALICE, BOB)

    resp = await auth_client.post(
        f"/v1/sessions/{session_id}/agent/mcp-servers",
        json={"name": "pwn", "transport": "stdio", "command": "/bin/sh", "args": ["-c", "id"]},
        headers={"X-Forwarded-Email": BOB},
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.parametrize(
    "hook",
    [
        "permission-request",
        "codex-elicitation-request",
        "antigravity-elicitation-request",
        "cursor-permission-request",
        "native-permission-request",
    ],
)
async def test_read_only_collaborator_cannot_publish_native_hooks(
    auth_client: httpx.AsyncClient, hook: str
) -> None:
    agent = await create_test_agent(auth_client, name="native-hooks", user=ALICE)
    path = f"/v1/sessions/{agent['_session_id']}"
    owner = {"X-Forwarded-Email": ALICE}
    collaborator = {"X-Forwarded-Email": BOB}
    for level, expected in [(LEVEL_READ, 403), (LEVEL_EDIT, 400)]:
        response = await auth_client.put(
            f"{path}/permissions", json={"user_id": BOB, "level": level}, headers=owner
        )
        assert response.status_code in (200, 204), response.text
        # An empty body reaches validation only after mutation authority is checked.
        response = await auth_client.post(f"{path}/hooks/{hook}", json={}, headers=collaborator)
        assert response.status_code == expected, response.text


@pytest.mark.parametrize("child", [False, True])
async def test_policy_evaluation_uses_inherited_edit_authority(
    auth_client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    child: bool,
) -> None:
    from tests.server.helpers import policy_tool_call_request
    from tests.server.integration.test_sessions_endpoints import _capture_published
    from tests.server.integration.test_sessions_policy_evaluate import (
        _drain_elicitation_id,
        _patch_default_policies,
    )

    agent = await create_test_agent(auth_client, name="policy-authority", user=ALICE)
    parent_id = agent["_session_id"]
    session_id = parent_id
    if child:
        session_id = (
            SqlAlchemyConversationStore(db_uri)
            .create_conversation(agent_id=agent["id"], parent_conversation_id=parent_id)
            .id
        )
    owner = {"X-Forwarded-Email": ALICE}
    collaborator = {"X-Forwarded-Email": BOB}
    _patch_default_policies(
        monkeypatch,
        "tests.server.integration.test_sessions_policy_evaluate._ask_for_bash",
    )
    response = await auth_client.put(
        f"/v1/sessions/{parent_id}/permissions",
        json={"user_id": BOB, "level": LEVEL_READ},
        headers=owner,
    )
    assert response.status_code in (200, 204), response.text
    published = []
    with monkeypatch.context() as observer:
        _capture_published(observer, published)
        response = await asyncio.wait_for(
            auth_client.post(
                f"/v1/sessions/{session_id}/policies/evaluate",
                json=policy_tool_call_request("Bash"),
                headers=collaborator,
            ),
            timeout=5,
        )
    assert response.status_code == 200, response.text
    assert response.json()["result"] == "POLICY_ACTION_ASK"
    assert not published

    response = await auth_client.put(
        f"/v1/sessions/{parent_id}/permissions",
        json={"user_id": BOB, "level": LEVEL_EDIT},
        headers=owner,
    )
    assert response.status_code in (200, 204), response.text
    drain = asyncio.create_task(_drain_elicitation_id(session_id))
    await asyncio.sleep(0.05)
    evaluation = asyncio.create_task(
        auth_client.post(
            f"/v1/sessions/{session_id}/policies/evaluate",
            json=policy_tool_call_request("Bash"),
            headers=collaborator,
        )
    )
    try:
        elicitation_id = await asyncio.wait_for(drain, timeout=5)
        verdict = await auth_client.post(
            f"/v1/sessions/{session_id}/elicitations/{elicitation_id}/resolve",
            json={"action": "accept"},
            headers=owner,
        )
        assert verdict.status_code == 202, verdict.text
        response = await asyncio.wait_for(evaluation, timeout=5)
        assert response.status_code == 200, response.text
        assert response.json()["result"] == "POLICY_ACTION_ALLOW"
    finally:
        for task in (drain, evaluation):
            task.cancel()
        await asyncio.gather(drain, evaluation, return_exceptions=True)


async def test_reuse_then_patch_never_reaches_the_owners_agent(
    auth_client: httpx.AsyncClient,
) -> None:
    """Binding the owner's agent into your own session does not let you patch it.

    This is the core exploit: BOB is shared ALICE's session, reuses her
    agent in a brand-new session of his own (becoming its owner), then tries
    to replace the shared agent's code. His session runs on his own copy, so
    the patch changes only that copy.
    """
    agent = await create_test_agent(auth_client, name="reused-agent", user=ALICE)
    alice_session = agent["_session_id"]
    await _share_editor(auth_client, alice_session, ALICE, BOB)

    # BOB reuses ALICE's agent in his own new session.
    reuse = await auth_client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"]},
        headers={"X-Forwarded-Email": BOB},
    )
    assert reuse.status_code == 201, reuse.text
    bob_session = reuse.json()["id"]
    assert reuse.json()["agent_id"] != agent["id"], "BOB runs on his own copy"

    put = await auth_client.put(
        f"/v1/sessions/{bob_session}/agent",
        files={
            "bundle": (
                "agent.tar.gz",
                build_agent_bundle(name="reused-agent", description="pwn"),
                "application/gzip",
            )
        },
        headers={"X-Forwarded-Email": BOB},
    )
    assert put.status_code == 200, put.text
    alices_agent = await auth_client.get(
        f"/v1/sessions/{alice_session}/agent", headers={"X-Forwarded-Email": ALICE}
    )
    assert alices_agent.json()["id"] == agent["id"]
    assert alices_agent.json()["description"] != "pwn"


async def test_legacy_null_owner_is_admin_only(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A legacy (NULL created_by) agent is admin-only to mutate.

    Simulates a pre-migration agent by clearing created_by. Not even the
    original session owner (ALICE) may mutate it, only an admin; a reuser
    (BOB) runs on his own copy, so his edits never reach it. The original
    owner regains a mutable agent by re-uploading (which creates a fresh
    owned row), which the other tests already cover.
    """
    import sqlalchemy as sa

    from omnigent.db.utils import get_or_create_engine

    agent = await create_test_agent(auth_client, name="legacy-agent", user=ALICE)
    alice_session = agent["_session_id"]

    # Make it a legacy row: clear the recorded owner.
    engine = get_or_create_engine(db_uri)
    with engine.begin() as conn:
        conn.execute(
            sa.text("UPDATE agents SET created_by = NULL WHERE id = :id"),
            {"id": bytes.fromhex(agent["id"])},
        )

    await _share_editor(auth_client, alice_session, ALICE, BOB)
    reuse = await auth_client.post(
        "/v1/sessions", json={"agent_id": agent["id"]}, headers={"X-Forwarded-Email": BOB}
    )
    assert reuse.status_code == 201, reuse.text
    bob_session = reuse.json()["id"]

    bundle_files = {
        "bundle": (
            "agent.tar.gz",
            build_agent_bundle(name="legacy-agent", description="v2"),
            "application/gzip",
        )
    }

    # BOB (reuser) edits only his own copy.
    assert reuse.json()["agent_id"] != agent["id"]
    bob_put = await auth_client.put(
        f"/v1/sessions/{bob_session}/agent",
        files=bundle_files,
        headers={"X-Forwarded-Email": BOB},
    )
    assert bob_put.status_code == 200, bob_put.text

    # Even ALICE (owning-session owner) is refused: a NULL row is admin-only.
    alice_put = await auth_client.put(
        f"/v1/sessions/{alice_session}/agent",
        files=bundle_files,
        headers={"X-Forwarded-Email": ALICE},
    )
    assert alice_put.status_code == 403, alice_put.text

    # An admin may update it.
    SqlAlchemyPermissionStore(db_uri).ensure_user(ADMIN, is_admin=True)
    admin_put = await auth_client.put(
        f"/v1/sessions/{alice_session}/agent",
        files=bundle_files,
        headers={"X-Forwarded-Email": ADMIN},
    )
    assert admin_put.status_code == 200, admin_put.text


async def test_admin_can_update_any_agent(auth_client: httpx.AsyncClient, db_uri: str) -> None:
    """A workspace admin may replace an agent they did not create."""
    agent = await create_test_agent(auth_client, name="admin-agent", user=ALICE)
    session_id = agent["_session_id"]
    SqlAlchemyPermissionStore(db_uri).ensure_user(ADMIN, is_admin=True)

    put = await auth_client.put(
        f"/v1/sessions/{session_id}/agent",
        files={
            "bundle": (
                "agent.tar.gz",
                build_agent_bundle(name="admin-agent", description="ops"),
                "application/gzip",
            )
        },
        headers={"X-Forwarded-Email": ADMIN},
    )
    assert put.status_code == 200, put.text


@pytest.mark.parametrize("level", [LEVEL_EDIT, LEVEL_MANAGE])
@pytest.mark.parametrize(
    "event,args",
    [
        (
            {
                "type": "external_codex_approval_mode_change",
                "data": {"terminal_launch_args": ["--sandbox", "read-only"]},
            },
            ["--sandbox", "read-only"],
        ),
        (
            {
                "type": "external_permission_mode_change",
                "data": {"permission_mode": "default", "initial_observation": False},
            },
            ["--permission-mode", "default"],
        ),
    ],
    ids=["codex", "claude"],
)
async def test_native_approval_changes_require_owner_authority(
    auth_client: httpx.AsyncClient,
    level: int,
    event: dict[str, object],
    args: list[str],
) -> None:
    agent = await create_test_agent(auth_client, name="native-settings", user=ALICE)
    session_id = agent["_session_id"]
    path = f"/v1/sessions/{session_id}"
    owner = {"X-Forwarded-Email": ALICE}
    response = await auth_client.put(
        f"{path}/permissions", json={"user_id": BOB, "level": level}, headers=owner
    )
    assert response.status_code in (200, 204), response.text
    initial = (await auth_client.get(path, headers=owner)).json()["terminal_launch_args"]
    response = await auth_client.post(
        f"{path}/events", json=event, headers={"X-Forwarded-Email": BOB}
    )
    assert response.status_code == 403, response.text
    assert (await auth_client.get(path, headers=owner)).json()["terminal_launch_args"] == initial
    response = await auth_client.post(f"{path}/events", json=event, headers=owner)
    assert response.status_code == 202, response.text
    assert (await auth_client.get(path, headers=owner)).json()["terminal_launch_args"] == args

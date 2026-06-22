"""End-to-end style integration tests for the comments REST API.

Covers gaps not exercised by ``test_comments_routes.py``:

* Full CRUD lifecycle in a single test (create, list, get-by-filter,
  update body+status, delete, verify gone).
* ``POST /v1/sessions/{id}/comments/send`` — formatted message
  includes per-file grouping, anchor content, and offset range.
* ``GET /v1/sessions/{id}/comments?path=…`` path filtering.
* 404 on delete of nonexistent comment.
* 404 on comment operations against a nonexistent session (auth mode).
* PATCH with both body and status in one request.
* ``GET /v1/sessions`` includes ``comments_count`` and
  ``comments_updated_at`` from the comments fingerprint.

Uses the same ``auth_app`` / ``auth_client`` fixture pattern as the
existing tests (real ``SqlAlchemyPermissionStore`` so
``UnifiedAuthProvider`` is active).
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
from omnigent.server.auth import LEVEL_EDIT
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests.unit.server.conftest import ControllableMockClient

# ── Helpers ──────────────────────────────────────────────────────────────────

ALICE = "alice@example.com"


def _seed_session(db_uri: str, *, with_agent: bool = False) -> str:
    """Create a conversation and grant Alice edit access.

    :param db_uri: Per-test SQLite URI.
    :param with_agent: If ``True``, create an agent row so the session
        appears in ``GET /v1/sessions`` (which filters ``agent_id IS NOT NULL``).
    :returns: The session id.
    """
    conv_store = SqlAlchemyConversationStore(db_uri)
    agent_id: str | None = None
    if with_agent:
        agent_store = SqlAlchemyAgentStore(db_uri)
        agent = agent_store.create(
            agent_id="ag_test",
            name="test-agent",
            bundle_location="fake/bundle",
        )
        agent_id = agent.id
    conv = conv_store.create_conversation(agent_id=agent_id)
    perm_store = SqlAlchemyPermissionStore(db_uri)
    perm_store.ensure_user(ALICE)
    perm_store.grant(ALICE, conv.id, LEVEL_EDIT)
    return conv.id


pytestmark = pytest.mark.asyncio


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def auth_app(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
) -> FastAPI:
    """App with permission store enabled (auth active)."""
    from omnigent.server.auth import UnifiedAuthProvider

    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(
            artifact_store=artifact_store,
            cache_dir=tmp_path / "cache",
        ),
        comment_store=SqlAlchemyCommentStore(db_uri),
        permission_store=SqlAlchemyPermissionStore(db_uri),
        auth_provider=UnifiedAuthProvider(source="header"),
    )


@pytest_asyncio.fixture()
async def auth_client(
    auth_app: FastAPI,
    mock_llm: ControllableMockClient,
    tmp_path: Path,
) -> AsyncIterator[httpx.AsyncClient]:
    """Async HTTP client wired to the auth-enabled app."""
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


# ── Tests ────────────────────────────────────────────────────────────────────


async def test_full_crud_lifecycle(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Create -> list -> filter -> update body+status -> delete -> verify gone."""
    session_id = _seed_session(db_uri)
    headers = {"X-Forwarded-Email": ALICE}

    # ── Create two comments on different paths ───────────────────────
    c1_resp = await auth_client.post(
        f"/v1/sessions/{session_id}/comments",
        json={
            "path": "src/foo.py",
            "body": "Fix this function",
            "start_index": 0,
            "end_index": 20,
        },
        headers=headers,
    )
    assert c1_resp.status_code == 200, c1_resp.text
    c1 = c1_resp.json()
    assert c1["status"] == "draft"
    assert c1["path"] == "src/foo.py"

    c2_resp = await auth_client.post(
        f"/v1/sessions/{session_id}/comments",
        json={
            "path": "src/bar.py",
            "body": "Add a type hint",
            "start_index": 5,
            "end_index": 15,
        },
        headers=headers,
    )
    assert c2_resp.status_code == 200, c2_resp.text
    c2 = c2_resp.json()

    # ── List all ─────────────────────────────────────────────────────
    list_resp = await auth_client.get(
        f"/v1/sessions/{session_id}/comments",
        headers=headers,
    )
    assert list_resp.status_code == 200
    assert len(list_resp.json()) == 2

    # ── Filter by path ───────────────────────────────────────────────
    filtered = await auth_client.get(
        f"/v1/sessions/{session_id}/comments",
        params={"path": "src/foo.py"},
        headers=headers,
    )
    assert filtered.status_code == 200
    filtered_comments = filtered.json()
    assert len(filtered_comments) == 1
    assert filtered_comments[0]["id"] == c1["id"]

    # ── Update both body and status in one PATCH ─────────────────────
    patch_resp = await auth_client.patch(
        f"/v1/sessions/{session_id}/comments/{c1['id']}",
        json={"body": "Updated function comment", "status": "addressed"},
        headers=headers,
    )
    assert patch_resp.status_code == 200
    patched = patch_resp.json()
    assert patched["body"] == "Updated function comment"
    assert patched["status"] == "addressed"

    # ── Delete ───────────────────────────────────────────────────────
    del_resp = await auth_client.delete(
        f"/v1/sessions/{session_id}/comments/{c1['id']}",
        headers=headers,
    )
    assert del_resp.status_code == 200
    assert del_resp.json()["deleted"] is True

    # ── Verify deleted ───────────────────────────────────────────────
    remaining = await auth_client.get(
        f"/v1/sessions/{session_id}/comments",
        headers=headers,
    )
    assert remaining.status_code == 200
    ids = [c["id"] for c in remaining.json()]
    assert c1["id"] not in ids
    assert c2["id"] in ids


async def test_delete_nonexistent_comment_returns_404(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Deleting a comment that does not exist returns 404."""
    session_id = _seed_session(db_uri)
    headers = {"X-Forwarded-Email": ALICE}

    resp = await auth_client.delete(
        f"/v1/sessions/{session_id}/comments/nonexistent-id",
        headers=headers,
    )
    assert resp.status_code == 404


async def test_comment_on_nonexistent_session_returns_404(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Operations on a session the user has no grant for return 403/404."""
    headers = {"X-Forwarded-Email": ALICE}

    resp = await auth_client.post(
        "/v1/sessions/conv_does_not_exist/comments",
        json={
            "path": "src/foo.py",
            "body": "Hello",
            "start_index": 0,
            "end_index": 5,
        },
        headers=headers,
    )
    # The permission check fires before the comment store — the user
    # has no grant for this session, so they get a 404.
    assert resp.status_code == 404


async def test_send_formats_multifile_message_with_anchors(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """The send endpoint groups by file, includes anchors and offsets."""
    session_id = _seed_session(db_uri)
    headers = {"X-Forwarded-Email": ALICE}

    # Comment with anchor_content on file A
    c1_resp = await auth_client.post(
        f"/v1/sessions/{session_id}/comments",
        json={
            "path": "src/alpha.py",
            "body": "Rename variable",
            "start_index": 10,
            "end_index": 25,
            "anchor_content": "old_var_name",
        },
        headers=headers,
    )
    assert c1_resp.status_code == 200, c1_resp.text
    c1 = c1_resp.json()

    # Comment without anchor_content on file B
    c2_resp = await auth_client.post(
        f"/v1/sessions/{session_id}/comments",
        json={
            "path": "src/beta.py",
            "body": "Add docstring",
            "start_index": 0,
            "end_index": 8,
        },
        headers=headers,
    )
    assert c2_resp.status_code == 200, c2_resp.text
    c2 = c2_resp.json()

    # Comment on file A again (should group with the first)
    c3_resp = await auth_client.post(
        f"/v1/sessions/{session_id}/comments",
        json={
            "path": "src/alpha.py",
            "body": "Fix indentation",
            "start_index": 50,
            "end_index": 60,
        },
        headers=headers,
    )
    assert c3_resp.status_code == 200, c3_resp.text
    c3 = c3_resp.json()

    send_resp = await auth_client.post(
        f"/v1/sessions/{session_id}/comments/send",
        json={"comment_ids": [c1["id"], c2["id"], c3["id"]]},
        headers=headers,
    )
    assert send_resp.status_code == 200
    payload = send_resp.json()

    msg = payload["formatted_message"]

    # File grouping: alpha.py comes before beta.py alphabetically
    alpha_pos = msg.index("src/alpha.py")
    beta_pos = msg.index("src/beta.py")
    assert alpha_pos < beta_pos, "Comments should be grouped alphabetically by file path"

    # Anchor content appears quoted in the message
    assert '"old_var_name"' in msg, "Anchor content should appear quoted in the message"

    # Both bodies present
    assert "Rename variable" in msg
    assert "Add docstring" in msg
    assert "Fix indentation" in msg

    # Offset ranges present
    assert "10" in msg and "25" in msg, "Offset range for c1 should appear"

    # All comment ids echoed
    assert set(payload["sent_comment_ids"]) == {c1["id"], c2["id"], c3["id"]}

    # All three comments are now addressed
    list_resp = await auth_client.get(
        f"/v1/sessions/{session_id}/comments",
        headers=headers,
    )
    statuses = {c["id"]: c["status"] for c in list_resp.json()}
    for cid in [c1["id"], c2["id"], c3["id"]]:
        assert statuses[cid] == "addressed"


async def test_send_nonexistent_comment_returns_404(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Sending a comment id that doesn't exist returns 404."""
    session_id = _seed_session(db_uri)
    headers = {"X-Forwarded-Email": ALICE}

    resp = await auth_client.post(
        f"/v1/sessions/{session_id}/comments/send",
        json={"comment_ids": ["nonexistent-id"]},
        headers=headers,
    )
    assert resp.status_code == 404


async def test_session_list_includes_comments_fingerprint(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """GET /v1/sessions includes comments_count and comments_updated_at."""
    session_id = _seed_session(db_uri, with_agent=True)
    headers = {"X-Forwarded-Email": ALICE}

    # Before any comments: fingerprint should show zero
    list_resp = await auth_client.get("/v1/sessions", headers=headers)
    assert list_resp.status_code == 200
    items = list_resp.json()["data"]
    target = [s for s in items if s["id"] == session_id]
    assert len(target) == 1
    assert target[0]["comments_count"] == 0
    assert target[0].get("comments_updated_at") is None

    # Add a comment
    add_resp = await auth_client.post(
        f"/v1/sessions/{session_id}/comments",
        json={
            "path": "src/main.py",
            "body": "Check this",
            "start_index": 0,
            "end_index": 10,
        },
        headers=headers,
    )
    assert add_resp.status_code == 200
    comment = add_resp.json()

    # After adding: count=1 and updated_at is set
    list_resp2 = await auth_client.get("/v1/sessions", headers=headers)
    items2 = list_resp2.json()["data"]
    target2 = [s for s in items2 if s["id"] == session_id]
    assert len(target2) == 1
    assert target2[0]["comments_count"] == 1
    assert target2[0]["comments_updated_at"] is not None
    assert target2[0]["comments_updated_at"] == comment["updated_at"]

    # Add a second comment and verify count bumps
    add_resp2 = await auth_client.post(
        f"/v1/sessions/{session_id}/comments",
        json={
            "path": "src/main.py",
            "body": "And this too",
            "start_index": 20,
            "end_index": 30,
        },
        headers=headers,
    )
    assert add_resp2.status_code == 200, add_resp2.text
    list_resp3 = await auth_client.get("/v1/sessions", headers=headers)
    items3 = list_resp3.json()["data"]
    target3 = [s for s in items3 if s["id"] == session_id]
    assert target3[0]["comments_count"] == 2

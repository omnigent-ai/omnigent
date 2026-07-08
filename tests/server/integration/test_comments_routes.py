"""Integration tests for the comments routes with auth active.

Uses a real ``SqlAlchemyPermissionStore`` so ``UnifiedAuthProvider``
is enabled and ``X-Forwarded-Email`` headers are respected.
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
from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests.server.conftest import ControllableMockClient

# ── Helpers ──────────────────────────────────────────────────────────────────


def _seed_session_with_grants(
    db_uri: str,
    grants: dict[str, int],
) -> str:
    """Create a bare conversation row and seed permission grants for it.

    The ``session_permissions`` table has a FK to ``conversations``, so the
    conversation row must exist before any grant can be inserted.

    :param db_uri: SQLite URI for the per-test database.
    :param grants: Mapping of ``{user_email: level}`` to grant on the new
        session, e.g. ``{"alice@example.com": LEVEL_EDIT}``.
    :returns: The newly created conversation ID, e.g. ``"conv_abc123"``.
    """
    conv_store = SqlAlchemyConversationStore(db_uri)
    conversation = conv_store.create_conversation()
    perm_store = SqlAlchemyPermissionStore(db_uri)
    for user_email, level in grants.items():
        perm_store.ensure_user(user_email)
        perm_store.grant(user_email, conversation.id, level)
    return conversation.id


pytestmark = pytest.mark.asyncio


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def auth_app(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
) -> FastAPI:
    """App with ``permission_store`` enabled so auth is active on comments routes.

    :param runtime_init: Fixture that initializes the runtime with a mock LLM.
    :param db_uri: Per-test SQLite URI.
    :param tmp_path: Pytest temp dir for artifacts.
    :returns: A :class:`FastAPI` instance with ``UnifiedAuthProvider`` active.
    """
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
    """Async HTTP client wired to the auth-enabled app.

    :param auth_app: FastAPI app with permission store.
    :param mock_llm: Controllable mock LLM — released on teardown.
    :param tmp_path: Pytest temp dir for the harness process manager.
    :yields: A ready-to-use :class:`httpx.AsyncClient`.
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


# ── Tests ──────────────────────────────────────────────────────────────────────


async def test_comments_created_by_reflects_request_user(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Two users posting to the same session each get their own ``created_by``.

    Alice and Bob both add a comment to the same session. When the
    comments are listed, each comment must carry the email of the user
    who created it — not a shared value, not None.

    If this test fails, ``get_user_id`` is not being called in
    ``add_comment``, or the value is not being threaded through to
    ``store.add()``.

    :param auth_client: HTTP client backed by the auth-enabled app.
    :param db_uri: Per-test SQLite URI, used to pre-seed permission grants
        so both users have edit access on the session.
    """
    session_id = _seed_session_with_grants(
        db_uri,
        {
            "alice@example.com": LEVEL_EDIT,
            "bob@example.com": LEVEL_EDIT,
        },
    )

    # Alice adds a comment.
    alice_resp = await auth_client.post(
        f"/v1/sessions/{session_id}/comments",
        json={
            "path": "src/app.py",
            "body": "Alice's review note",
            "start_index": 0,
            "end_index": 10,
        },
        headers={"X-Forwarded-Email": "alice@example.com"},
    )
    alice_resp.raise_for_status()
    alice_comment = alice_resp.json()

    # Bob adds a comment.
    bob_resp = await auth_client.post(
        f"/v1/sessions/{session_id}/comments",
        json={
            "path": "src/app.py",
            "body": "Bob's review note",
            "start_index": 0,
            "end_index": 8,
        },
        headers={"X-Forwarded-Email": "bob@example.com"},
    )
    bob_resp.raise_for_status()
    bob_comment = bob_resp.json()

    # POST responses must carry the correct author immediately.
    assert alice_comment["created_by"] == "alice@example.com", (
        f"Expected alice@example.com from POST, got {alice_comment['created_by']!r}. "
        "The X-Forwarded-Email header is not being read in add_comment()."
    )
    assert bob_comment["created_by"] == "bob@example.com", (
        f"Expected bob@example.com from POST, got {bob_comment['created_by']!r}. "
        "The X-Forwarded-Email header is not being read in add_comment()."
    )

    # The two authors must be distinct — not the same value.
    assert alice_comment["created_by"] != bob_comment["created_by"], (
        "Both comments have the same created_by — the per-request user identity "
        "is not being captured independently for each request."
    )

    # List the session's comments and verify the values survived the round-trip.
    list_resp = await auth_client.get(
        f"/v1/sessions/{session_id}/comments",
        headers={"X-Forwarded-Email": "alice@example.com"},
    )
    list_resp.raise_for_status()
    listed = list_resp.json()

    assert len(listed) == 2, (
        f"Expected 2 comments, got {len(listed)}. One of the POSTs may not have persisted."
    )

    by_body = {c["body"]: c["created_by"] for c in listed}
    alice_key = "Alice's review note"
    bob_key = "Bob's review note"
    assert by_body[alice_key] == "alice@example.com", (
        f"Alice's comment has wrong created_by in listing: {by_body[alice_key]!r}"
    )
    assert by_body[bob_key] == "bob@example.com", (
        f"Bob's comment has wrong created_by in listing: {by_body[bob_key]!r}"
    )


async def test_admin_cannot_add_comment_to_nonexistent_session(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Admin bypass must not allow orphan comments on missing sessions."""
    admin = "admin@example.com"
    missing_session_id = "conv_does_not_exist"
    perm_store = SqlAlchemyPermissionStore(db_uri)
    perm_store.ensure_user(admin, is_admin=True)

    resp = await auth_client.post(
        f"/v1/sessions/{missing_session_id}/comments",
        json={
            "path": "src/app.py",
            "body": "Admin orphan probe",
            "start_index": 0,
            "end_index": 5,
        },
        headers={"X-Forwarded-Email": admin},
    )

    assert resp.status_code == 404
    assert SqlAlchemyCommentStore(db_uri).list_for_conversation(missing_session_id) == []


async def test_read_only_user_cannot_mutate_comments(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A user with read-only access can list comments but not add, edit, or delete them.

    Alice owns the session and adds a comment. Bob has ``LEVEL_READ`` only.
    Attempts by Bob to POST, PATCH, DELETE, or send comments must return 403.
    Bob's GET request must succeed (200).

    If this test fails, the ``require_access`` call is missing or uses the
    wrong level for one of the mutating handlers.

    :param auth_client: HTTP client backed by the auth-enabled app.
    :param db_uri: Per-test SQLite URI, used to set up alice's owner grant
        and bob's read-only grant directly.
    """
    session_id = _seed_session_with_grants(
        db_uri,
        {
            "alice@example.com": LEVEL_EDIT,
            "bob@example.com": LEVEL_READ,
        },
    )

    # Alice adds a comment so there is something to act on.
    add_resp = await auth_client.post(
        f"/v1/sessions/{session_id}/comments",
        json={
            "path": "src/main.py",
            "body": "Alice's comment",
            "start_index": 0,
            "end_index": 5,
        },
        headers={"X-Forwarded-Email": "alice@example.com"},
    )
    assert add_resp.status_code == 200, (
        f"Alice (owner) could not add a comment: {add_resp.status_code} {add_resp.text}"
    )
    comment_id = add_resp.json()["id"]

    # Bob (read-only) must NOT be able to add a new comment.
    bob_add = await auth_client.post(
        f"/v1/sessions/{session_id}/comments",
        json={
            "path": "src/main.py",
            "body": "Bob's comment",
            "start_index": 0,
            "end_index": 5,
        },
        headers={"X-Forwarded-Email": "bob@example.com"},
    )
    assert bob_add.status_code == 403, (
        f"Expected 403 for read-only user adding a comment, got {bob_add.status_code}. "
        "add_comment is not enforcing LEVEL_EDIT."
    )

    # Bob (read-only) must NOT be able to patch a comment.
    bob_patch = await auth_client.patch(
        f"/v1/sessions/{session_id}/comments/{comment_id}",
        json={"body": "Bob edited this"},
        headers={"X-Forwarded-Email": "bob@example.com"},
    )
    assert bob_patch.status_code == 403, (
        f"Expected 403 for read-only user patching a comment, got {bob_patch.status_code}. "
        "update_comment is not enforcing LEVEL_EDIT."
    )

    # Bob (read-only) must NOT be able to delete a comment.
    bob_delete = await auth_client.delete(
        f"/v1/sessions/{session_id}/comments/{comment_id}",
        headers={"X-Forwarded-Email": "bob@example.com"},
    )
    assert bob_delete.status_code == 403, (
        f"Expected 403 for read-only user deleting a comment, got {bob_delete.status_code}. "
        "delete_comment is not enforcing LEVEL_EDIT."
    )

    # Bob (read-only) must NOT be able to send comments to the agent.
    bob_send = await auth_client.post(
        f"/v1/sessions/{session_id}/comments/send",
        json={"comment_ids": [comment_id]},
        headers={"X-Forwarded-Email": "bob@example.com"},
    )
    assert bob_send.status_code == 403, (
        f"Expected 403 for read-only user sending comments, got {bob_send.status_code}. "
        "send_to_agent is not enforcing LEVEL_EDIT."
    )

    # Bob (read-only) CAN list comments.
    bob_list = await auth_client.get(
        f"/v1/sessions/{session_id}/comments",
        headers={"X-Forwarded-Email": "bob@example.com"},
    )
    assert bob_list.status_code == 200, (
        f"Expected 200 for read-only user listing comments, got {bob_list.status_code}. "
        "list_comments should allow LEVEL_READ access."
    )
    listed = bob_list.json()
    assert len(listed) == 1, (
        f"Bob should see Alice's comment in the listing, got {len(listed)} comments."
    )


async def _add_comment(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    user: str,
    path: str,
    body: str,
    start_index: int,
    end_index: int,
) -> dict:
    """Add a comment as ``user`` and return the serialized comment dict.

    :param client: Auth-enabled HTTP client.
    :param session_id: Owning session ID.
    :param user: Email to send in ``X-Forwarded-Email``.
    :param path: File path for the comment.
    :param body: Comment body text.
    :param start_index: Inclusive start offset.
    :param end_index: Exclusive end offset.
    :returns: The created comment dict (includes ``id`` and ``status``).
    """
    resp = await client.post(
        f"/v1/sessions/{session_id}/comments",
        json={
            "path": path,
            "body": body,
            "start_index": start_index,
            "end_index": end_index,
        },
        headers={"X-Forwarded-Email": user},
    )
    resp.raise_for_status()
    return resp.json()


async def test_send_marks_comments_addressed_and_formats_message(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Pins the current ``/comments/send`` contract without a live agent.

    The send endpoint only touches the comment store (mark addressed +
    format message); it does not invoke the LLM, so this runs in CI without
    an API key — unlike the e2e coverage in ``tests/e2e/test_comments_e2e.py``.

    Current behavior asserted here:
    1. ``formatted_message`` contains each comment body and the file path.
    2. ``sent_comment_ids`` echoes the requested IDs.
    3. Both comments transition ``draft`` -> ``addressed`` after send.

    :param auth_client: HTTP client backed by the auth-enabled app.
    :param db_uri: Per-test SQLite URI used to seed the edit grant.
    """
    session_id = _seed_session_with_grants(db_uri, {"alice@example.com": LEVEL_EDIT})

    c1 = await _add_comment(
        auth_client,
        session_id,
        user="alice@example.com",
        path="src/review.py",
        body="Rename this variable",
        start_index=0,
        end_index=4,
    )
    c2 = await _add_comment(
        auth_client,
        session_id,
        user="alice@example.com",
        path="src/review.py",
        body="Add a docstring here",
        start_index=10,
        end_index=14,
    )
    assert c1["status"] == "draft" and c2["status"] == "draft", (
        "Newly created comments must start as 'draft'."
    )

    send_resp = await auth_client.post(
        f"/v1/sessions/{session_id}/comments/send",
        json={"comment_ids": [c1["id"], c2["id"]]},
        headers={"X-Forwarded-Email": "alice@example.com"},
    )
    send_resp.raise_for_status()
    payload = send_resp.json()

    formatted = payload["formatted_message"]
    assert "Rename this variable" in formatted, (
        f"First comment body missing from formatted_message: {formatted!r}"
    )
    assert "Add a docstring here" in formatted, (
        f"Second comment body missing from formatted_message: {formatted!r}"
    )
    assert "src/review.py" in formatted, f"File path missing from formatted_message: {formatted!r}"
    assert payload["sent_comment_ids"] == [c1["id"], c2["id"]], (
        f"sent_comment_ids should echo the requested IDs, got {payload['sent_comment_ids']!r}"
    )

    list_resp = await auth_client.get(
        f"/v1/sessions/{session_id}/comments",
        headers={"X-Forwarded-Email": "alice@example.com"},
    )
    list_resp.raise_for_status()
    statuses = {c["id"]: c["status"] for c in list_resp.json()}
    assert statuses[c1["id"]] == "addressed", (
        f"Comment {c1['id']} should be 'addressed' after send, got {statuses[c1['id']]!r}"
    )
    assert statuses[c2["id"]] == "addressed", (
        f"Comment {c2['id']} should be 'addressed' after send, got {statuses[c2['id']]!r}"
    )


@pytest.mark.xfail(
    strict=True,
    reason="send must not auto-resolve comments before verification.",
)
async def test_send_leaves_comments_open_until_verified(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Spec: sending to the agent should NOT auto-resolve comments.

    Sending comments creates a request for the agent, but the feedback is
    not actually resolved until the requested change is verified (explicit
    user action or a verified-resolution workflow). The current endpoint
    optimistically flips ``draft`` -> ``addressed`` on send, which hides
    unresolved feedback if the agent fails, ignores, or mis-handles a
    comment. This strict xfail documents the desired contract: the comment
    stays ``draft`` immediately after ``/comments/send``.

    :param auth_client: HTTP client backed by the auth-enabled app.
    :param db_uri: Per-test SQLite URI used to seed the edit grant.
    """
    session_id = _seed_session_with_grants(db_uri, {"alice@example.com": LEVEL_EDIT})

    comment = await _add_comment(
        auth_client,
        session_id,
        user="alice@example.com",
        path="src/review.py",
        body="Please fix this edge case",
        start_index=0,
        end_index=4,
    )

    send_resp = await auth_client.post(
        f"/v1/sessions/{session_id}/comments/send",
        json={"comment_ids": [comment["id"]]},
        headers={"X-Forwarded-Email": "alice@example.com"},
    )
    send_resp.raise_for_status()

    list_resp = await auth_client.get(
        f"/v1/sessions/{session_id}/comments",
        headers={"X-Forwarded-Email": "alice@example.com"},
    )
    list_resp.raise_for_status()
    status = {c["id"]: c["status"] for c in list_resp.json()}[comment["id"]]
    assert status == "draft", (
        f"Comment should remain 'draft' (open) until verified, got {status!r}. "
        "Sending to the agent should not optimistically auto-resolve feedback."
    )


async def test_comment_api_serializes_updated_at(
    auth_client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``updated_at`` rides the comment API: set on POST, bumped on PATCH.

    The web app's Comment type and the session-list comments fingerprint
    both rely on this field reaching the wire; an asdict()-level
    serialization regression (e.g. renaming the entity field) would break
    clients without failing any store test.
    """
    alice = "alice@example.com"
    session_id = _seed_session_with_grants(db_uri, {alice: LEVEL_EDIT})

    # Deterministic write clock: PATCH must land on a later epoch second
    # than POST, which real time can't guarantee inside one test.
    us = 1_000_000  # updated_at is epoch-µs; created_at stays seconds
    clock = {"now": 1_000}
    monkeypatch.setattr(
        "omnigent.stores.comment_store.sqlalchemy_store.now_epoch_us",
        lambda: clock["now"] * us,
    )

    created = await _add_comment(
        auth_client,
        session_id,
        user=alice,
        path="src/app.py",
        body="fix me",
        start_index=0,
        end_index=6,
    )
    # A fresh comment reports its creation instant as the last mutation
    # time — in microseconds, while created_at stays in seconds.
    assert created["created_at"] == 1_000
    assert created["updated_at"] == 1_000 * us

    clock["now"] = 2_000
    patch_resp = await auth_client.patch(
        f"/v1/sessions/{session_id}/comments/{created['id']}",
        json={"status": "addressed"},
        headers={"X-Forwarded-Email": alice},
    )
    patch_resp.raise_for_status()
    patched = patch_resp.json()
    # The mutation time must move while creation time is untouched — a
    # stale updated_at means the session fingerprint misses edits.
    assert patched["updated_at"] == 2_000 * us
    assert patched["created_at"] == 1_000


async def test_patch_rejects_unknown_status_with_400(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """An out-of-domain status is a 400, not a 500.

    ``comments.status`` is a closed enum (draft/addressed). A PATCH with an
    unknown value must be rejected at the route with a clean validation
    error rather than reaching the store, where the enum codec raises and
    would otherwise surface as an opaque 500.
    """
    alice = "alice@example.com"
    session_id = _seed_session_with_grants(db_uri, {alice: LEVEL_EDIT})
    created = await _add_comment(
        auth_client,
        session_id,
        user=alice,
        path="src/app.py",
        body="fix me",
        start_index=0,
        end_index=6,
    )

    resp = await auth_client.patch(
        f"/v1/sessions/{session_id}/comments/{created['id']}",
        json={"status": "resolved"},  # not a real comment status
        headers={"X-Forwarded-Email": alice},
    )
    assert resp.status_code == 400, (
        f"Unknown status must be a 400, got {resp.status_code}: {resp.text}"
    )


# ── Author-only edit/delete (one editor may not rewrite another's comment) ─────


async def test_body_edit_is_author_only_status_change_is_shared(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A second editor may resolve another user's comment but not rewrite it.

    Alice and Bob both have ``LEVEL_EDIT`` on the session. Alice authors a
    comment. Bob — a legitimate editor — must NOT be able to edit the
    comment's *body* (403, and the stored text is untouched), but he MUST
    still be able to flip its *status* (the shared review-workflow action
    the agent and "Address All" also perform). Alice retains full edit
    rights over her own comment.

    This is the core of the fix: session-level edit access alone no longer
    authorizes rewriting another user's words. If the body PATCH returns
    200, the author gate is missing; if the status PATCH returns 403, the
    gate over-reached into the shared workflow path.

    :param auth_client: HTTP client backed by the auth-enabled app.
    :param db_uri: Per-test SQLite URI used to seed both edit grants.
    """
    session_id = _seed_session_with_grants(
        db_uri,
        {"alice@example.com": LEVEL_EDIT, "bob@example.com": LEVEL_EDIT},
    )
    comment = await _add_comment(
        auth_client,
        session_id,
        user="alice@example.com",
        path="src/app.py",
        body="Alice's note",
        start_index=0,
        end_index=5,
    )

    # Bob cannot rewrite the body of Alice's comment.
    bob_body_patch = await auth_client.patch(
        f"/v1/sessions/{session_id}/comments/{comment['id']}",
        json={"body": "Bob overwrote this"},
        headers={"X-Forwarded-Email": "bob@example.com"},
    )
    assert bob_body_patch.status_code == 403, (
        f"Expected 403 for a non-author editing another user's comment body, "
        f"got {bob_body_patch.status_code}. The author gate on body edits is missing."
    )

    # Decisive: the rejected edit did not mutate the stored body. A 403 that
    # still wrote the row would mean the gate ran after the store mutation.
    after_reject = await auth_client.get(
        f"/v1/sessions/{session_id}/comments",
        headers={"X-Forwarded-Email": "alice@example.com"},
    )
    after_reject.raise_for_status()
    assert after_reject.json()[0]["body"] == "Alice's note", (
        "Bob's forbidden body edit still changed the stored text — the author "
        "check must run before store.update_comment."
    )

    # Bob CAN mark Alice's comment addressed — status changes stay shared.
    bob_status_patch = await auth_client.patch(
        f"/v1/sessions/{session_id}/comments/{comment['id']}",
        json={"status": "addressed"},
        headers={"X-Forwarded-Email": "bob@example.com"},
    )
    assert bob_status_patch.status_code == 200, (
        f"Expected 200 for an editor marking another user's comment addressed, "
        f"got {bob_status_patch.status_code}. The author gate must not apply to "
        "status-only changes (the agent and 'Address All' rely on this)."
    )
    assert bob_status_patch.json()["status"] == "addressed"

    # Alice retains full edit rights over her own comment's body.
    alice_body_patch = await auth_client.patch(
        f"/v1/sessions/{session_id}/comments/{comment['id']}",
        json={"body": "Alice revised her own note"},
        headers={"X-Forwarded-Email": "alice@example.com"},
    )
    assert alice_body_patch.status_code == 200, (
        f"Author could not edit her own comment body: {alice_body_patch.status_code} "
        f"{alice_body_patch.text}"
    )
    assert alice_body_patch.json()["body"] == "Alice revised her own note"


async def test_delete_is_author_only(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A second editor cannot delete another user's comment; the author can.

    Alice and Bob both have ``LEVEL_EDIT``. Bob's attempt to delete Alice's
    comment must 403 and leave the comment in place; Alice's own delete must
    succeed and remove it.

    :param auth_client: HTTP client backed by the auth-enabled app.
    :param db_uri: Per-test SQLite URI used to seed both edit grants.
    """
    session_id = _seed_session_with_grants(
        db_uri,
        {"alice@example.com": LEVEL_EDIT, "bob@example.com": LEVEL_EDIT},
    )
    comment = await _add_comment(
        auth_client,
        session_id,
        user="alice@example.com",
        path="src/app.py",
        body="Alice's note",
        start_index=0,
        end_index=5,
    )

    bob_delete = await auth_client.delete(
        f"/v1/sessions/{session_id}/comments/{comment['id']}",
        headers={"X-Forwarded-Email": "bob@example.com"},
    )
    assert bob_delete.status_code == 403, (
        f"Expected 403 for a non-author deleting another user's comment, got "
        f"{bob_delete.status_code}. The author gate on delete is missing."
    )

    # The comment must survive Bob's forbidden delete.
    still_there = await auth_client.get(
        f"/v1/sessions/{session_id}/comments",
        headers={"X-Forwarded-Email": "alice@example.com"},
    )
    still_there.raise_for_status()
    assert len(still_there.json()) == 1, (
        "Bob's forbidden delete removed the comment anyway — the author check "
        "must run before store.delete."
    )

    # Alice deletes her own comment successfully.
    alice_delete = await auth_client.delete(
        f"/v1/sessions/{session_id}/comments/{comment['id']}",
        headers={"X-Forwarded-Email": "alice@example.com"},
    )
    assert alice_delete.status_code == 200, (
        f"Author could not delete her own comment: {alice_delete.status_code} {alice_delete.text}"
    )
    gone = await auth_client.get(
        f"/v1/sessions/{session_id}/comments",
        headers={"X-Forwarded-Email": "alice@example.com"},
    )
    gone.raise_for_status()
    assert gone.json() == [], "Author's own delete did not remove the comment."


async def test_authorless_comment_editable_by_any_editor(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A comment with no recorded author stays editable/deletable by any editor.

    Legacy comments (created before per-user attribution) and single-user
    comments have ``created_by is None``. There is no author to protect, so
    the author gate must fall through and allow any ``LEVEL_EDIT`` collaborator
    to edit and delete them — otherwise the fix would strand legacy data as
    permanently uneditable.

    The authorless comment is seeded directly through the store (the POST
    route always stamps the requesting user, so it cannot produce a
    ``created_by``-less row).

    :param auth_client: HTTP client backed by the auth-enabled app.
    :param db_uri: Per-test SQLite URI, used both to seed the grant and to
        insert the authorless comment via the real comment store.
    """
    session_id = _seed_session_with_grants(db_uri, {"bob@example.com": LEVEL_EDIT})

    # Seed a legacy/authorless comment straight into the store.
    comment = SqlAlchemyCommentStore(db_uri).add(
        conversation_id=session_id,
        path="src/legacy.py",
        body="Legacy note",
        start_index=0,
        end_index=4,
        created_by=None,
    )

    # Bob (an editor, not the author — there is none) can edit the body.
    bob_edit = await auth_client.patch(
        f"/v1/sessions/{session_id}/comments/{comment.id}",
        json={"body": "Bob updated the legacy note"},
        headers={"X-Forwarded-Email": "bob@example.com"},
    )
    assert bob_edit.status_code == 200, (
        f"An editor could not edit an authorless comment: {bob_edit.status_code} "
        f"{bob_edit.text}. The created_by-None fallback is not allowing edits."
    )
    assert bob_edit.json()["body"] == "Bob updated the legacy note"

    # And delete it.
    bob_delete = await auth_client.delete(
        f"/v1/sessions/{session_id}/comments/{comment.id}",
        headers={"X-Forwarded-Email": "bob@example.com"},
    )
    assert bob_delete.status_code == 200, (
        f"An editor could not delete an authorless comment: {bob_delete.status_code} "
        f"{bob_delete.text}."
    )

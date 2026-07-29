"""E2E tests for the per-session review comments API.

All tests are pure HTTP — they call the
``/v1/sessions/{id}/comments`` routes directly without spinning
up an LLM.  They only need the ``http_client`` fixture.

The live server always runs with ``permission_store`` active (the
CLI always enables it), so every test must:

* Use a *real* session created via ``POST /v1/sessions`` (not a
  random string — sessions that don't exist in the DB are denied by
  ``check_session_access``).
* Send ``X-Forwarded-Email`` headers so the server can resolve a
  user identity and check grants.

A one-off minimal agent bundle is uploaded at module level and
reused across all tests to keep overhead low.

Usage::

    pytest tests/e2e/test_comments_e2e.py -v
"""

from __future__ import annotations

import io
import json
import tarfile
from typing import Any

import httpx
import yaml

from omnigent.server.auth import LEVEL_READ

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_OWNER_EMAIL = "owner@e2e.test"
"""Default user that creates sessions and therefore holds LEVEL_OWNER."""

_READER_EMAIL = "reader@e2e.test"
"""User granted LEVEL_READ in permission tests."""

_AGENT_NAME = "e2e-comments-test"
"""Fixed agent name shared across the whole module."""

# Module-level cache so the agent bundle is uploaded at most once per
# pytest session, regardless of how many tests run.
_AGENT_ID: str | None = None


# ---------------------------------------------------------------------------
# Agent / session setup helpers
# ---------------------------------------------------------------------------


def _build_minimal_agent_bundle() -> bytes:
    """Build a minimal agent bundle as an in-memory tar.gz.

    Creates a ``config.yaml`` with the bare minimum fields required
    by the agent spec validator. The LLM model value matches the
    agent name so tests that hit mock-LLM paths get a consistent
    target, though comments tests never start a response.

    :returns: Gzipped tarball bytes accepted by multipart
        ``POST /v1/sessions``.
    """
    config = yaml.dump(
        {
            "spec_version": 1,
            "name": _AGENT_NAME,
            "executor": {
                "type": "omnigent",
                "config": {"harness": "openai-agents"},
            },
            "llm": {
                "model": _AGENT_NAME,
                "connection": {"api_key": "test-key"},
            },
        }
    ).encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo(name="config.yaml")
        info.size = len(config)
        tf.addfile(info, io.BytesIO(config))
    return buf.getvalue()


def _ensure_agent(client: httpx.Client) -> str:
    """Upload the minimal agent bundle via multipart session create if not
    already cached, and return the agent ID.

    Uses ``_AGENT_ID`` as a module-level cache so the upload happens
    at most once per pytest process. A 409 response (agent already
    exists) is handled by looking up the agent via
    ``GET /v1/sessions?agent_name=``.

    :param client: Live server HTTP client.
    :returns: The agent's ``id`` field, e.g. ``"ag_abc123"``.
    """
    global _AGENT_ID
    if _AGENT_ID is not None:
        return _AGENT_ID

    bundle = _build_minimal_agent_bundle()
    metadata = json.dumps({})
    resp = client.post(
        "/v1/sessions",
        data={"metadata": metadata},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
    )
    if resp.status_code == 409:
        # Agent already exists — look up its id via sessions list.
        list_resp = client.get(
            "/v1/sessions",
            params={"agent_name": _AGENT_NAME, "limit": 1},
        )
        list_resp.raise_for_status()
        sessions = list_resp.json()["data"]
        assert sessions, f"Agent {_AGENT_NAME!r} returned 409 but no sessions found."
        _AGENT_ID = sessions[0]["agent_id"]
    else:
        resp.raise_for_status()
        session_id = resp.json()["session_id"]
        agent_resp = client.get(f"/v1/sessions/{session_id}/agent")
        agent_resp.raise_for_status()
        _AGENT_ID = agent_resp.json()["id"]

    return _AGENT_ID


def _create_session(client: httpx.Client, *, email: str = _OWNER_EMAIL) -> str:
    """Create a real session as *email* and return its ID.

    Uses multipart bundled create so each session gets its own
    session-scoped agent, avoiding cross-user agent-ownership issues.
    The creator is automatically granted LEVEL_OWNER by the session
    route, so subsequent requests from *email* on this session pass
    all permission checks.

    :param client: Live server HTTP client.
    :param email: User identity to send in ``X-Forwarded-Email``.
        Defaults to :data:`_OWNER_EMAIL`.
    :returns: The created session ID, e.g. ``"conv_abc123"``.
    """
    bundle = _build_minimal_agent_bundle()
    metadata = json.dumps({})
    resp = client.post(
        "/v1/sessions",
        data={"metadata": metadata},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        headers={"X-Forwarded-Email": email},
    )
    assert resp.status_code == 201, f"Session creation failed: {resp.status_code} {resp.text}"
    return resp.json()["session_id"]


def _grant_access(
    client: httpx.Client,
    session_id: str,
    *,
    granter: str,
    target_user: str,
    level: int,
) -> None:
    """Grant *target_user* access to *session_id* at the given *level*.

    :param client: Live server HTTP client.
    :param session_id: The session to grant access to.
    :param granter: Email of the user making the grant (must have
        LEVEL_MANAGE or higher).
    :param target_user: Email of the user receiving the grant.
    :param level: Numeric permission level (1=read, 2=edit, 3=manage).
    """
    resp = client.put(
        f"/v1/sessions/{session_id}/permissions",
        json={"user_id": target_user, "level": level},
        headers={"X-Forwarded-Email": granter},
    )
    assert resp.status_code == 200, f"Grant failed: {resp.status_code} {resp.text}"


# ---------------------------------------------------------------------------
# Comment CRUD helpers
# ---------------------------------------------------------------------------


def _add_comment(
    client: httpx.Client,
    session_id: str,
    *,
    path: str,
    line: int,
    body: str,
    email: str = _OWNER_EMAIL,
) -> dict[str, Any]:
    """POST a single file comment and return the created dict.

    The ``line`` parameter is kept for test readability but is no longer
    sent to the server; comments now use absolute character offsets.
    A zero offset is used as a placeholder for tests that do not need
    precise positioning.

    :param client: HTTP client pointed at the live server.
    :param session_id: Owning session.
    :param path: File path for the comment.
    :param line: Ignored (kept for call-site readability).
    :param body: Comment text.
    :param email: User identity for ``X-Forwarded-Email``.
    :returns: The server-created comment dict.
    """
    resp = client.post(
        f"/v1/sessions/{session_id}/comments",
        json={
            "path": path,
            "body": body,
            "start_index": 0,
            "end_index": 0,
        },
        headers={"X-Forwarded-Email": email},
    )
    resp.raise_for_status()
    return resp.json()


def _list_comments(
    client: httpx.Client,
    session_id: str,
    *,
    path: str | None = None,
    email: str = _OWNER_EMAIL,
) -> list[dict[str, Any]]:
    """GET comments for a session, optionally filtered by path.

    :param client: HTTP client pointed at the live server.
    :param session_id: Session to query.
    :param path: If given, filter by file path.
    :param email: User identity for ``X-Forwarded-Email``.
    :returns: List of comment dicts.
    """
    url = f"/v1/sessions/{session_id}/comments"
    params: dict[str, str] = {}
    if path is not None:
        params["path"] = path
    resp = client.get(url, params=params, headers={"X-Forwarded-Email": email})
    resp.raise_for_status()
    return resp.json()


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_comments_add_and_list(http_client: httpx.Client) -> None:
    """Create two comments on the same file, then list and verify both.

    Covers the POST and GET round-trip and checks that all expected
    fields are returned with the correct values.

    :param http_client: HTTP client pointed at the live server.
    """
    session_id = _create_session(http_client)

    c1 = _add_comment(http_client, session_id, path="app.py", line=10, body="Fix this loop")
    c2 = _add_comment(http_client, session_id, path="app.py", line=25, body="Add type hints")

    # Both comments must have IDs assigned by the server.
    assert c1["id"], "First comment must have a non-empty id"
    assert c2["id"], "Second comment must have a non-empty id"

    listed = _list_comments(http_client, session_id)

    # Exactly two comments must be returned — no more, no fewer.
    assert len(listed) == 2, (
        f"Expected 2 comments after two POSTs, got {len(listed)}. "
        "If 0, the server is not persisting comments. "
        "If > 2, a previous test left stale data under this session_id."
    )

    bodies = [c["body"] for c in listed]
    # The body text must survive the round-trip intact.
    assert "Fix this loop" in bodies, (
        f"First comment body not found in listing: {bodies}. "
        "Indicates the POST body was not persisted."
    )
    assert "Add type hints" in bodies, (
        f"Second comment body not found in listing: {bodies}. "
        "Indicates the second POST body was not persisted."
    )

    # Comments start in draft status.
    statuses = {c["body"]: c["status"] for c in listed}
    assert statuses["Fix this loop"] == "draft", (
        f"Newly created comment should be 'draft', got {statuses['Fix this loop']!r}"
    )

    # created_by must be present in both POST response and GET listing.
    assert "created_by" in c1, (
        "POST response is missing the 'created_by' field. "
        "The server may not be serializing the new column."
    )
    assert "created_by" in listed[0], (
        "GET listing is missing the 'created_by' field. "
        "The store or serialization is not returning the column."
    )


def test_comments_list_by_path_filters_correctly(http_client: httpx.Client) -> None:
    """Comments for path A are not returned when filtering by path B.

    Ensures the ?path= query param scopes the list to only matching
    file paths.

    :param http_client: HTTP client pointed at the live server.
    """
    session_id = _create_session(http_client)

    _add_comment(http_client, session_id, path="main.py", line=1, body="Comment on main")
    _add_comment(http_client, session_id, path="utils.py", line=5, body="Comment on utils")

    main_comments = _list_comments(http_client, session_id, path="main.py")
    utils_comments = _list_comments(http_client, session_id, path="utils.py")

    # Each path filter must return exactly the comments for that file.
    assert len(main_comments) == 1, (
        f"Expected 1 comment for main.py, got {len(main_comments)}. "
        "The ?path= filter is not scoping correctly."
    )
    assert main_comments[0]["body"] == "Comment on main", (
        f"Wrong comment returned for main.py: {main_comments[0]['body']!r}"
    )

    assert len(utils_comments) == 1, (
        f"Expected 1 comment for utils.py, got {len(utils_comments)}. "
        "The ?path= filter is not scoping correctly."
    )
    assert utils_comments[0]["body"] == "Comment on utils", (
        f"Wrong comment returned for utils.py: {utils_comments[0]['body']!r}"
    )


def test_comments_session_isolation(http_client: httpx.Client) -> None:
    """Comments added to session A are invisible to session B.

    Verifies that the session_id is used as an isolation boundary.
    If comments leaked across sessions, both lists would be non-empty.

    :param http_client: HTTP client pointed at the live server.
    """
    conv_a = _create_session(http_client)
    conv_b = _create_session(http_client)

    _add_comment(http_client, conv_a, path="shared.py", line=1, body="Only in A")

    b_comments = _list_comments(http_client, conv_b)

    # Session B must see zero comments because none were added to it.
    assert b_comments == [], (
        f"Session B should have no comments, but got: {b_comments}. "
        "Comments are leaking across session boundaries."
    )


def test_comments_update_body_and_status(http_client: httpx.Client) -> None:
    """Update a comment's body text and status; verify both are persisted.

    :param http_client: HTTP client pointed at the live server.
    """
    session_id = _create_session(http_client)
    created = _add_comment(http_client, session_id, path="api.py", line=42, body="Original text")
    comment_id = created["id"]

    # Update body only.
    patch_resp = http_client.patch(
        f"/v1/sessions/{session_id}/comments/{comment_id}",
        json={"body": "Revised text"},
        headers={"X-Forwarded-Email": _OWNER_EMAIL},
    )
    patch_resp.raise_for_status()
    updated = patch_resp.json()

    # The body must be the new text, not the original.
    assert updated["body"] == "Revised text", (
        f"Body not updated: expected 'Revised text', got {updated['body']!r}. "
        "PATCH is not persisting body changes."
    )
    # Status must be unchanged since we only patched body.
    assert updated["status"] == "draft", (
        f"Status changed unexpectedly: expected 'draft', got {updated['status']!r}"
    )

    # Update status to addressed.
    resolve_resp = http_client.patch(
        f"/v1/sessions/{session_id}/comments/{comment_id}",
        json={"status": "addressed"},
        headers={"X-Forwarded-Email": _OWNER_EMAIL},
    )
    resolve_resp.raise_for_status()
    resolved = resolve_resp.json()

    # The status must reflect the PATCH; body must be unchanged.
    assert resolved["status"] == "addressed", (
        f"Status not updated to 'addressed': got {resolved['status']!r}"
    )
    assert resolved["body"] == "Revised text", (
        "Body regressed after status update — PATCH should only touch "
        f"fields present in the request body, got {resolved['body']!r}"
    )


def test_comments_update_returns_404_for_comment_from_other_session(
    http_client: httpx.Client,
) -> None:
    """PATCH via session B rejects a comment that belongs to A.

    Verifies the ownership check fires BEFORE mutation: the comment body
    and status must be unchanged in session A after the rejected request.

    :param http_client: HTTP client pointed at the live server.
    """
    conv_a = _create_session(http_client)
    conv_b = _create_session(http_client)

    comment = _add_comment(http_client, conv_a, path="owned.py", line=1, body="Original body")

    resp = http_client.patch(
        f"/v1/sessions/{conv_b}/comments/{comment['id']}",
        json={"body": "Hijacked body", "status": "resolved"},
        headers={"X-Forwarded-Email": _OWNER_EMAIL},
    )
    assert resp.status_code == 404, (
        f"Expected 404 when patching a comment owned by another session, "
        f"got {resp.status_code}. Ownership check is missing or fires after mutation."
    )

    # The comment must be unchanged in session A.
    a_comments = _list_comments(http_client, conv_a)
    assert len(a_comments) == 1
    assert a_comments[0]["body"] == "Original body", (
        f"Comment body was mutated despite the rejected PATCH: "
        f"got {a_comments[0]['body']!r}. Mutation happened before ownership check."
    )
    assert a_comments[0]["status"] == "draft", (
        f"Comment status was mutated despite the rejected PATCH: got {a_comments[0]['status']!r}."
    )


def test_comments_delete_removes_comment(http_client: httpx.Client) -> None:
    """Delete a comment; subsequent GET confirms it is gone.

    :param http_client: HTTP client pointed at the live server.
    """
    session_id = _create_session(http_client)
    c1 = _add_comment(http_client, session_id, path="delete_me.py", line=1, body="To be deleted")
    _add_comment(http_client, session_id, path="delete_me.py", line=2, body="To survive")
    comment_id = c1["id"]

    delete_resp = http_client.delete(
        f"/v1/sessions/{session_id}/comments/{comment_id}",
        headers={"X-Forwarded-Email": _OWNER_EMAIL},
    )
    delete_resp.raise_for_status()
    assert delete_resp.json() == {"deleted": True}, (
        f'Expected {{"deleted": true}}, got {delete_resp.json()}'
    )

    remaining = _list_comments(http_client, session_id)

    # Only the second comment should remain.
    assert len(remaining) == 1, (
        f"Expected 1 comment after deletion, got {len(remaining)}. "
        "If 2, the DELETE did not remove the comment. "
        "If 0, the wrong comment was deleted."
    )
    assert remaining[0]["body"] == "To survive", (
        f"Wrong comment survived deletion: {remaining[0]['body']!r}"
    )


def test_comments_delete_returns_404_for_missing(http_client: httpx.Client) -> None:
    """Deleting a non-existent comment returns 404.

    :param http_client: HTTP client pointed at the live server.
    """
    session_id = _create_session(http_client)
    resp = http_client.delete(
        f"/v1/sessions/{session_id}/comments/nonexistent-id-abc123",
        headers={"X-Forwarded-Email": _OWNER_EMAIL},
    )
    # 404 is the expected code for an unknown comment.
    assert resp.status_code == 404, (
        f"Expected 404 for unknown comment id, got {resp.status_code}. "
        "The endpoint is not guarding against unknown ids."
    )


def test_comments_delete_returns_404_for_comment_from_other_session(
    http_client: httpx.Client,
) -> None:
    """Delete via session B's endpoint rejects a comment that belongs to A.

    Verifies the ownership check fires BEFORE deletion: the comment must
    still exist in session A after the rejected request.

    :param http_client: HTTP client pointed at the live server.
    """
    conv_a = _create_session(http_client)
    conv_b = _create_session(http_client)

    # Add a comment to session A.
    comment = _add_comment(http_client, conv_a, path="owned.py", line=1, body="Belongs to A")

    # Attempt to delete it via session B's endpoint.
    resp = http_client.delete(
        f"/v1/sessions/{conv_b}/comments/{comment['id']}",
        headers={"X-Forwarded-Email": _OWNER_EMAIL},
    )
    # Must be rejected — the comment does not belong to conv_b.
    assert resp.status_code == 404, (
        f"Expected 404 when deleting a comment owned by another session, "
        f"got {resp.status_code}. The ownership check is missing or fires after delete."
    )

    # The comment must still exist in session A — it was not deleted.
    a_comments = _list_comments(http_client, conv_a)
    assert len(a_comments) == 1, (
        f"Expected comment to still exist in conv_a after rejected delete, "
        f"but found {len(a_comments)} comments. "
        "The delete executed before the ownership check."
    )
    assert a_comments[0]["id"] == comment["id"], (
        "The surviving comment has a different id — wrong comment was deleted."
    )


def test_comments_send_does_not_mutate_on_partial_failure(
    http_client: httpx.Client,
) -> None:
    """send leaves all comments unmutated when any requested id is invalid.

    Creates one valid draft comment in conv_a, then tries to send it via
    conv_b's send endpoint (which also includes a cross-session id).
    The valid comment must remain 'draft' after the rejected request.

    :param http_client: HTTP client pointed at the live server.
    """
    conv_a = _create_session(http_client)
    conv_b = _create_session(http_client)

    # Add a comment to session A.
    comment = _add_comment(http_client, conv_a, path="batch.py", line=3, body="Should stay draft")

    # Also add a comment to conv_b as the "valid" id in the batch.
    b_comment = _add_comment(http_client, conv_b, path="batch.py", line=5, body="Valid in B")

    # Send via conv_b: b_comment is valid, comment belongs to conv_a (invalid).
    resp = http_client.post(
        f"/v1/sessions/{conv_b}/comments/send",
        json={"comment_ids": [b_comment["id"], comment["id"]]},
        headers={"X-Forwarded-Email": _OWNER_EMAIL},
    )
    # Must fail due to the cross-session id.
    assert resp.status_code == 404, (
        f"Expected 404 when batch includes a comment from another session, got {resp.status_code}."
    )

    # The valid comment (b_comment) must NOT have been mutated to 'sent'.
    b_remaining = _list_comments(http_client, conv_b)
    b_statuses = {c["id"]: c["status"] for c in b_remaining}
    assert b_statuses.get(b_comment["id"]) == "draft", (
        f"Expected b_comment to remain 'draft' after failed send, "
        f"got {b_statuses.get(b_comment['id'])!r}. "
        "The send endpoint is mutating comments before ownership validation."
    )


def test_comments_send_to_agent_formats_message_and_transitions_status(
    http_client: httpx.Client,
) -> None:
    """send returns a formatted message and marks comments as 'addressed'.

    Creates two draft comments on different lines of the same file, posts
    to send, then verifies:
    1. formatted_message contains both comment bodies.
    2. formatted_message contains the file path.
    3. Both comments' status is now 'addressed'.

    :param http_client: HTTP client pointed at the live server.
    """
    session_id = _create_session(http_client)
    c1 = _add_comment(http_client, session_id, path="review.py", line=5, body="Missing null check")
    c2 = _add_comment(
        http_client, session_id, path="review.py", line=12, body="Consider extracting helper"
    )

    send_resp = http_client.post(
        f"/v1/sessions/{session_id}/comments/send",
        json={"comment_ids": [c1["id"], c2["id"]]},
        headers={"X-Forwarded-Email": _OWNER_EMAIL},
    )
    send_resp.raise_for_status()
    payload = send_resp.json()

    formatted = payload["formatted_message"]
    sent_ids = payload["sent_comment_ids"]

    # The formatted message must contain both comment bodies verbatim.
    assert "Missing null check" in formatted, (
        f"First comment body missing from formatted_message: {formatted!r}. "
        "The backend is not including the comment body in the message."
    )
    assert "Consider extracting helper" in formatted, (
        f"Second comment body missing from formatted_message: {formatted!r}"
    )
    # The file path must be included so the agent knows which file to fix.
    assert "review.py" in formatted, (
        f"File path 'review.py' missing from formatted_message: {formatted!r}"
    )

    # Both comment ids must be acknowledged as sent by the server.
    assert set(sent_ids) == {c1["id"], c2["id"]}, (
        f"sent_comment_ids mismatch: expected both ids, got {sent_ids}"
    )

    # After send the comments must be in 'addressed' status.
    updated = _list_comments(http_client, session_id)
    statuses = {c["id"]: c["status"] for c in updated}
    assert statuses[c1["id"]] == "addressed", (
        f"Comment {c1['id']} should be 'addressed' after send, "
        f"got {statuses[c1['id']]!r}. Status transition not persisted."
    )
    assert statuses[c2["id"]] == "addressed", (
        f"Comment {c2['id']} should be 'addressed' after send, got {statuses[c2['id']]!r}"
    )


def test_comments_send_to_agent_with_empty_ids_returns_header_only(
    http_client: httpx.Client,
) -> None:
    """send with no comment_ids returns only the header line.

    Verifies the boundary case where the caller passes an empty list:
    the endpoint should succeed and return just the instruction header.

    :param http_client: HTTP client pointed at the live server.
    """
    session_id = _create_session(http_client)

    send_resp = http_client.post(
        f"/v1/sessions/{session_id}/comments/send",
        json={"comment_ids": []},
        headers={"X-Forwarded-Email": _OWNER_EMAIL},
    )
    send_resp.raise_for_status()
    payload = send_resp.json()

    # The header line is always present.
    assert "Please address" in payload["formatted_message"], (
        f"Header line missing from empty send response: {payload['formatted_message']!r}"
    )
    # No comments were sent so no ids are returned.
    assert payload["sent_comment_ids"] == [], (
        f"Expected empty sent_comment_ids, got {payload['sent_comment_ids']}"
    )


def test_comments_send_returns_404_for_comment_from_other_session(
    http_client: httpx.Client,
) -> None:
    """send returns 404 when a comment_id belongs to a different session.

    Creates a comment in session A, then tries to send it via
    session B's send endpoint.  The route must reject the request
    because the comment's session_id does not match the URL path.

    This is the cross-session boundary check for the send endpoint —
    analogous to the isolation already enforced by delete.

    :param http_client: HTTP client pointed at the live server.
    """
    conv_a = _create_session(http_client)
    conv_b = _create_session(http_client)

    # Add a comment to session A.
    comment = _add_comment(http_client, conv_a, path="foo.py", line=1, body="Belongs to A")

    # Attempt to send it via session B's endpoint.
    resp = http_client.post(
        f"/v1/sessions/{conv_b}/comments/send",
        json={"comment_ids": [comment["id"]]},
        headers={"X-Forwarded-Email": _OWNER_EMAIL},
    )
    # The comment does not belong to conv_b — must return 404.
    assert resp.status_code == 404, (
        f"Expected 404 when sending a comment owned by another session, "
        f"got {resp.status_code}. "
        "The send endpoint is not enforcing session_id ownership."
    )


def test_comments_created_by_round_trips(http_client: httpx.Client) -> None:
    """created_by is present in POST response and survives the GET round-trip.

    Because the server has auth active, ``created_by`` equals the
    ``X-Forwarded-Email`` value — not ``None``.  The test verifies the
    field is present and consistent between the two endpoints.

    :param http_client: HTTP client pointed at the live server.
    """
    session_id = _create_session(http_client)
    created = _add_comment(http_client, session_id, path="rt.py", line=1, body="Check me")

    # POST response must include the field.
    assert "created_by" in created, (
        "POST response is missing 'created_by'. "
        "Ensure asdict(comment) serializes the new entity field."
    )

    listed = _list_comments(http_client, session_id)
    assert len(listed) == 1
    assert "created_by" in listed[0], (
        "GET listing is missing 'created_by'. "
        "The column is not being mapped through _to_entity() or the store."
    )

    # The value must be consistent between the two endpoints.
    assert listed[0]["created_by"] == created["created_by"], (
        f"created_by mismatch between POST ({created['created_by']!r}) "
        f"and GET ({listed[0]['created_by']!r}). "
        "The stored value is not being read back correctly."
    )

    # With auth active the value must be the actual user email, not None.
    assert created["created_by"] == _OWNER_EMAIL, (
        f"Expected created_by == {_OWNER_EMAIL!r} since the request carries "
        f"X-Forwarded-Email, got {created['created_by']!r}. "
        "The route is not threading the user identity through to the store."
    )


# ── Range-comment helpers ──────────────────────────────────────────────────────


def _add_range_comment(
    client: httpx.Client,
    session_id: str,
    *,
    path: str,
    start_index: int,
    end_index: int,
    body: str,
    anchor_content: str | None = None,
    email: str = _OWNER_EMAIL,
) -> dict[str, Any]:
    """POST a range comment with absolute content offsets and return the created dict.

    :param client: HTTP client pointed at the live server.
    :param session_id: Owning session.
    :param path: File path for the comment.
    :param start_index: 0-based absolute character offset where the anchor
        begins (inclusive).
    :param end_index: 0-based absolute character offset where the anchor
        ends (exclusive).
    :param body: Comment text.
    :param anchor_content: Optional plain-text snapshot of the selected range.
    :param email: User identity for ``X-Forwarded-Email``.
    :returns: The server-created comment dict.
    """
    resp = client.post(
        f"/v1/sessions/{session_id}/comments",
        json={
            "path": path,
            "body": body,
            "start_index": start_index,
            "end_index": end_index,
            "anchor_content": anchor_content,
        },
        headers={"X-Forwarded-Email": email},
    )
    resp.raise_for_status()
    return resp.json()


# ── Range-comment tests ────────────────────────────────────────────────────────


def test_comments_add_range_persists_indices(
    http_client: httpx.Client,
) -> None:
    """A range comment stores start_index and end_index exactly.

    Adds a comment with non-zero absolute character offsets, then verifies
    that both the POST response and the subsequent GET return the exact
    start_index and end_index that were sent.

    :param http_client: HTTP client pointed at the live server.
    """
    session_id = _create_session(http_client)

    comment = _add_range_comment(
        http_client,
        session_id,
        path="src/utils.py",
        start_index=42,
        end_index=87,
        body="This block should be extracted",
    )

    # POST response must echo both offset fields verbatim.
    assert comment["start_index"] == 42, (
        f"Expected start_index=42, got {comment['start_index']}. "
        "Server is not persisting start_index."
    )
    assert comment["end_index"] == 87, (
        f"Expected end_index=87, got {comment['end_index']}. Server is not persisting end_index."
    )

    # Verify the same values survive a round-trip via the GET endpoint.
    listed = _list_comments(http_client, session_id, path="src/utils.py")
    assert len(listed) == 1, f"Expected 1 comment, got {len(listed)}"
    c = listed[0]
    assert c["start_index"] == 42, f"GET start_index mismatch: {c['start_index']}"
    assert c["end_index"] == 87, f"GET end_index mismatch: {c['end_index']}"


def test_comments_send_includes_anchor_content_in_message(
    http_client: httpx.Client,
) -> None:
    """send includes the anchor_content snippet and file path in the formatted message.

    When anchor_content is provided the formatted_message must quote it as
    the location indicator so the agent can find the selected range.
    Also verifies that the comment body and file path are included.

    :param http_client: HTTP client pointed at the live server.
    """
    session_id = _create_session(http_client)

    comment = _add_range_comment(
        http_client,
        session_id,
        path="server.py",
        start_index=50,
        end_index=120,
        body="Extract this into a helper",
        anchor_content="def handle_request(req):",
    )

    send_resp = http_client.post(
        f"/v1/sessions/{session_id}/comments/send",
        json={"comment_ids": [comment["id"]]},
        headers={"X-Forwarded-Email": _OWNER_EMAIL},
    )
    send_resp.raise_for_status()
    formatted = send_resp.json()["formatted_message"]

    assert "def handle_request(req):" in formatted, (
        f"Expected anchor_content in formatted_message, got: {formatted!r}. "
        "The formatter is not including anchor_content as the location label."
    )
    assert "Extract this into a helper" in formatted, (
        f"Comment body missing from formatted_message: {formatted!r}"
    )
    assert "server.py" in formatted, (
        f"File path 'server.py' missing from formatted_message: {formatted!r}"
    )


# ── Permission-enforcement tests ───────────────────────────────────────────────


def test_comments_read_only_user_cannot_mutate(http_client: httpx.Client) -> None:
    """A user with read-only access can list comments but not mutate them.

    Owner creates the session and a comment. Reader is granted LEVEL_READ.
    Reader must get 403 on POST, PATCH, DELETE, and send; and 200 on GET.

    If this test fails, the ``require_access`` guard is missing or uses
    the wrong level in one of the mutating comment handlers.

    :param http_client: HTTP client pointed at the live server.
    """

    session_id = _create_session(http_client, email=_OWNER_EMAIL)

    # Grant reader read-only access.
    _grant_access(
        http_client,
        session_id,
        granter=_OWNER_EMAIL,
        target_user=_READER_EMAIL,
        level=LEVEL_READ,
    )

    # Owner adds a comment so there is something to act on.
    comment = _add_comment(
        http_client,
        session_id,
        path="src/main.py",
        body="Owner comment",
        line=1,
    )
    comment_id = comment["id"]

    # Reader must NOT be able to add a comment.
    add_resp = http_client.post(
        f"/v1/sessions/{session_id}/comments",
        json={
            "path": "src/main.py",
            "body": "Reader comment",
            "start_index": 0,
            "end_index": 0,
        },
        headers={"X-Forwarded-Email": _READER_EMAIL},
    )
    assert add_resp.status_code == 403, (
        f"Expected 403 for read-only user adding a comment, got {add_resp.status_code}. "
        "add_comment is not enforcing LEVEL_EDIT."
    )

    # Reader must NOT be able to patch a comment.
    patch_resp = http_client.patch(
        f"/v1/sessions/{session_id}/comments/{comment_id}",
        json={"body": "Reader edit"},
        headers={"X-Forwarded-Email": _READER_EMAIL},
    )
    assert patch_resp.status_code == 403, (
        f"Expected 403 for read-only user patching a comment, got {patch_resp.status_code}. "
        "update_comment is not enforcing LEVEL_EDIT."
    )

    # Reader must NOT be able to delete a comment.
    delete_resp = http_client.delete(
        f"/v1/sessions/{session_id}/comments/{comment_id}",
        headers={"X-Forwarded-Email": _READER_EMAIL},
    )
    assert delete_resp.status_code == 403, (
        f"Expected 403 for read-only user deleting a comment, got {delete_resp.status_code}. "
        "delete_comment is not enforcing LEVEL_EDIT."
    )

    # Reader must NOT be able to send comments to the agent.
    send_resp = http_client.post(
        f"/v1/sessions/{session_id}/comments/send",
        json={"comment_ids": [comment_id]},
        headers={"X-Forwarded-Email": _READER_EMAIL},
    )
    assert send_resp.status_code == 403, (
        f"Expected 403 for read-only user sending comments, got {send_resp.status_code}. "
        "send_to_agent is not enforcing LEVEL_EDIT."
    )

    # Reader CAN list comments (LEVEL_READ is sufficient for GET).
    list_resp = http_client.get(
        f"/v1/sessions/{session_id}/comments",
        headers={"X-Forwarded-Email": _READER_EMAIL},
    )
    assert list_resp.status_code == 200, (
        f"Expected 200 for read-only user listing comments, got {list_resp.status_code}. "
        "list_comments should allow LEVEL_READ access."
    )
    listed = list_resp.json()
    assert len(listed) == 1, (
        f"Reader should see the owner's comment, got {len(listed)} comment(s)."
    )

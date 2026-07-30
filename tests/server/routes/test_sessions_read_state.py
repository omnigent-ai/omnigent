"""Tests for the per-user read-state feature:

  * ``PUT /v1/sessions/{session_id}/read-state`` — set the caller's
    read-state for one session (returns ``204``).
  * ``viewer_last_seen`` / ``viewer_unread`` embedded per-user in the
    ``GET /v1/sessions`` list items (built by ``_build_session_list_item``).

Read state is per-user and in-memory on the server (module-level dicts in
``omnigent.server.routes.sessions``); each test resets those globals so
state doesn't leak between cases. Runs without auth (``permission_store``
is ``None``), so the caller is the single-user ``None`` identity and the
PUT's access check short-circuits.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.testclient import TestClient

from omnigent.entities import Conversation
from omnigent.errors import OmnigentError
from omnigent.server.routes import sessions as sessions_mod
from omnigent.server.routes.sessions import create_sessions_router


class _ConversationStore:
    """Conversation store stub — unused by the PUT when auth is off."""

    def get_conversation(self, conversation_id: str) -> None:
        """Return ``None`` (no conversation lookups happen without auth)."""
        return


class _AgentStore:
    """Agent store stub — present only to satisfy the router factory."""

    def get(self, agent_id: str) -> None:
        """Return ``None``."""
        return


def _build_app() -> FastAPI:
    """Build a FastAPI app exposing the sessions router with no auth."""
    router = create_sessions_router(
        conversation_store=_ConversationStore(),  # type: ignore[arg-type]
        agent_store=_AgentStore(),  # type: ignore[arg-type]
    )
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _handle(request: Request, exc: OmnigentError) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    app.include_router(router, prefix="/v1")
    return app


def _make_conversation(conv_id: str = "conv_a") -> Conversation:
    """A minimal session-shaped conversation for the list-item builder."""
    return Conversation(
        id=conv_id,
        created_at=100,
        updated_at=200,
        root_conversation_id=conv_id,
        title="A session",
        agent_id="ag_test",
    )


def _build_item(user_id: str | None, conv: Conversation) -> object:
    """Call the list-item builder the way ``GET /v1/sessions`` does."""
    return sessions_mod._build_session_list_item(
        conv,
        agent_names_by_id={"ag_test": "test-agent"},
        grants=[],
        user_id=user_id,
        user_is_admin=False,
        permissions_enabled=False,
        pending_count=0,
        child_session_ids=[],
        comments_fingerprint=None,
    )


@pytest.fixture(autouse=True)
def _reset_read_state() -> Iterator[None]:
    """Clear the module-level read-state caches around each test."""
    sessions_mod._read_last_seen.clear()
    sessions_mod._read_explicit_unread.clear()
    yield
    sessions_mod._read_last_seen.clear()
    sessions_mod._read_explicit_unread.clear()


def test_put_mark_unread_returns_204_and_updates_cache() -> None:
    """Marking unread persists the baseline + override and returns 204."""
    client = TestClient(_build_app())

    resp = client.put(
        "/v1/sessions/conv_a/read-state",
        json={"last_seen": 4_999, "unread": True},
    )
    assert resp.status_code == 204, resp.text
    assert resp.content == b""
    # The single-user (None) caller maps to the shared discovery key.
    key = sessions_mod._discovery_key(None)
    assert sessions_mod._read_last_seen[key]["conv_a"] == 4_999
    assert "conv_a" in sessions_mod._read_explicit_unread[key]


def test_put_mark_seen_clears_unread_and_advances_baseline() -> None:
    """Marking seen (unread=false) drops the override and moves last_seen up."""
    client = TestClient(_build_app())
    client.put("/v1/sessions/conv_a/read-state", json={"last_seen": 4_999, "unread": True})

    resp = client.put(
        "/v1/sessions/conv_a/read-state",
        json={"last_seen": 9_000, "unread": False},
    )
    assert resp.status_code == 204, resp.text
    key = sessions_mod._discovery_key(None)
    assert sessions_mod._read_last_seen[key]["conv_a"] == 9_000
    assert "conv_a" not in sessions_mod._read_explicit_unread.get(key, set())


def test_list_item_embeds_viewer_read_state() -> None:
    """``_build_session_list_item`` reflects the caller's read-state."""
    client = TestClient(_build_app())
    client.put("/v1/sessions/conv_a/read-state", json={"last_seen": 4_999, "unread": True})

    item = _build_item(None, _make_conversation("conv_a"))
    assert item.viewer_last_seen == 4_999  # type: ignore[attr-defined]
    assert item.viewer_unread is True  # type: ignore[attr-defined]


def test_list_item_defaults_when_user_never_saw_session() -> None:
    """A session the user never touched has no baseline and reads as seen."""
    item = _build_item(None, _make_conversation("conv_untouched"))
    assert item.viewer_last_seen is None  # type: ignore[attr-defined]
    assert item.viewer_unread is False  # type: ignore[attr-defined]


def test_read_state_is_scoped_per_user() -> None:
    """One user's read-state doesn't leak into another user's list items."""
    app = _build_app()
    client = TestClient(app)
    # Alice marks conv_a unread (her X-Forwarded-Email identifies her). With
    # auth off the server treats all callers as the shared user, so to prove
    # per-user scoping we write directly into Bob's and Alice's caches.
    sessions_mod._set_read_state("alice@example.com", "conv_a", 4_999, True)

    alice_item = _build_item("alice@example.com", _make_conversation("conv_a"))
    bob_item = _build_item("bob@example.com", _make_conversation("conv_a"))

    assert alice_item.viewer_unread is True  # type: ignore[attr-defined]
    assert bob_item.viewer_unread is False  # type: ignore[attr-defined]
    assert bob_item.viewer_last_seen is None  # type: ignore[attr-defined]
    del client, app


def test_prune_clears_read_state_across_all_users() -> None:
    """Pruning a session drops its read-state from every user's caches."""
    sessions_mod._set_read_state("alice@example.com", "conv_a", 4_999, True)
    sessions_mod._set_read_state("bob@example.com", "conv_a", 100, False)
    sessions_mod._set_read_state("alice@example.com", "conv_b", 200, True)  # untouched

    sessions_mod._prune_session_read_state("conv_a")

    # conv_a is gone for both users...
    assert sessions_mod._read_state_entry("alice@example.com", "conv_a") == (None, False)
    assert sessions_mod._read_state_entry("bob@example.com", "conv_a") == (None, False)
    # ...but other sessions are untouched.
    assert sessions_mod._read_state_entry("alice@example.com", "conv_b") == (200, True)

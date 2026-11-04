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


_FINISH_SESSION_IDS = ("conv_finish", "conv_finish_child")


def _clear_read_and_finish_state() -> None:
    sessions_mod._read_last_seen.clear()
    sessions_mod._read_explicit_unread.clear()
    sessions_mod._read_watched_turn_finish.clear()
    for session_id in _FINISH_SESSION_IDS:
        sessions_mod._session_status_cache.pop(session_id, None)
        sessions_mod._session_turn_finish_cache.pop(session_id, None)


@pytest.fixture(autouse=True)
def _reset_read_state() -> Iterator[None]:
    """Clear the module-level read-state and turn-finish caches around each test."""
    _clear_read_and_finish_state()
    yield
    _clear_read_and_finish_state()


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


def test_list_item_reports_a_booting_session_as_running() -> None:
    """A parked first message or an in-flight dispatch reads as ``running``."""
    from omnigent.runtime import pending_inputs
    from omnigent.server.routes._sessions import orchestration

    conv = _make_conversation("conv_boot")
    assert _build_item("u1", conv).status == "idle"  # type: ignore[attr-defined]

    pending_id = pending_inputs.record(
        "conv_boot", [{"type": "input_text", "text": "hi"}], created_by=None
    )
    try:
        assert _build_item("u1", conv).status == "running"  # type: ignore[attr-defined]
    finally:
        pending_inputs.resolve("conv_boot", pending_id)
    assert _build_item("u1", conv).status == "idle"  # type: ignore[attr-defined]

    with orchestration._mark_dispatch_in_flight("conv_boot"):
        assert _build_item("u1", conv).status == "running"  # type: ignore[attr-defined]
    assert _build_item("u1", conv).status == "idle"  # type: ignore[attr-defined]


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
    sessions_mod._set_read_state(
        "alice@example.com", "conv_a", 4_999, True, watched_turn_finish_id="p-1"
    )
    sessions_mod._set_read_state("bob@example.com", "conv_a", 100, False)
    sessions_mod._set_read_state("alice@example.com", "conv_b", 200, True)  # untouched

    sessions_mod._prune_session_read_state("conv_a")

    assert sessions_mod._watched_turn_finish_id("alice@example.com", "conv_a") is None

    # conv_a is gone for both users...
    assert sessions_mod._read_state_entry("alice@example.com", "conv_a") == (None, False)
    assert sessions_mod._read_state_entry("bob@example.com", "conv_a") == (None, False)
    # ...but other sessions are untouched.
    assert sessions_mod._read_state_entry("alice@example.com", "conv_b") == (200, True)


def test_turn_finish_id_changes_only_on_an_in_flight_to_terminal_edge() -> None:
    """Each finished turn gets a new id that every viewer of the row sees."""
    conv = _make_conversation("conv_finish")
    assert _build_item(None, conv).last_turn_finish_id is None  # type: ignore[attr-defined]

    sessions_mod._publish_status("conv_finish", "running")
    assert _build_item(None, conv).last_turn_finish_id is None  # type: ignore[attr-defined]
    sessions_mod._publish_status("conv_finish", "idle")
    first_finish = _build_item(None, conv).last_turn_finish_id  # type: ignore[attr-defined]
    assert first_finish is not None

    sessions_mod._publish_status("conv_finish", "idle")
    assert _build_item(None, conv).last_turn_finish_id == first_finish  # type: ignore[attr-defined]

    sessions_mod._publish_status("conv_finish", "waiting")
    sessions_mod._publish_status("conv_finish", "failed", failure_origin="test_turn_failed")
    second_finish = _build_item(None, conv).last_turn_finish_id  # type: ignore[attr-defined]
    assert second_finish not in (None, first_finish)

    # A trailing idle cannot clear a sticky failure, so the finish stays the same.
    sessions_mod._publish_status("conv_finish", "idle")
    assert _build_item(None, conv).status == "failed"  # type: ignore[attr-defined]
    assert _build_item(None, conv).last_turn_finish_id == second_finish  # type: ignore[attr-defined]
    assert (
        _build_item("alice@example.com", conv).last_turn_finish_id  # type: ignore[attr-defined]
        == second_finish
    )

    sessions_mod._relinquish_session_live_state("conv_finish")
    assert _build_item(None, conv).last_turn_finish_id is None  # type: ignore[attr-defined]


def test_parent_turn_finish_id_follows_its_childs_finish() -> None:
    """A parent row that stops running when its child finishes reports that finish."""
    sessions_mod._publish_status("conv_finish", "running")
    sessions_mod._publish_status("conv_finish", "idle")
    parent_finish = sessions_mod._session_turn_finish_id("conv_finish", [])

    sessions_mod._publish_status("conv_finish_child", "running")
    assert sessions_mod._session_status_with_child_rollup(
        "conv_finish", ["conv_finish_child"]
    ) == ("running")
    sessions_mod._publish_status("conv_finish_child", "idle")

    rolled_up_finish = sessions_mod._session_turn_finish_id("conv_finish", ["conv_finish_child"])
    assert rolled_up_finish == sessions_mod._session_turn_finish_id("conv_finish_child", [])
    assert rolled_up_finish != parent_finish


def test_put_records_the_watched_turn_finish_for_the_caller() -> None:
    """A watched finish id is stored per user and kept when a later PUT omits it."""
    client = TestClient(_build_app())
    conv = _make_conversation("conv_a")

    resp = client.put(
        "/v1/sessions/conv_a/read-state",
        json={"last_seen": 5_000, "unread": False, "watched_turn_finish_id": "p-1"},
    )
    assert resp.status_code == 204, resp.text
    assert _build_item(None, conv).viewer_watched_turn_finish_id == "p-1"  # type: ignore[attr-defined]

    resp = client.put("/v1/sessions/conv_a/read-state", json={"last_seen": 6_000, "unread": False})
    assert resp.status_code == 204, resp.text
    item = _build_item(None, conv)
    assert item.viewer_last_seen == 6_000  # type: ignore[attr-defined]
    assert item.viewer_watched_turn_finish_id == "p-1"  # type: ignore[attr-defined]

    sessions_mod._set_read_state(
        "alice@example.com", "conv_a", 1, False, watched_turn_finish_id="p-2"
    )
    assert (
        _build_item("alice@example.com", conv).viewer_watched_turn_finish_id  # type: ignore[attr-defined]
        == "p-2"
    )
    assert _build_item("bob@example.com", conv).viewer_watched_turn_finish_id is None  # type: ignore[attr-defined]


def test_put_keeps_the_newer_watched_turn_finish() -> None:
    """A delayed acknowledgment of an earlier finish does not replace a newer one."""
    client = TestClient(_build_app())
    conv = _make_conversation("conv_finish")
    finish_ids = []
    for _ in range(2):
        sessions_mod._publish_status("conv_finish", "running")
        sessions_mod._publish_status("conv_finish", "idle")
        finish_ids.append(_build_item(None, conv).last_turn_finish_id)  # type: ignore[attr-defined]
    earlier_finish, current_finish = finish_ids

    for watched in (current_finish, earlier_finish):
        resp = client.put(
            "/v1/sessions/conv_finish/read-state",
            json={"last_seen": 5_000, "unread": False, "watched_turn_finish_id": watched},
        )
        assert resp.status_code == 204, resp.text

    assert _build_item(None, conv).viewer_watched_turn_finish_id == current_finish  # type: ignore[attr-defined]

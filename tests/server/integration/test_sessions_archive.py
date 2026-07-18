"""Integration tests for session archive lifecycle and agent contents download.

Covers:
- ``PATCH /v1/sessions/{id}`` with ``archived=True/False``
- ``GET /v1/sessions`` with ``include_archived`` filtering
- ``GET /v1/sessions/{id}/agent/contents`` returning a valid gzip tarball

Uses the shared ``client`` fixture from ``tests/server/conftest.py``
(real stores + mock LLM) so the tests hit the real route-to-store
pipeline without subprocesses.
"""

from __future__ import annotations

import gzip
import io
import tarfile
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from omnigent.server.routes import sessions as sessions_module
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from tests.server.helpers import create_test_session

pytestmark = pytest.mark.asyncio


# ── Archive / unarchive lifecycle ────────────────────────


async def test_session_not_archived_by_default(
    client: httpx.AsyncClient,
) -> None:
    """A freshly created session has ``archived=False``."""
    session = await create_test_session(client, name="archive-default")
    assert session["archived"] is False


async def test_archive_hides_session_from_default_listing(
    client: httpx.AsyncClient,
) -> None:
    """Archiving a session removes it from the default GET /v1/sessions listing."""
    session = await create_test_session(client, name="archive-hide")
    session_id = session["id"]

    # Archive it.
    patch_resp = await client.patch(
        f"/v1/sessions/{session_id}",
        json={"archived": True},
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["archived"] is True

    # Default listing (include_archived=False) should not contain it.
    listing = await client.get("/v1/sessions")
    assert listing.status_code == 200
    listed_ids = [s["id"] for s in listing.json()["data"]]
    assert session_id not in listed_ids


async def test_archived_session_appears_with_include_archived(
    client: httpx.AsyncClient,
) -> None:
    """An archived session is returned when ``include_archived=True``."""
    session = await create_test_session(client, name="archive-include")
    session_id = session["id"]

    await client.patch(
        f"/v1/sessions/{session_id}",
        json={"archived": True},
    )

    listing = await client.get("/v1/sessions", params={"include_archived": "true"})
    assert listing.status_code == 200
    listed_ids = [s["id"] for s in listing.json()["data"]]
    assert session_id in listed_ids


async def test_unarchive_restores_session_to_default_listing(
    client: httpx.AsyncClient,
) -> None:
    """Unarchiving a session makes it visible in the default listing again."""
    session = await create_test_session(client, name="archive-restore")
    session_id = session["id"]

    # Archive then unarchive.
    await client.patch(f"/v1/sessions/{session_id}", json={"archived": True})
    patch_resp = await client.patch(
        f"/v1/sessions/{session_id}",
        json={"archived": False},
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["archived"] is False

    # Back in the default listing.
    listing = await client.get("/v1/sessions")
    assert listing.status_code == 200
    listed_ids = [s["id"] for s in listing.json()["data"]]
    assert session_id in listed_ids


# ── Best-effort stop before archive ───────────────────────


async def test_archive_running_session_attempts_stop(
    client: httpx.AsyncClient,
) -> None:
    """Archiving a running session calls ``_stop_session_via_runner``."""
    session = await create_test_session(client, name="archive-running")
    session_id = session["id"]

    mock_stop = AsyncMock(return_value=True)
    sessions_module._session_status_cache[session_id] = "running"
    try:
        with patch.object(sessions_module, "_stop_session_via_runner", mock_stop):
            resp = await client.patch(
                f"/v1/sessions/{session_id}",
                json={"archived": True},
            )
        assert resp.status_code == 200
        assert resp.json()["archived"] is True
        mock_stop.assert_awaited_once()
    finally:
        sessions_module._session_status_cache.pop(session_id, None)


async def test_archive_idle_parent_stops_running_child(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Archiving an idle parent with a running child stops the child.

    Regression test: ``_best_effort_stop`` previously used the child
    rollup only to decide whether to act, then always issued the stop
    against the parent's own session id. A parent that has already gone
    idle while its sub-agent child keeps running would get a no-op stop,
    leaving the child orphaned once the parent (and its DB row, via the
    cascading subtree delete/archive) is gone.
    """
    session = await create_test_session(client, name="archive-idle-parent-child")
    session_id = session["id"]

    conv_store = SqlAlchemyConversationStore(db_uri)
    child = conv_store.create_conversation(
        kind="sub_agent",
        title="researcher:auth",
        parent_conversation_id=session_id,
        agent_id=session["agent_id"],
    )

    mock_stop = AsyncMock(return_value=True)
    sessions_module._session_status_cache[child.id] = "running"
    try:
        with patch.object(sessions_module, "_stop_session_via_runner", mock_stop):
            resp = await client.patch(
                f"/v1/sessions/{session_id}",
                json={"archived": True},
            )
        assert resp.status_code == 200
        assert resp.json()["archived"] is True
        # The child must be the one stopped, not the (idle) parent.
        mock_stop.assert_awaited_once()
        assert mock_stop.await_args is not None
        assert mock_stop.await_args.args[0] == child.id
    finally:
        sessions_module._session_status_cache.pop(child.id, None)


async def test_archive_proceeds_when_stop_fails(
    client: httpx.AsyncClient,
) -> None:
    """Archive succeeds even when the runner stop raises."""
    session = await create_test_session(client, name="archive-stop-fail")
    session_id = session["id"]

    mock_stop = AsyncMock(side_effect=ConnectionError("runner gone"))
    sessions_module._session_status_cache[session_id] = "running"
    try:
        with patch.object(sessions_module, "_stop_session_via_runner", mock_stop):
            resp = await client.patch(
                f"/v1/sessions/{session_id}",
                json={"archived": True},
            )
        assert resp.status_code == 200
        assert resp.json()["archived"] is True
    finally:
        sessions_module._session_status_cache.pop(session_id, None)


async def test_archive_proceeds_when_child_lookup_fails(
    client: httpx.AsyncClient,
) -> None:
    """Archive succeeds even when the child-id DB lookup raises."""
    session = await create_test_session(client, name="archive-db-fail")
    session_id = session["id"]

    sessions_module._session_status_cache[session_id] = "running"
    try:
        with patch.object(
            sessions_module,
            "_best_effort_stop",
            wraps=sessions_module._best_effort_stop,
        ):
            orig = sessions_module._best_effort_stop

            async def _patched_stop(sid, cs, rr):
                with patch.object(
                    cs,
                    "list_child_conversation_ids_by_parent",
                    side_effect=RuntimeError("transient DB error"),
                ):
                    await orig(sid, cs, rr)

            with patch.object(sessions_module, "_best_effort_stop", _patched_stop):
                resp = await client.patch(
                    f"/v1/sessions/{session_id}",
                    json={"archived": True},
                )
        assert resp.status_code == 200
        assert resp.json()["archived"] is True
    finally:
        sessions_module._session_status_cache.pop(session_id, None)


async def test_archive_idle_session(
    client: httpx.AsyncClient,
) -> None:
    """An idle session can be archived normally (no stop needed)."""
    session = await create_test_session(client, name="archive-idle")
    session_id = session["id"]

    mock_stop = AsyncMock()
    with patch.object(sessions_module, "_stop_session_via_runner", mock_stop):
        resp = await client.patch(
            f"/v1/sessions/{session_id}",
            json={"archived": True},
        )
    assert resp.status_code == 200
    assert resp.json()["archived"] is True
    mock_stop.assert_not_awaited()


async def test_unarchive_skips_stop(
    client: httpx.AsyncClient,
) -> None:
    """Unarchiving does not attempt a stop, even if the session is running."""
    session = await create_test_session(client, name="unarchive-running")
    session_id = session["id"]

    await client.patch(f"/v1/sessions/{session_id}", json={"archived": True})

    mock_stop = AsyncMock()
    sessions_module._session_status_cache[session_id] = "running"
    try:
        with patch.object(sessions_module, "_stop_session_via_runner", mock_stop):
            resp = await client.patch(
                f"/v1/sessions/{session_id}",
                json={"archived": False},
            )
        assert resp.status_code == 200
        assert resp.json()["archived"] is False
        mock_stop.assert_not_awaited()
    finally:
        sessions_module._session_status_cache.pop(session_id, None)


# ── Agent contents download ──────────────────────────────


async def test_agent_contents_returns_valid_gzip_tarball(
    client: httpx.AsyncClient,
) -> None:
    """GET /v1/sessions/{id}/agent/contents returns a valid tar.gz bundle."""
    session = await create_test_session(client, name="contents-download")
    session_id = session["id"]

    resp = await client.get(f"/v1/sessions/{session_id}/agent/contents")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/gzip"

    # Verify the bytes are valid gzip.
    decompressed = gzip.decompress(resp.content)
    assert len(decompressed) > 0

    # Verify the bytes are a valid tar archive containing config.yaml.
    with tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz") as tf:
        names = tf.getnames()
        assert "config.yaml" in names


async def test_agent_contents_404_for_nonexistent_session(
    client: httpx.AsyncClient,
) -> None:
    """GET /v1/sessions/{id}/agent/contents returns 404 for a missing session."""
    resp = await client.get("/v1/sessions/conv_nonexistent/agent/contents")
    assert resp.status_code == 404

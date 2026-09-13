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

import asyncio
import gzip
import io
import tarfile
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from omnigent.server.routes import sessions as _sessions_facade
from omnigent.server.routes._sessions import common as _sessions_common
from omnigent.server.routes._sessions import orchestration as _sessions_orchestration
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


async def _drain_detached_stops() -> None:
    """
    Wait out the archive PATCH's detached best-effort stop.

    The handler spawns the stop as a retained background task and responds
    immediately, so assertions about the stop must let it finish first.
    """
    await asyncio.gather(
        *list(_sessions_orchestration._detached_stop_tasks),
        return_exceptions=True,
    )


async def test_archive_running_session_attempts_stop(
    client: httpx.AsyncClient,
) -> None:
    """Archiving a running session calls ``_stop_session_via_runner``."""
    session = await create_test_session(client, name="archive-running")
    session_id = session["id"]

    mock_stop = AsyncMock(return_value=True)
    _sessions_common._session_status_cache[session_id] = "running"
    try:
        with patch.object(_sessions_orchestration, "_stop_session_via_runner", mock_stop):
            resp = await client.patch(
                f"/v1/sessions/{session_id}",
                json={"archived": True},
            )
            await _drain_detached_stops()
        assert resp.status_code == 200
        assert resp.json()["archived"] is True
        mock_stop.assert_awaited_once()
    finally:
        _sessions_common._session_status_cache.pop(session_id, None)


async def test_archive_does_not_block_on_slow_stop(
    client: httpx.AsyncClient,
) -> None:
    """
    The PATCH responds while the best-effort stop is still in flight.

    The stop carries per-runner timeouts of several seconds against a
    wedged or asleep runner; awaiting it inline made every archive of a
    running session eat those timeouts before the flag flipped. The
    handler detaches the stop instead — the response must not wait for
    it, and the stop must still run.
    """
    session = await create_test_session(client, name="archive-slow-stop")
    session_id = session["id"]

    release = asyncio.Event()
    stopped: list[str] = []

    async def _parked_stop(sid: str, *_args: object) -> None:
        stopped.append(sid)
        await release.wait()

    _sessions_common._session_status_cache[session_id] = "running"
    try:
        with patch.object(_sessions_facade, "_best_effort_stop", _parked_stop):
            # Would exhaust the timeout here if the handler awaited the
            # stop inline (the fake stop parks until released below).
            resp = await asyncio.wait_for(
                client.patch(f"/v1/sessions/{session_id}", json={"archived": True}),
                timeout=5.0,
            )
            assert resp.status_code == 200
            assert resp.json()["archived"] is True
            # The detached task starts on a subsequent loop pass and parks
            # on the release gate — the stop still runs.
            for _ in range(100):
                if stopped:
                    break
                await asyncio.sleep(0)
            assert stopped == [session_id]
            release.set()
            await _drain_detached_stops()
    finally:
        _sessions_common._session_status_cache.pop(session_id, None)


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
    _sessions_common._session_status_cache[child.id] = "running"
    try:
        with patch.object(_sessions_orchestration, "_stop_session_via_runner", mock_stop):
            resp = await client.patch(
                f"/v1/sessions/{session_id}",
                json={"archived": True},
            )
            await _drain_detached_stops()
        assert resp.status_code == 200
        assert resp.json()["archived"] is True
        # The child must be the one stopped, not the (idle) parent.
        mock_stop.assert_awaited_once()
        assert mock_stop.await_args is not None
        assert mock_stop.await_args.args[0] == child.id
    finally:
        _sessions_common._session_status_cache.pop(child.id, None)


async def test_archive_tears_down_host_spawned_runner(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    Archiving a host-spawned session tears down its dedicated runner.

    Killing the pane alone leaves the host-launched runner connected, so
    ``/health`` keeps reporting ``runner_online: true`` and a later
    message hangs on "working" against a dead pane. Archive is the one
    lifecycle action with no client-side stop, so the server carries the
    teardown itself rather than racing a second stop against the same
    runner.
    """
    session = await create_test_session(client, name="archive-host-spawned")
    session_id = session["id"]

    conv_store = SqlAlchemyConversationStore(db_uri)
    conv_store.set_host_id(
        session_id, "a1b2c3d4e5f61234567890abcdef0123", workspace="/tmp/archive-ws"
    )
    conv_store.set_runner_id(session_id, "b1b2c3d4e5f61234567890abcdef0123")

    mock_teardown = AsyncMock(return_value=True)
    _sessions_common._session_status_cache[session_id] = "running"
    try:
        with (
            patch.object(
                _sessions_orchestration, "_stop_session_via_runner", AsyncMock(return_value=True)
            ),
            patch.object(_sessions_facade, "_stop_session_host_runner", mock_teardown),
        ):
            resp = await client.patch(
                f"/v1/sessions/{session_id}",
                json={"archived": True},
            )
            await _drain_detached_stops()
        assert resp.status_code == 200
        assert resp.json()["archived"] is True
        mock_teardown.assert_awaited_once()
        assert mock_teardown.await_args is not None
        assert mock_teardown.await_args.args[:3] == (
            session_id,
            "a1b2c3d4e5f61234567890abcdef0123",
            "b1b2c3d4e5f61234567890abcdef0123",
        )
    finally:
        _sessions_common._session_status_cache.pop(session_id, None)
        _sessions_common._intentional_stop_sessions.discard(session_id)


async def test_failed_archive_leaves_session_running(
    client: httpx.AsyncClient,
) -> None:
    """
    A rejected archive PATCH must not stop the session.

    The stop is spawned only after the archived flag commits, so a
    request that fails a later validation (here a server-derived
    per-user pin key) leaves the session both unarchived and untouched.
    """
    session = await create_test_session(client, name="archive-rejected")
    session_id = session["id"]

    mock_stop = AsyncMock(return_value=True)
    _sessions_common._session_status_cache[session_id] = "running"
    try:
        with patch.object(_sessions_orchestration, "_stop_session_via_runner", mock_stop):
            resp = await client.patch(
                f"/v1/sessions/{session_id}",
                json={"archived": True, "labels": {"omnigent.pinned.someone": "1"}},
            )
            await _drain_detached_stops()
        assert resp.status_code >= 400
        mock_stop.assert_not_awaited()
        listed = await client.get(f"/v1/sessions/{session_id}")
        assert listed.json()["archived"] is False
    finally:
        _sessions_common._session_status_cache.pop(session_id, None)


async def test_archive_proceeds_when_stop_fails(
    client: httpx.AsyncClient,
) -> None:
    """Archive succeeds even when the runner stop raises."""
    session = await create_test_session(client, name="archive-stop-fail")
    session_id = session["id"]

    mock_stop = AsyncMock(side_effect=ConnectionError("runner gone"))
    _sessions_common._session_status_cache[session_id] = "running"
    try:
        with patch.object(_sessions_orchestration, "_stop_session_via_runner", mock_stop):
            resp = await client.patch(
                f"/v1/sessions/{session_id}",
                json={"archived": True},
            )
            await _drain_detached_stops()
        assert resp.status_code == 200
        assert resp.json()["archived"] is True
    finally:
        _sessions_common._session_status_cache.pop(session_id, None)


async def test_archive_proceeds_when_child_lookup_fails(
    client: httpx.AsyncClient,
) -> None:
    """Archive succeeds even when the child-id DB lookup raises."""
    session = await create_test_session(client, name="archive-db-fail")
    session_id = session["id"]

    _sessions_common._session_status_cache[session_id] = "running"
    try:
        with patch.object(
            _sessions_orchestration,
            "_best_effort_stop",
            wraps=_sessions_orchestration._best_effort_stop,
        ):
            orig = _sessions_orchestration._best_effort_stop

            async def _patched_stop(sid, cs, rr):
                with patch.object(
                    cs,
                    "list_child_conversation_ids_by_parent",
                    side_effect=RuntimeError("transient DB error"),
                ):
                    await orig(sid, cs, rr)

            with patch.object(_sessions_facade, "_best_effort_stop", _patched_stop):
                resp = await client.patch(
                    f"/v1/sessions/{session_id}",
                    json={"archived": True},
                )
                await _drain_detached_stops()
        assert resp.status_code == 200
        assert resp.json()["archived"] is True
    finally:
        _sessions_common._session_status_cache.pop(session_id, None)


async def test_archive_idle_session(
    client: httpx.AsyncClient,
) -> None:
    """An idle session can be archived normally (no stop needed)."""
    session = await create_test_session(client, name="archive-idle")
    session_id = session["id"]

    mock_stop = AsyncMock()
    with patch.object(_sessions_orchestration, "_stop_session_via_runner", mock_stop):
        resp = await client.patch(
            f"/v1/sessions/{session_id}",
            json={"archived": True},
        )
        await _drain_detached_stops()
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
    _sessions_common._session_status_cache[session_id] = "running"
    try:
        with patch.object(_sessions_orchestration, "_stop_session_via_runner", mock_stop):
            resp = await client.patch(
                f"/v1/sessions/{session_id}",
                json={"archived": False},
            )
        assert resp.status_code == 200
        assert resp.json()["archived"] is False
        mock_stop.assert_not_awaited()
    finally:
        _sessions_common._session_status_cache.pop(session_id, None)


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

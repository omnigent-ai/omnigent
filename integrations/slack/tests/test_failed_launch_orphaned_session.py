"""A failed runner launch after ``create_session`` must not leak the session.

``_ensure_session`` creates the session, then ``launch_runner`` raises (host
offline), so the ``upsert_session`` that would record the thread->session
binding never runs. Unless the bot cleans up, the created session is orphaned:
nothing references it, and the user's retry mints a *second* brand-new session
— one leaked session per failed launch attempt.

This drives the REAL ``SlackOmnigentService`` + REAL ``OmnigentClient`` (real
httpx) against the shared ``FakeOmnigentServer`` respx contract, exactly like
``test_integration.py``, with ``launch_status = 409`` (host offline). Slack is
mocked with a ``RecordingSlackClient``.

The guard is fix-shape-agnostic: after a failed-launch turn, the session the
bot created on the server must NOT be orphaned — it must be either recorded in
the store (a retry can find it) or deleted on the server (cleaned up). On a
buggy build it is neither, so these tests fail.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import respx
from fakes import FakeOmnigentServer, RecordingSlackClient
from omnigent_slack.models import ThreadKey, UserConfig
from omnigent_slack.omnigent import OmnigentClientPool
from omnigent_slack.service import SlackOmnigentService
from omnigent_slack.store import SQLiteStore

_SERVER = "http://omnigent.test"
_WAIT_TIMEOUT_S = 10.0


async def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "store.sqlite3")
    await store.initialize()
    return store


async def _configure_user(
    store: SQLiteStore, team_id: str, user_id: str, *, agent_id: str = "ag_1"
) -> None:
    await store.upsert_user_config(
        team_id,
        user_id,
        UserConfig(
            agent_id=agent_id,
            agent_name="debby",
            workspace="/home/bot/work",
            host_id="h1",
        ),
    )


class _NoopSetup:
    """SetupFlow stand-in for turns where the user is already configured."""

    async def prompt_unconfigured(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("configured user should not be prompted to set up")

    async def prompt_relogin(self, *args: object, **kwargs: object) -> bool:
        return True


async def _wait_for_turns(service: SlackOmnigentService, timeout: float = _WAIT_TIMEOUT_S) -> None:
    """Await the spawned turn tasks (event-driven; timeout only bounds a hang)."""
    tasks = list(service._turn_tasks)
    if not tasks:
        return
    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=timeout)


@respx.mock
async def test_failed_launch_after_create_does_not_leak_orphan_session(tmp_path: Path) -> None:
    """A create-then-failed-launch must not leave an orphaned server-side session.

    New thread -> ``create_session`` succeeds -> ``launch_runner`` 409s (host
    offline). The bot honestly reports the failure, but the created session must
    be either recorded (so a retry reuses it) or deleted (cleaned up) — never
    left orphaned.
    """
    server = FakeOmnigentServer(_SERVER)
    server.launch_status = 409  # host offline: launch raises AFTER create succeeds
    server.install(respx.mock)

    store = await _store(tmp_path)
    await _configure_user(store, "T1", "U1")
    key = ThreadKey(team_id="T1", channel_id="C1", thread_ts="100.1")

    pool = OmnigentClientPool()
    service = SlackOmnigentService(store=store, pool=pool, setup=_NoopSetup(), server_url=_SERVER)
    client = RecordingSlackClient()

    try:
        await service.handle_app_mention(
            body={"team_id": "T1", "event_id": "Ev1"},
            event={"channel": "C1", "ts": "100.1", "user": "U1", "text": "<@B1> go"},
            client=client,
            context={"bot_user_id": "B1"},
        )
        await _wait_for_turns(service)
    finally:
        await service.shutdown()
        await pool.aclose_all()

    # The session WAS created and the launch WAS attempted (and 409'd) — the
    # preconditions for the leak.
    create_count = server.paths("POST").count("/v1/sessions")
    assert create_count == 1, f"expected exactly one session create, saw {create_count}"
    server.assert_request("POST", "/v1/hosts/h1/runners")
    # The user saw an honest failure (not a silent success).
    assert not any("/stream" in p for p in server.paths("GET"))

    # THE GUARD: the created session must not be orphaned. Either the binding was
    # recorded (a retry can find it) or the orphan was deleted on the server.
    recorded = await store.get_session(key)
    delete_paths = server.paths("DELETE")
    assert recorded is not None or len(delete_paths) >= 1, (
        "leaked orphaned session: create_session succeeded and launch_runner "
        "failed, but the created session was neither recorded in the store "
        f"(get_session -> {recorded!r}) nor deleted on the server "
        f"(DELETE calls: {delete_paths})"
    )


@respx.mock
async def test_retry_after_failed_launch_does_not_accumulate_orphans(tmp_path: Path) -> None:
    """A user's retry after a failed launch must not accumulate orphaned sessions.

    On a buggy build the first failed launch leaves an unrecorded session, so
    the retry mints a SECOND brand-new session — two server-side sessions for one
    thread, neither referenced. A correct build either reuses the recorded
    session on retry (one create) or cleans each orphan up (delete per create),
    so no created session is ever left orphaned.
    """
    server = FakeOmnigentServer(_SERVER)
    server.launch_status = 409
    server.install(respx.mock)

    store = await _store(tmp_path)
    await _configure_user(store, "T1", "U1")
    key = ThreadKey(team_id="T1", channel_id="C1", thread_ts="100.1")

    pool = OmnigentClientPool()
    service = SlackOmnigentService(store=store, pool=pool, setup=_NoopSetup(), server_url=_SERVER)
    client = RecordingSlackClient()

    try:
        # First attempt, then the user retries the SAME thread.
        for event_id in ("Ev1", "Ev2"):
            await service.handle_app_mention(
                body={"team_id": "T1", "event_id": event_id},
                event={"channel": "C1", "ts": "100.1", "user": "U1", "text": "<@B1> go"},
                client=client,
                context={"bot_user_id": "B1"},
            )
            await _wait_for_turns(service)
    finally:
        await service.shutdown()
        await pool.aclose_all()

    create_count = server.paths("POST").count("/v1/sessions")
    recorded = await store.get_session(key)
    delete_count = len(server.paths("DELETE"))

    # No created session may be left orphaned: everything the bot created must be
    # accounted for — cleaned up (a delete per create) or recorded (so the retry
    # reused it, keeping the create count at 1).
    orphans = create_count - delete_count - (1 if recorded is not None else 0)
    assert orphans <= 0, (
        f"retry accumulated orphaned sessions: {create_count} session(s) created, "
        f"{delete_count} deleted, recorded binding={recorded!r} "
        f"-> {orphans} orphan(s) leaked"
    )

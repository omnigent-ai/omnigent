"""E2E regression: a crashed host must read ``host_online: false``.

Host liveness per session is exposed by ``GET /health`` as the
``host_online`` field (the open-session view uses it to tell "runner
asleep, just send a message" — host alive — apart from "host offline,
reconnect/fork"). The store's host-liveness check
(:meth:`HostStore.online_host_ids`, via ``_bulk_session_liveness``)
treats a host as live only when its row is ``status == 'online'``
**and** its last-seen (``updated_at``) is within
``HOST_LIVENESS_TTL_S``.

Without the freshness gate, host liveness would trust
``hosts.status == 'online'`` alone. That status is only ever flipped to
``'offline'`` by the host tunnel handler's disconnect path
(``host_tunnel.py`` finally/except), and there is no startup
reconciliation — so a host that died without a graceful disconnect
(``kill -9``, OOM, a deploy/replica restart, a host-side crash) leaves
its row stranded ``'online'`` indefinitely. The heartbeat
(``host_store.heartbeat`` on the tunnel ping loop) keeps ``updated_at``
fresh while the host lives; once it stops, the freshness window lapses
and the host correctly reads offline.

This test guards that gate: a host connects (row → online), a session
binds to it, then the host crashes (its last-seen heartbeat recedes far
into the past, no graceful disconnect ran). ``/health`` must report
``host_online: false`` for the session. The companion test pins the
anti-flap direction: a host seen within the window still reads
``host_online: true``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import update
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlHost
from omnigent.db.utils import get_or_create_engine, now_epoch
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_store import HOST_LIVENESS_TTL_S, HostStore
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture()
async def host_aware_client(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
) -> AsyncIterator[httpx.AsyncClient]:
    """An HTTP client over an app wired with a DB-backed ``host_store``.

    The default ``client`` fixture builds the app without a
    ``host_store``, so ``/health``'s ``host_online`` falls back to the
    per-replica in-memory registry — empty unless a real host tunnel is
    open. Host-liveness freshness, by contrast, lives in the DB
    (``hosts.status`` + ``updated_at`` via ``HostStore.online_host_ids``).
    To exercise that gate end to end we need the cross-replica path the
    production deploy uses: ``create_app(..., host_store=...)``.

    :param runtime_init: Ensures runtime singletons are initialized.
    :param db_uri: SQLite URI shared with the test's stores.
    :param tmp_path: Per-test scratch dir for artifact/cache stores.
    :returns: An ``httpx.AsyncClient`` bound to the host-aware app.
    """
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(
            artifact_store=artifact_store,
            cache_dir=tmp_path / "cache",
        ),
        comment_store=SqlAlchemyCommentStore(db_uri),
        host_store=HostStore(db_uri),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield client


_HOST_ID = "9b2ec6de30f5e014c7056afe505510c3"

# How far in the past to push the host's last-seen to model a host
# that crashed "a while ago". The freshness window (on the order of the
# 90 s ping-miss threshold) is far smaller than this, so the host is
# unambiguously stale.
_CRASHED_LONG_AGO_S = 3600


async def _session_host_online(client: httpx.AsyncClient, session_id: str) -> bool | None:
    """Return the ``host_online`` value ``GET /health`` reports for a session.

    :param client: Test HTTP client wired to the app.
    :param session_id: Session whose host liveness to read,
        e.g. ``"d1f9214d74c38b9f9a9db17ed8352dc4"``.
    :returns: ``True`` when the bound host is online and fresh,
        ``False`` when it is offline/stale, ``None`` when the session
        has no host binding.
    """
    resp = await client.get("/health", params={"session_id": session_id})
    assert resp.status_code == 200, f"health failed: {resp.text}"
    data: dict[str, Any] = resp.json()
    return data["session"]["host_online"]


def _age_host_last_seen(db_uri: str, host_id: str, age_seconds: int) -> None:
    """Push a host's last-seen timestamp into the past, leaving status.

    Models a host that connected (row is ``online``) and then crashed
    without a graceful disconnect: the row is never updated again, so
    its last-seen recedes into the past while ``status`` stays
    ``'online'``. ``updated_at`` is the host row's last-touch timestamp
    (the column the freshness gate uses).

    :param db_uri: SQLite URI shared with the app's stores.
    :param host_id: Host whose timestamp to age.
    :param age_seconds: How many seconds in the past to set it.
    """
    engine = get_or_create_engine(db_uri)
    with Session(engine) as session:
        session.execute(
            update(SqlHost)
            .where(SqlHost.host_id == host_id)
            .values(updated_at=now_epoch() - age_seconds)
        )
        session.commit()


async def test_crashed_host_session_reads_host_offline(
    host_aware_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A session whose host crashed must report ``host_online: false``.

    Guards the stale-``online`` fix end to end through
    ``GET /health``'s ``host_online``. Regression being guarded: before
    the freshness gate, host liveness was read straight from
    ``hosts.status``, so a host that never ran its disconnect cleanup
    stayed ``online`` forever and its sessions read ``host_online: true``
    indefinitely — the open-session view would tell the user to "send a
    message to wake the runner" against a dead host instead of offering
    reconnect/fork.
    """
    conv_store = SqlAlchemyConversationStore(db_uri)
    host_store = HostStore(db_uri)

    # A real session bound to an agent (so it satisfies the endpoint's
    # has_agent_id=True filter, same as a UI-created session).
    agent = await create_test_agent(host_aware_client)
    session_id = agent["_session_id"]

    # The host connects: this is exactly what the tunnel handler does on
    # host.hello — upserts the row to status='online'. No live tunnel /
    # heartbeat is modeled, which is precisely the post-crash DB state.
    host_store.upsert_on_connect(
        host_id=_HOST_ID, name="alice-laptop", user_id=RESERVED_USER_LOCAL
    )
    conv_store.set_host_id(session_id, _HOST_ID, workspace="/tmp/ws")

    # Baseline: while the host is freshly online, host_online is True. If
    # this fails, the binding or the liveness path itself is broken (not
    # the bug under test).
    assert await _session_host_online(host_aware_client, session_id) is True, (
        "freshly-online host: host_online should be True"
    )

    # The host crashes: no graceful disconnect runs, so set_offline is
    # never called and the row stays 'online', but its last-seen recedes
    # far into the past.
    _age_host_last_seen(db_uri, _HOST_ID, _CRASHED_LONG_AGO_S)

    # A host last seen an hour ago is not live, so host_online must flip
    # to False. Before the freshness gate this would have stayed True —
    # the check trusted the stale 'online' status with no TTL.
    assert await _session_host_online(host_aware_client, session_id) is False, (
        "a host last seen "
        f"{_CRASHED_LONG_AGO_S}s ago still reads host_online: true — "
        "the heartbeat/TTL freshness check is not gating liveness, so a "
        "crashed host reads as 'online' forever."
    )


async def test_recently_seen_host_reads_host_online(
    host_aware_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A host seen within the TTL still reads ``host_online: true`` (anti-flap).

    The freshness gate must not be so tight that a healthy host whose
    heartbeat is merely a few seconds old gets aged out — that would
    flap live sessions' host_online between true and false. Here the
    host's last-seen is set comfortably inside the window (``status`` is
    still ``"online"`` and it was seen recently), and the session must
    still read ``host_online: true``.
    """
    conv_store = SqlAlchemyConversationStore(db_uri)
    host_store = HostStore(db_uri)

    agent = await create_test_agent(host_aware_client)
    session_id = agent["_session_id"]

    host_store.upsert_on_connect(
        host_id=_HOST_ID, name="alice-laptop", user_id=RESERVED_USER_LOCAL
    )
    conv_store.set_host_id(session_id, _HOST_ID, workspace="/tmp/ws")

    # Last seen comfortably inside the window (about a third of the TTL).
    _age_host_last_seen(db_uri, _HOST_ID, HOST_LIVENESS_TTL_S // 3)

    assert await _session_host_online(host_aware_client, session_id) is True, (
        "a host seen within the freshness window must stay host_online: true — "
        "the TTL gate is flapping a healthy host offline"
    )

"""Reconnect liveness stamp is durable before a runner's tunnel takes work.

A runner whose heartbeat expired reconnects to replica B while replica A
serves ``child_sessions``. If B's stamp is only queued in the background, A
can read the stale heartbeat and settle the running child to idle while the
runner is busy. The connect path awaits the stamp, so a settle that starts
after B accepts the tunnel sees the fresh heartbeat and leaves the row alone.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest

from omnigent.db.utils import generate_agent_id
from omnigent.server import session_live_state
from omnigent.server.routes._sessions.helpers import (
    _session_status_cache,
    reconcile_orphaned_running_status,
)
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)

_RUNNER_LIVENESS_GRACE_S = 90


def _seed_running_child(db_uri: str) -> tuple[str, str]:
    """Seed a session persisted as ``running`` whose runner heartbeat is stale.

    :param db_uri: SQLite database URI shared with the code under test.
    :returns: ``(session_id, runner_id)``.
    """
    agent_store = SqlAlchemyAgentStore(db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)
    agent_id = generate_agent_id()
    agent_store.create(
        agent_id,
        name=f"stamp-agent-{agent_id}",
        bundle_location="test:///bundle",
    )
    conv = conv_store.create_conversation(agent_id=agent_id)
    runner_id = f"runner_{conv.id}"
    assert conv_store.set_runner_id(conv.id, runner_id)
    conv_store.set_session_live_status(conv.id, "running")
    _session_status_cache.pop(conv.id, None)
    return conv.id, runner_id


def _live_status(db_uri: str, session_id: str) -> str | None:
    conv = SqlAlchemyConversationStore(db_uri).get_conversation(session_id)
    assert conv is not None
    return conv.live_status


def _settle_from_another_replica(db_uri: str, session_id: str) -> bool:
    """Run the child_sessions settle the way a replica without the tunnel does."""
    store = SqlAlchemyConversationStore(db_uri)
    return reconcile_orphaned_running_status(
        session_id, store, int(time.time()) - _RUNNER_LIVENESS_GRACE_S
    )


class _GatedStore:
    """Store proxy whose liveness stamp waits on *gate* before writing."""

    def __init__(self, inner: SqlAlchemyConversationStore, gate: threading.Event) -> None:
        self._inner = inner
        self._gate = gate

    def touch_runner_liveness(self, runner_ids: list[str], now: int) -> None:
        self._gate.wait(timeout=10)
        self._inner.touch_runner_liveness(runner_ids, now)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


@pytest.fixture()
def configured_store() -> Iterator[None]:
    """Unwire the live-state module on teardown."""
    yield
    session_live_state.configure(None)


async def test_connect_stamp_waits_for_the_write_before_returning(
    db_uri: str,
    configured_store: None,
) -> None:
    """The tunnel can't register until the stamp is durable."""
    session_id, runner_id = _seed_running_child(db_uri)
    gate = threading.Event()
    inner = SqlAlchemyConversationStore(db_uri)
    session_live_state.configure(_GatedStore(inner, gate))  # type: ignore[arg-type]

    stamp = asyncio.create_task(session_live_state.stamp_runner_liveness(runner_id))
    await asyncio.sleep(0.1)
    assert not stamp.done(), "connect must not proceed while the stamp is pending"

    gate.set()
    await asyncio.wait_for(stamp, timeout=10)
    # With the heartbeat durable, a settle from another replica leaves it running.
    assert not _settle_from_another_replica(db_uri, session_id)
    assert _live_status(db_uri, session_id) == "running"


async def test_stamp_failure_is_logged_and_does_not_block_connect(
    db_uri: str,
    configured_store: None,
) -> None:
    """A failed stamp is best-effort: the connection proceeds without raising."""
    _, runner_id = _seed_running_child(db_uri)

    class _FailingStore:
        def touch_runner_liveness(self, runner_ids: list[str], now: int) -> None:
            raise RuntimeError("database unavailable")

    session_live_state.configure(_FailingStore())  # type: ignore[arg-type]
    await session_live_state.stamp_runner_liveness(runner_id)
    assert session_live_state.last_liveness_stamp(runner_id) is not None

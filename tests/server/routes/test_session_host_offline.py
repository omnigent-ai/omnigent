"""Unit tests for ``_session_host_offline`` — when a failed send blames the host.

A native-terminal message that finds no runner is reported as an offline host
only when the session's user-run host has no live tunnel anywhere. Everything
else (a host that is connected, live on another replica, a managed sandbox the
server wakes itself, or a session with no host) keeps the generic failure.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

import pytest

from omnigent.entities.conversation import Conversation
from omnigent.server.routes.sessions.routes_events import _session_host_offline
from omnigent.stores.host_store import HOST_LIVENESS_TTL_S, Host

pytestmark = pytest.mark.asyncio

_HOST_ID = "c2d81b1a6812ae1cf32221c5a2a70ba0"


def _conv(host_id: str | None) -> Conversation:
    return Conversation(
        id="conv_1",
        created_at=1,
        updated_at=1,
        root_conversation_id="conv_1",
        agent_id="agent_1",
        host_id=host_id,
        workspace="/work/repo" if host_id else None,
    )


def _host(
    *, status: str = "offline", age_s: float = 0, sandbox_provider: str | None = None
) -> Host:
    now = int(time.time())
    return Host(
        host_id=_HOST_ID,
        name="laptop",
        user_id="alice@example.com",
        status=status,
        created_at=now - 1000,
        updated_at=now - int(age_s),
        sandbox_provider=sandbox_provider,
    )


class _Registry:
    def __init__(self, connected: bool) -> None:
        self._connected = connected

    def get(self, host_id: str) -> object | None:
        return object() if self._connected else None


class _Store:
    def __init__(self, host: Host | None) -> None:
        self._host = host
        self.reads = 0

    def get_host(self, host_id: str) -> Host | None:
        self.reads += 1
        return self._host


def _state(*, connected: bool, host: Host | None, with_store: bool = True) -> Any:
    return SimpleNamespace(
        host_registry=_Registry(connected),
        host_store=_Store(host) if with_store else None,
    )


async def test_host_missing_from_server_and_marked_offline_is_offline() -> None:
    """The plain case: no tunnel here, and the host row says nobody holds one."""
    assert await _session_host_offline(_conv(_HOST_ID), _state(connected=False, host=_host()))


async def test_stale_online_row_without_heartbeat_is_offline() -> None:
    """A row still marked online but past the liveness window is not a live host."""
    stale = _host(status="online", age_s=HOST_LIVENESS_TTL_S + 60)
    assert await _session_host_offline(_conv(_HOST_ID), _state(connected=False, host=stale))


async def test_without_a_host_store_a_missing_tunnel_is_offline() -> None:
    """Nothing else to consult: the absent tunnel alone decides."""
    state = _state(connected=False, host=None, with_store=False)
    assert await _session_host_offline(_conv(_HOST_ID), state)


async def test_connected_host_is_not_offline() -> None:
    """A tunnel on this server means the host can take a launch; no store read either."""
    state = _state(connected=True, host=_host())
    assert not await _session_host_offline(_conv(_HOST_ID), state)
    assert state.host_store.reads == 0


async def test_host_live_on_another_replica_is_not_offline() -> None:
    """A fresh online row with no local tunnel means a sibling replica holds it."""
    live = _host(status="online", age_s=1)
    assert not await _session_host_offline(_conv(_HOST_ID), _state(connected=False, host=live))


async def test_managed_sandbox_is_not_offline() -> None:
    """The server wakes managed sandboxes; telling the user to start one is wrong."""
    managed = _host(sandbox_provider="modal")
    assert not await _session_host_offline(_conv(_HOST_ID), _state(connected=False, host=managed))


async def test_unknown_host_row_is_not_offline() -> None:
    """A deleted host row gives nothing to tell the user to start."""
    assert not await _session_host_offline(_conv(_HOST_ID), _state(connected=False, host=None))


async def test_session_without_a_host_is_not_offline() -> None:
    """A hostless session has no host to blame."""
    assert not await _session_host_offline(_conv(None), _state(connected=False, host=_host()))


async def test_server_without_a_host_registry_cannot_say() -> None:
    """No registry wired means no evidence either way."""
    state = SimpleNamespace(host_registry=None, host_store=_Store(_host()))
    assert not await _session_host_offline(_conv(_HOST_ID), state)

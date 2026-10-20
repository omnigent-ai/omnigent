"""Decide when a dropped runner's host is offline, so its turn can wait for it."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from types import SimpleNamespace

import pytest

from omnigent.db.utils import now_epoch
from omnigent.server.routes._sessions import orchestration
from omnigent.server.routes._sessions.common import _RelayStatusSnapshot
from omnigent.stores.host_store import Host

_HOST_ID = "host_0123456789abcdef"


def _host(*, status: str = "offline", sandbox_provider: str | None = None) -> Host:
    now = now_epoch()
    return Host(
        host_id=_HOST_ID,
        name="test-laptop",
        user_id="user@example.com",
        status=status,
        created_at=now - 3600,
        updated_at=now,
        sandbox_provider=sandbox_provider,
    )


class _FakeHostStore:
    def __init__(self, host: Callable[[], Host] | None = None, *, error: Exception | None = None):
        # Build the row at read time so its last-seen is fresh when the test runs.
        self.host = host
        self.error = error

    def get_host(self, host_id: str) -> Host | None:
        if self.error is not None:
            raise self.error
        return self.host() if self.host is not None and host_id == _HOST_ID else None


class _FakeRegistry:
    def __init__(self, connected: bool) -> None:
        self.connected = connected

    def get(self, host_id: str) -> object | None:
        return object() if self.connected else None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("store", "connected", "expected"),
    [
        # A laptop that went to sleep: no tunnel here and its row reads offline.
        (_FakeHostStore(_host), False, True),
        # Its row went stale without a graceful disconnect.
        (_FakeHostStore(lambda: replace(_host(status="online"), updated_at=0)), False, True),
        # Connected to this replica, or live by its row on another one.
        (_FakeHostStore(_host), True, False),
        (_FakeHostStore(lambda: _host(status="online")), False, False),
        # A managed sandbox has its own wake path; an unknown host proves nothing.
        (_FakeHostStore(lambda: _host(sandbox_provider="e2b")), False, False),
        (_FakeHostStore(), False, False),
        # An unreadable row is inconclusive.
        (_FakeHostStore(error=RuntimeError("store down")), False, None),
        (None, False, False),
    ],
)
async def test_host_offline_state(
    monkeypatch: pytest.MonkeyPatch,
    store: _FakeHostStore | None,
    connected: bool,
    expected: bool | None,
) -> None:
    """Only a user-run host that is gone everywhere counts as offline."""
    monkeypatch.setattr(orchestration, "get_server_host_store", lambda: store)
    monkeypatch.setattr(
        orchestration, "get_server_host_registry", lambda: _FakeRegistry(connected)
    )
    assert await orchestration._host_offline_state(_HOST_ID) is expected


@pytest.mark.asyncio
async def test_host_offline_state_without_a_host_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """A session with no known host never waits for one."""
    monkeypatch.setattr(orchestration, "get_server_host_store", lambda: _FakeHostStore(_host))
    assert await orchestration._host_offline_state(None) is False


class _AncestorStore:
    """Serve sub-agent rows by id, like the conversation store's point read."""

    def __init__(self, *rows: SimpleNamespace) -> None:
        self.rows = {row.id: row for row in rows}

    def get_conversation(self, conversation_id: str) -> SimpleNamespace | None:
        return self.rows.get(conversation_id)


def _row(row_id: str, *, parent: str | None, host_id: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=row_id,
        kind="sub_agent" if parent is not None else "default",
        labels={},
        parent_conversation_id=parent,
        root_conversation_id="root" if parent is not None else None,
        runner_id="runner_shared",
        host_id=host_id,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("as_snapshot", [False, True])
async def test_session_host_id_follows_hostless_parents(as_snapshot: bool) -> None:
    """A grandchild under a hostless child still waits on the root's host."""
    root = _row("root", parent=None, host_id=_HOST_ID)
    child = _row("child", parent="root")
    grandchild = _row("grandchild", parent="child")
    store = _AncestorStore(root, child, grandchild)
    conv: object = grandchild
    if as_snapshot:
        conv = _RelayStatusSnapshot(
            live_status="running",
            kind="sub_agent",
            parent_conversation_id="child",
            runner_id="runner_shared",
            host_id=None,
            updated_at=0,
        )
    assert await orchestration._session_host_id(conv, store) == _HOST_ID  # type: ignore[arg-type]


class _SideChatStore(_AncestorStore):
    """A conversation store whose point read can fail."""

    def __init__(self, *rows: SimpleNamespace, fail: bool = False) -> None:
        super().__init__(*rows)
        self.fail = fail

    def get_conversation(self, conversation_id: str) -> SimpleNamespace | None:
        if self.fail:
            raise RuntimeError("store down")
        return super().get_conversation(conversation_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(("read_fails", "expected"), [(False, True), (True, False)])
async def test_relay_finds_an_offline_host_through_a_side_chats_source(
    monkeypatch: pytest.MonkeyPatch, read_fails: bool, expected: bool
) -> None:
    """A side chat's relay reads its stored row, since its saved snapshot can't name the source.

    A parentless, hostless side chat routes through the session it was forked
    from. Only the stored row carries that link, so with the row unreadable
    the snapshot alone leaves the host unknown and the turn is not held.
    """
    from omnigent.server.routes._sessions.common import _RelayHandle
    from omnigent.stores.conversation_store import SIDE_CHAT_LABEL_KEY, SIDE_CHAT_SOURCE_LABEL_KEY

    source = _row("source", parent=None, host_id=_HOST_ID)
    side_chat = SimpleNamespace(
        **{
            **vars(_row("side", parent=None)),
            "labels": {SIDE_CHAT_LABEL_KEY: "1", SIDE_CHAT_SOURCE_LABEL_KEY: "source"},
        }
    )
    store = _SideChatStore(source, side_chat, fail=read_fails)
    snapshot = _RelayStatusSnapshot(
        live_status="running",
        kind="default",
        parent_conversation_id=None,
        runner_id="runner_shared",
        host_id=None,
        updated_at=0,
    )
    handle = _RelayHandle(
        runner_id="runner_shared",
        task=SimpleNamespace(done=lambda: False),  # type: ignore[arg-type]
        ready=SimpleNamespace(),  # type: ignore[arg-type]
        status_snapshot=snapshot,
    )
    monkeypatch.setitem(orchestration._runner_relay_tasks, "side", handle)
    monkeypatch.setattr(orchestration, "get_server_host_store", lambda: _FakeHostStore(_host))
    monkeypatch.setattr(orchestration, "get_server_host_registry", lambda: _FakeRegistry(False))
    # The runner's tunnel dropped here, so its disconnect task owns the turn.
    monkeypatch.setattr(orchestration, "_runner_disconnect_owned", lambda runner_id: True)

    result = await orchestration._relay_host_offline("side", store, "runner_shared")  # type: ignore[arg-type]
    assert result is expected


_FURTHEST_DEADLINE = (
    1500.0 + orchestration.HOST_OFFLINE_HOLD_S + orchestration._HOST_OFFLINE_HOLD_SKEW_S
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2000", True),
        ("1000", False),
        ("1500", False),
        (str(_FURTHEST_DEADLINE), True),
        (str(_FURTHEST_DEADLINE + 1), False),
        ("not-a-time", False),
        (None, False),
    ],
)
def test_host_offline_hold_active_reads_an_unexpired_deadline(
    value: str | None, expected: bool
) -> None:
    """Only a deadline ahead, and no further out than one hold plus skew, marks a hold."""
    labels = {} if value is None else {orchestration.HOST_OFFLINE_HOLD_UNTIL_LABEL_KEY: value}
    assert orchestration.host_offline_hold_active(labels, now=1500.0) is expected

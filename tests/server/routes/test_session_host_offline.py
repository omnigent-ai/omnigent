"""Tests for the host-offline policy check and its disconnect-sweep partition.

A session must not be marked ``failed`` just because its runner's HOST went
offline (laptop asleep, machine off the network) — only a confirmed runner
death should fail it immediately. These tests cover the policy helper
(:func:`_session_host_offline`), the sweep partition it drives
(:func:`_partition_disconnect_targets`), and the relay's own mid-turn-fail
branch, all in isolation from the full tunnel stack;
``tests/server/integration/test_sessions_tunnel_three_layer.py`` covers the
end-to-end wiring (scheduling the disconnect-sweep hold, cancelling it on
reconnect, the hold's expiry).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx
import pytest

from omnigent.entities import Conversation
from omnigent.server import shutdown_state
from omnigent.server.routes import sessions as sessions_module
from omnigent.server.routes._sessions import common as common_module
from omnigent.server.routes._sessions import orchestration as orch
from omnigent.stores.host_store import Host, now_epoch


@dataclass
class _FakeHostStore:
    hosts: dict[str, Host] = field(default_factory=dict)

    def get_host(self, host_id: str) -> Host | None:
        return self.hosts.get(host_id)


@dataclass
class _FakeConversationStore:
    convs: dict[str, Conversation] = field(default_factory=dict)

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        return self.convs.get(conversation_id)


def _conv(
    id: str = "conv_1",
    *,
    host_id: str | None = None,
    kind: str = "default",
    parent_conversation_id: str | None = None,
    live_status: str | None = None,
) -> Conversation:
    return Conversation(
        id=id,
        created_at=1,
        updated_at=1,
        root_conversation_id=id,
        host_id=host_id,
        kind=kind,
        parent_conversation_id=parent_conversation_id,
        live_status=live_status,
    )


def _host(
    host_id: str = "host_1",
    *,
    status: str = "online",
    updated_at: int | None = None,
    sandbox_provider: str | None = None,
) -> Host:
    return Host(
        host_id=host_id,
        name="test-host",
        user_id="alice",
        status=status,
        created_at=1,
        updated_at=updated_at if updated_at is not None else now_epoch(),
        sandbox_provider=sandbox_provider,
    )


@pytest.fixture(autouse=True)
def _clean_state() -> None:
    """Keep the module-level caches from leaking between tests."""
    orch._session_status_cache.clear()
    orch._intentional_stop_sessions.clear()
    yield
    orch._session_status_cache.clear()
    orch._intentional_stop_sessions.clear()


# ── _session_host_offline ───────────────────────────────────────────────


class TestSessionHostOffline:
    @pytest.mark.asyncio
    async def test_no_host_store_reads_not_offline(self) -> None:
        """No ``host_store`` wired ⇒ conservative "unknown" (fail as today)."""
        conv = _conv(host_id="host_1")
        assert not await orch._session_host_offline(conv, None, _FakeConversationStore())

    @pytest.mark.asyncio
    async def test_no_host_id_reads_not_offline(self) -> None:
        """A CLI session with no host binding is never held."""
        conv = _conv(host_id=None)
        store = _FakeHostStore()
        assert not await orch._session_host_offline(conv, store, _FakeConversationStore())

    @pytest.mark.asyncio
    async def test_live_host_reads_not_offline(self) -> None:
        """A live host ⇒ the runner disconnect is treated as before."""
        conv = _conv(host_id="host_1")
        store = _FakeHostStore(hosts={"host_1": _host(status="online")})
        assert not await orch._session_host_offline(conv, store, _FakeConversationStore())

    @pytest.mark.asyncio
    async def test_offline_external_host_reads_offline(self) -> None:
        """An external host that dropped its tunnel reads as host-offline."""
        conv = _conv(host_id="host_1")
        store = _FakeHostStore(hosts={"host_1": _host(status="offline")})
        assert await orch._session_host_offline(conv, store, _FakeConversationStore())

    @pytest.mark.asyncio
    async def test_stale_online_host_reads_offline(self) -> None:
        """A host whose heartbeat went stale past the liveness TTL is offline."""
        conv = _conv(host_id="host_1")
        store = _FakeHostStore(
            hosts={"host_1": _host(status="online", updated_at=now_epoch() - 10_000)}
        )
        assert await orch._session_host_offline(conv, store, _FakeConversationStore())

    @pytest.mark.asyncio
    async def test_managed_sandbox_host_never_reads_offline(self) -> None:
        """A managed sandbox has its own wake path — excluded from the hold."""
        conv = _conv(host_id="host_1")
        store = _FakeHostStore(hosts={"host_1": _host(status="offline", sandbox_provider="modal")})
        assert not await orch._session_host_offline(conv, store, _FakeConversationStore())

    @pytest.mark.asyncio
    async def test_unknown_host_reads_not_offline(self) -> None:
        """A host_id with no matching row is "unknown", not "offline"."""
        conv = _conv(host_id="host_missing")
        store = _FakeHostStore()
        assert not await orch._session_host_offline(conv, store, _FakeConversationStore())

    @pytest.mark.asyncio
    async def test_host_lookup_error_reads_not_offline(self) -> None:
        """A store error must not be misread as a confirmed host outage."""

        class _RaisingHostStore:
            def get_host(self, host_id: str) -> Host | None:
                raise RuntimeError("boom")

        conv = _conv(host_id="host_1")
        assert not await orch._session_host_offline(
            conv,
            _RaisingHostStore(),
            _FakeConversationStore(),  # type: ignore[arg-type]
        )

    @pytest.mark.asyncio
    async def test_subagent_resolves_parent_host(self) -> None:
        """A sub-agent has no ``host_id`` of its own — its parent's host is checked."""
        parent = _conv(id="conv_parent", host_id="host_1")
        child = _conv(
            id="conv_child", host_id=None, kind="sub_agent", parent_conversation_id="conv_parent"
        )
        store = _FakeHostStore(hosts={"host_1": _host(status="offline")})
        conv_store = _FakeConversationStore(convs={"conv_parent": parent})
        assert await orch._session_host_offline(child, store, conv_store)

    @pytest.mark.asyncio
    async def test_subagent_with_missing_parent_reads_not_offline(self) -> None:
        """An unresolvable parent leaves the sub-agent's host "unknown"."""
        child = _conv(
            id="conv_child", host_id=None, kind="sub_agent", parent_conversation_id="conv_gone"
        )
        store = _FakeHostStore(hosts={"host_1": _host(status="offline")})
        conv_store = _FakeConversationStore()
        assert not await orch._session_host_offline(child, store, conv_store)


# ── _partition_disconnect_targets ───────────────────────────────────────


class TestPartitionDisconnectTargets:
    @pytest.mark.asyncio
    async def test_idle_session_goes_to_fail_now_without_a_host_check(self) -> None:
        """An idle session was never going to fail — skip it, never hold it."""
        conv = _conv(host_id="host_1", live_status="idle")
        store = _FakeHostStore(hosts={"host_1": _host(status="offline")})
        fail_now, hold = await orch._partition_disconnect_targets(
            [conv], _FakeConversationStore(), store
        )
        assert fail_now == [conv]
        assert hold == []

    @pytest.mark.asyncio
    async def test_mid_turn_host_live_goes_to_fail_now(self) -> None:
        """A mid-turn session on a live host fails exactly as before."""
        conv = _conv(host_id="host_1", live_status="running")
        store = _FakeHostStore(hosts={"host_1": _host(status="online")})
        fail_now, hold = await orch._partition_disconnect_targets(
            [conv], _FakeConversationStore(), store
        )
        assert fail_now == [conv]
        assert hold == []

    @pytest.mark.asyncio
    async def test_mid_turn_host_offline_is_held(self) -> None:
        """A mid-turn session whose host is offline is held, not failed."""
        conv = _conv(host_id="host_1", live_status="running")
        store = _FakeHostStore(hosts={"host_1": _host(status="offline")})
        fail_now, hold = await orch._partition_disconnect_targets(
            [conv], _FakeConversationStore(), store
        )
        assert fail_now == []
        assert hold == [conv]

    @pytest.mark.asyncio
    async def test_waiting_status_counts_as_mid_turn(self) -> None:
        """``waiting`` (background work outlives the turn) is held too."""
        conv = _conv(host_id="host_1", live_status="waiting")
        store = _FakeHostStore(hosts={"host_1": _host(status="offline")})
        fail_now, hold = await orch._partition_disconnect_targets(
            [conv], _FakeConversationStore(), store
        )
        assert hold == [conv]
        assert fail_now == []

    @pytest.mark.asyncio
    async def test_managed_sandbox_offline_goes_to_fail_now(self) -> None:
        """A managed sandbox is excluded from the hold, so it fails as before."""
        conv = _conv(host_id="host_1", live_status="running")
        store = _FakeHostStore(hosts={"host_1": _host(status="offline", sandbox_provider="modal")})
        fail_now, hold = await orch._partition_disconnect_targets(
            [conv], _FakeConversationStore(), store
        )
        assert fail_now == [conv]
        assert hold == []

    @pytest.mark.asyncio
    async def test_subagent_child_of_offline_host_parent_is_held(self) -> None:
        """A sub-agent inherits its parent's host-offline verdict."""
        parent = _conv(id="conv_parent", host_id="host_1")
        child = _conv(
            id="conv_child",
            host_id=None,
            kind="sub_agent",
            parent_conversation_id="conv_parent",
            live_status="running",
        )
        store = _FakeHostStore(hosts={"host_1": _host(status="offline")})
        conv_store = _FakeConversationStore(convs={"conv_parent": parent})
        fail_now, hold = await orch._partition_disconnect_targets([child], conv_store, store)
        assert fail_now == []
        assert hold == [child]

    @pytest.mark.asyncio
    async def test_intentional_stop_bypasses_the_host_check(self) -> None:
        """A Stop-marked session is left for the sweep's own skip, never held."""
        conv = _conv(host_id="host_1", live_status="running")
        orch._intentional_stop_sessions.add(conv.id)
        store = _FakeHostStore(hosts={"host_1": _host(status="offline")})
        fail_now, hold = await orch._partition_disconnect_targets(
            [conv], _FakeConversationStore(), store
        )
        assert fail_now == [conv]
        assert hold == []

    @pytest.mark.asyncio
    async def test_cached_status_wins_over_the_row(self) -> None:
        """The live relay-fed cache is authoritative over a stale row value."""
        conv = _conv(host_id="host_1", live_status="idle")
        orch._session_status_cache[conv.id] = "running"
        store = _FakeHostStore(hosts={"host_1": _host(status="offline")})
        fail_now, hold = await orch._partition_disconnect_targets(
            [conv], _FakeConversationStore(), store
        )
        assert fail_now == []
        assert hold == [conv]


# ── _relay_runner_stream: mid-turn drop while the host is offline ────────


class TestRelayHostOfflineDrop:
    @pytest.fixture(autouse=True)
    def _reset_shutdown_state(self) -> None:
        shutdown_state.reset_for_tests()
        yield
        shutdown_state.reset_for_tests()

    @pytest.mark.asyncio
    async def test_relay_defers_to_the_hold_when_host_offline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A relay-side drop must not fail a session whose host is offline.

        Path 1 (the disconnect-grace sweep on this same runner/replica)
        always fires alongside this drop and owns the bounded hold, so the
        relay stays quiet rather than publish a competing
        ``runner_disconnected`` failure.
        """
        monkeypatch.setattr(orch, "RUNNER_DISCONNECT_GRACE_S", 0.05)

        session_id = "conv_relay_host_offline"
        conv = _conv(id=session_id, host_id="host_1", live_status="running")
        conv_store = _FakeConversationStore(convs={session_id: conv})
        host_store = _FakeHostStore(hosts={"host_1": _host(status="offline")})
        monkeypatch.setattr(common_module, "_server_host_store", host_store)
        sessions_module._session_status_cache[session_id] = "running"

        def _raise(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom")

        client = httpx.AsyncClient(transport=httpx.MockTransport(_raise), base_url="http://runner")
        try:
            await orch._relay_runner_stream(session_id, client, conv_store)
            assert sessions_module._session_status_cache.get(session_id) == "running", (
                "the relay failed a host-offline session instead of deferring to the hold"
            )
        finally:
            await client.aclose()
            sessions_module._session_status_cache.pop(session_id, None)

    @pytest.mark.asyncio
    async def test_relay_still_fails_mid_turn_drop_when_host_online(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A live host leaves the relay's ordinary mid-turn failure unchanged."""
        monkeypatch.setattr(orch, "RUNNER_DISCONNECT_GRACE_S", 0.05)

        session_id = "conv_relay_host_online"
        conv = _conv(id=session_id, host_id="host_1", live_status="running")
        conv_store = _FakeConversationStore(convs={session_id: conv})
        host_store = _FakeHostStore(hosts={"host_1": _host(status="online")})
        monkeypatch.setattr(common_module, "_server_host_store", host_store)
        sessions_module._session_status_cache[session_id] = "running"

        def _raise(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom")

        client = httpx.AsyncClient(transport=httpx.MockTransport(_raise), base_url="http://runner")
        try:
            await orch._relay_runner_stream(session_id, client, conv_store)
            assert sessions_module._session_status_cache.get(session_id) == "failed"
        finally:
            await client.aclose()
            sessions_module._session_status_cache.pop(session_id, None)

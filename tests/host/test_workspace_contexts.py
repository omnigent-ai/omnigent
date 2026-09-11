"""Real-process coverage for host-owned pre-chat workspace contexts."""

from __future__ import annotations

import asyncio
import base64
import os
import re
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from omnigent.host.connect import HostProcess
from omnigent.host.frames import (
    HostWorkspaceContextRequestFrame,
    HostWorkspaceContextResultFrame,
    HostWorkspaceContextStreamFrame,
    decode_host_frame,
)
from omnigent.host.workspace_contexts import LEASE_SECONDS, WorkspaceContextManager

_HAS_TMUX = shutil.which("tmux") is not None


@dataclass
class _StreamSink:
    """Collect the multiplexed frames emitted by one terminal attachment."""

    frames: list[HostWorkspaceContextStreamFrame] = field(default_factory=list)

    async def send(self, frame: HostWorkspaceContextStreamFrame) -> None:
        self.frames.append(frame)

    def binary(self) -> bytes:
        return b"".join(
            base64.b64decode(frame.data)
            for frame in self.frames
            if frame.binary and frame.close_code is None
        )


class _HostTunnel:
    """Minimal host tunnel used to exercise ``HostProcess`` dispatch."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, data: str) -> None:
        self.sent.append(data)


async def _discard_stream(_frame: HostWorkspaceContextStreamFrame) -> None:
    return None


async def _request(
    manager: WorkspaceContextManager,
    op: str,
    *,
    context_id: str = "",
    user_id: str = "alice",
    params: dict[str, Any] | None = None,
    send: Callable[[HostWorkspaceContextStreamFrame], Awaitable[None]] = _discard_stream,
    tunnel: object | None = None,
) -> HostWorkspaceContextResultFrame:
    """Issue one request using the same frame boundary as the host tunnel."""

    return await manager.handle(
        HostWorkspaceContextRequestFrame(
            request_id=f"req-{op}",
            op=op,
            user_id=user_id,
            context_id=context_id,
            params=params or {},
        ),
        send=send,
        tunnel=tunnel if tunnel is not None else object(),
    )


def _ok(result: HostWorkspaceContextResultFrame) -> dict[str, Any]:
    assert result.status == "ok", result
    assert result.payload is not None
    return result.payload


async def _wait_for_stream(sink: _StreamSink, needle: bytes, timeout: float = 8.0) -> bytes:
    """Wait until an attachment has emitted a specific byte sequence."""

    async def observed() -> bytes:
        while needle not in (data := sink.binary()):
            await asyncio.sleep(0.05)
        return data

    return await asyncio.wait_for(observed(), timeout=timeout)


async def _wait_for_process_exit(pid: int, timeout: float = 5.0) -> None:
    """Wait until a tmux pane PID is gone."""

    async def exited() -> None:
        while True:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            await asyncio.sleep(0.05)

    await asyncio.wait_for(exited(), timeout=timeout)


async def test_host_frame_dispatch_creates_canonical_context(tmp_path: Path) -> None:
    """Host dispatch returns the manager result over the encoded tunnel frame."""

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    alias = tmp_path / "workspace-link"
    alias.symlink_to(workspace, target_is_directory=True)
    manager = WorkspaceContextManager()
    process = object.__new__(HostProcess)
    process._workspace_contexts = manager
    tunnel = _HostTunnel()
    try:
        await process._dispatch_host_frame(  # pyright: ignore[reportPrivateUsage]
            tunnel,  # type: ignore[arg-type]
            HostWorkspaceContextRequestFrame(
                request_id="req-create",
                op="create",
                user_id="alice",
                params={"workspace": str(alias)},
            ),
        )
        assert len(tunnel.sent) == 1
        result = decode_host_frame(tunnel.sent[0])
        assert isinstance(result, HostWorkspaceContextResultFrame)
        payload = _ok(result)
        assert payload["workspace"] == str(workspace.resolve())
        assert payload["session_id"] is None
        assert payload["lease_seconds"] == LEASE_SECONDS
    finally:
        await manager.shutdown()


async def test_owner_handoff_and_validation(tmp_path: Path) -> None:
    """Contexts stay owner-scoped and bind once to an exact canonical workspace."""

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    alias = tmp_path / "workspace-link"
    alias.symlink_to(workspace, target_is_directory=True)
    other = tmp_path / "other"
    other.mkdir()
    manager = WorkspaceContextManager()
    try:
        missing_owner = await _request(
            manager, "create", user_id="", params={"workspace": str(workspace)}
        )
        assert (missing_owner.status, missing_owner.error_status) == ("error", 403)
        relative = await _request(manager, "create", params={"workspace": "relative"})
        assert (relative.status, relative.error_status) == ("error", 400)

        context = _ok(await _request(manager, "create", params={"workspace": str(alias)}))
        context_id = str(context["id"])
        foreign = await _request(manager, "describe", context_id=context_id, user_id="bob")
        assert (foreign.status, foreign.error_status) == ("error", 404)

        mismatch = await _request(
            manager,
            "handoff",
            context_id=context_id,
            params={"session_id": "session-one", "workspace": str(other)},
        )
        assert (mismatch.status, mismatch.error_status) == ("error", 409)

        adopted = _ok(
            await _request(
                manager,
                "handoff",
                context_id=context_id,
                params={"session_id": "session-one", "workspace": str(workspace)},
            )
        )
        assert adopted["session_id"] == "session-one"
        assert adopted["context_deleted"] is True
        rebound = await _request(
            manager,
            "handoff",
            context_id=context_id,
            params={"session_id": "session-two", "workspace": str(alias)},
        )
        assert (rebound.status, rebound.error_status) == ("error", 404)
    finally:
        await manager.shutdown()


@pytest.mark.skipif(not _HAS_TMUX, reason="tmux not installed")
async def test_close_before_bridge_starts_releases_channel_and_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An immediate client close cannot strand a channel before bridge startup."""

    now = [0.0]
    bridge_started = asyncio.Event()

    async def blocked_bridge(*_args: object, **_kwargs: object) -> None:
        bridge_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(
        "omnigent.host.workspace_contexts.bridge_tmux_control_to_websocket",
        blocked_bridge,
    )
    manager = WorkspaceContextManager(clock=lambda: now[0])
    tunnel = object()
    try:
        context_id = str(
            _ok(await _request(manager, "create", params={"workspace": str(tmp_path)}))["id"]
        )
        terminal = _ok(
            await _request(
                manager,
                "create_terminal",
                context_id=context_id,
                params={"terminal": "bash", "session_key": "one"},
            )
        )
        attached = await _request(
            manager,
            "attach",
            context_id=context_id,
            params={"terminal_id": terminal["id"], "channel_id": "closing-channel"},
            tunnel=tunnel,
        )
        assert _ok(attached) == {"channel_id": "closing-channel"}

        # Stay in this event-loop turn so the task is cancelled before its
        # coroutine body, including its finally block, gets a chance to run.
        manager.receive(
            HostWorkspaceContextStreamFrame(channel_id="closing-channel", close_code=1000),
            tunnel=tunnel,
        )
        for _ in range(10):
            await asyncio.sleep(0)
            if not manager._channels:  # pyright: ignore[reportPrivateUsage]
                break
        assert not bridge_started.is_set()
        assert manager._channels == {}  # pyright: ignore[reportPrivateUsage]
        assert not manager._contexts[context_id].channels  # pyright: ignore[reportPrivateUsage]

        now[0] = LEASE_SECONDS
        await manager.reap_expired()
        expired = await _request(manager, "describe", context_id=context_id)
        assert (expired.status, expired.error_status) == ("error", 404)
    finally:
        await manager.shutdown()


@pytest.mark.skipif(not _HAS_TMUX, reason="tmux not installed")
async def test_disconnect_during_attach_liveness_check_refuses_channel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tunnel lost during the liveness await cannot publish an attachment."""

    active_tunnel: list[object | None] = []
    manager = WorkspaceContextManager(
        tunnel_is_live=lambda tunnel: bool(active_tunnel) and active_tunnel[0] is tunnel
    )
    tunnel = object()
    active_tunnel.append(tunnel)
    liveness_started = asyncio.Event()
    release_liveness = asyncio.Event()
    try:
        context_id = str(
            _ok(await _request(manager, "create", params={"workspace": str(tmp_path)}))["id"]
        )
        terminal = _ok(
            await _request(
                manager,
                "create_terminal",
                context_id=context_id,
                params={"terminal": "bash", "session_key": "one"},
            )
        )
        instance = manager.registry.get(context_id, "bash", "one")
        assert instance is not None

        async def delayed_alive() -> bool:
            liveness_started.set()
            await release_liveness.wait()
            return True

        monkeypatch.setattr(instance, "is_alive", delayed_alive)
        attach_task = asyncio.create_task(
            _request(
                manager,
                "attach",
                context_id=context_id,
                params={"terminal_id": terminal["id"], "channel_id": "racing-channel"},
                tunnel=tunnel,
            )
        )
        await asyncio.wait_for(liveness_started.wait(), timeout=2)

        active_tunnel[0] = None
        await manager.disconnect(tunnel)
        release_liveness.set()
        result = await asyncio.wait_for(attach_task, timeout=2)
        assert (result.status, result.error_status) == ("error", 503)
        assert manager._channels == {}  # pyright: ignore[reportPrivateUsage]
        assert not manager._contexts[context_id].channels  # pyright: ignore[reportPrivateUsage]
    finally:
        release_liveness.set()
        await manager.shutdown()


@pytest.mark.skipif(not _HAS_TMUX, reason="tmux not installed")
async def test_close_during_attach_liveness_check_refuses_channel_and_allows_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A close received during liveness probing cannot strand an attachment."""

    now = [0.0]
    manager = WorkspaceContextManager(clock=lambda: now[0])
    tunnel = object()
    liveness_started = asyncio.Event()
    release_liveness = asyncio.Event()
    try:
        context_id = str(
            _ok(await _request(manager, "create", params={"workspace": str(tmp_path)}))["id"]
        )
        terminal = _ok(
            await _request(
                manager,
                "create_terminal",
                context_id=context_id,
                params={"terminal": "bash", "session_key": "one"},
            )
        )
        instance = manager.registry.get(context_id, "bash", "one")
        assert instance is not None

        async def delayed_alive() -> bool:
            liveness_started.set()
            await release_liveness.wait()
            return True

        monkeypatch.setattr(instance, "is_alive", delayed_alive)
        attach_task = asyncio.create_task(
            _request(
                manager,
                "attach",
                context_id=context_id,
                params={"terminal_id": terminal["id"], "channel_id": "timed-out-channel"},
                tunnel=tunnel,
            )
        )
        await asyncio.wait_for(liveness_started.wait(), timeout=2)

        manager.receive(
            HostWorkspaceContextStreamFrame(channel_id="timed-out-channel", close_code=1000),
            tunnel=object(),
        )
        assert not manager._pending_channels[  # pyright: ignore[reportPrivateUsage]
            "timed-out-channel"
        ].cancelled
        manager.receive(
            HostWorkspaceContextStreamFrame(channel_id="timed-out-channel", close_code=1000),
            tunnel=tunnel,
        )
        release_liveness.set()
        result = await asyncio.wait_for(attach_task, timeout=2)
        assert (result.status, result.error_status) == ("error", 499)
        assert manager._channels == {}  # pyright: ignore[reportPrivateUsage]
        assert manager._pending_channels == {}  # pyright: ignore[reportPrivateUsage]
        assert not manager._contexts[context_id].channels  # pyright: ignore[reportPrivateUsage]

        now[0] = LEASE_SECONDS
        await manager.reap_expired()
        expired = await _request(manager, "describe", context_id=context_id)
        assert (expired.status, expired.error_status) == ("error", 404)
    finally:
        release_liveness.set()
        await manager.shutdown()


@pytest.mark.skipif(not _HAS_TMUX, reason="tmux not installed")
async def test_close_while_attach_waits_for_context_lock_refuses_channel(
    tmp_path: Path,
) -> None:
    """A close received while attachment is queued cannot be lost."""

    manager = WorkspaceContextManager()
    tunnel = object()
    context_lock: asyncio.Lock | None = None
    try:
        context_id = str(
            _ok(await _request(manager, "create", params={"workspace": str(tmp_path)}))["id"]
        )
        terminal = _ok(
            await _request(
                manager,
                "create_terminal",
                context_id=context_id,
                params={"terminal": "bash", "session_key": "one"},
            )
        )
        context_lock = manager._contexts[context_id].lock  # pyright: ignore[reportPrivateUsage]
        await context_lock.acquire()
        attach_task = asyncio.create_task(
            _request(
                manager,
                "attach",
                context_id=context_id,
                params={"terminal_id": terminal["id"], "channel_id": "queued-channel"},
                tunnel=tunnel,
            )
        )
        for _ in range(10):
            await asyncio.sleep(0)
            if "queued-channel" in manager._pending_channels:  # pyright: ignore[reportPrivateUsage]
                break
        assert "queued-channel" in manager._pending_channels  # pyright: ignore[reportPrivateUsage]

        manager.receive(
            HostWorkspaceContextStreamFrame(channel_id="queued-channel", close_code=1000),
            tunnel=tunnel,
        )
        context_lock.release()
        result = await asyncio.wait_for(attach_task, timeout=2)
        assert (result.status, result.error_status) == ("error", 499)
        assert manager._channels == {}  # pyright: ignore[reportPrivateUsage]
        assert manager._pending_channels == {}  # pyright: ignore[reportPrivateUsage]
        assert not manager._contexts[context_id].channels  # pyright: ignore[reportPrivateUsage]
    finally:
        if context_lock is not None and context_lock.locked():
            context_lock.release()
        await manager.shutdown()


@pytest.mark.skipif(not _HAS_TMUX, reason="tmux not installed")
async def test_terminal_survives_reconnect_and_obeys_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real shell survives reload, retains output, and stays leased while attached."""

    now = [0.0]
    workspace = tmp_path.resolve()
    manager = WorkspaceContextManager(clock=lambda: now[0])
    first_tunnel = object()
    first_stream = _StreamSink()
    second_tunnel = object()
    second_stream = _StreamSink()
    try:
        context = _ok(await _request(manager, "create", params={"workspace": str(workspace)}))
        context_id = str(context["id"])
        terminal = _ok(
            await _request(
                manager,
                "create_terminal",
                context_id=context_id,
                params={"terminal": "bash", "session_key": "one"},
            )
        )
        assert terminal["object"] == "workspace.resource"
        assert terminal["workspace_context_id"] == context_id
        assert terminal["session_id"] is None
        assert "environment" not in terminal
        terminal_id = str(terminal["id"])
        [entry] = manager.registry.list_for_conversation(context_id)
        pane_pid = entry.instance.pane_pid_sync()
        assert pane_pid is not None

        _ok(
            await _request(
                manager,
                "attach",
                context_id=context_id,
                params={"terminal_id": terminal_id, "channel_id": "channel-one"},
                send=first_stream.send,
                tunnel=first_tunnel,
            )
        )
        duplicate_channel = await _request(
            manager,
            "attach",
            context_id=context_id,
            params={"terminal_id": terminal_id, "channel_id": "channel-one"},
        )
        assert (duplicate_channel.status, duplicate_channel.error_status) == ("error", 400)
        monkeypatch.setattr("omnigent.host.workspace_contexts._MAX_CHANNELS_PER_CONTEXT", 1)
        channel_limited = await _request(
            manager,
            "attach",
            context_id=context_id,
            params={"terminal_id": terminal_id, "channel_id": "channel-overflow"},
        )
        assert (channel_limited.status, channel_limited.error_status) == ("error", 429)
        command = (
            b'printf \'CTX_PID=%s\\nCTX_CWD=%s\\n\' "$$" "$PWD"; '
            b"printf persisted > reconnect-marker.txt; printf 'CTX_DONE\\n'\r"
        )
        manager.receive(
            HostWorkspaceContextStreamFrame(
                channel_id="channel-one",
                data=base64.b64encode(command).decode("ascii"),
                binary=True,
            ),
            tunnel=first_tunnel,
        )
        first_output = await _wait_for_stream(first_stream, b"CTX_DONE")
        match = re.search(rb"CTX_PID=(\d+)", first_output)
        assert match is not None
        assert int(match.group(1)) == pane_pid
        assert f"CTX_CWD={workspace}".encode() in first_output
        assert (workspace / "reconnect-marker.txt").read_text() == "persisted"

        await manager.disconnect(first_tunnel)
        assert await entry.instance.is_alive()
        assert entry.instance.pane_pid_sync() == pane_pid
        listed = _ok(await _request(manager, "list_terminals", context_id=context_id))
        assert [resource["id"] for resource in listed["data"]] == [terminal_id]

        _ok(
            await _request(
                manager,
                "attach",
                context_id=context_id,
                params={"terminal_id": terminal_id, "channel_id": "channel-two"},
                send=second_stream.send,
                tunnel=second_tunnel,
            )
        )
        reloaded_output = await _wait_for_stream(second_stream, b"CTX_DONE")
        assert f"CTX_CWD={workspace}".encode() in reloaded_output
        assert entry.instance.pane_pid_sync() == pane_pid

        # No further input is sent. The open attachment alone prevents expiry.
        now[0] = LEASE_SECONDS + 1
        await manager.reap_expired()
        assert _ok(await _request(manager, "describe", context_id=context_id))["id"] == context_id
        assert await entry.instance.is_alive()

        await manager.disconnect(second_tunnel)
        now[0] += LEASE_SECONDS - 1
        assert _ok(await _request(manager, "heartbeat", context_id=context_id))["id"] == context_id
        now[0] += LEASE_SECONDS - 1
        await manager.reap_expired()
        assert _ok(await _request(manager, "describe", context_id=context_id))["id"] == context_id
        now[0] += LEASE_SECONDS
        await manager.reap_expired()
        expired = await _request(manager, "describe", context_id=context_id)
        assert (expired.status, expired.error_status) == ("error", 404)
        await _wait_for_process_exit(pane_pid)
    finally:
        await manager.shutdown()


@pytest.mark.skipif(not _HAS_TMUX, reason="tmux not installed")
async def test_multiple_viewers_preserve_lazy_shell_across_handoff(tmp_path: Path) -> None:
    """Concurrent creation, handoff, and one viewer leaving preserve the shared shell."""
    now = [0.0]
    manager = WorkspaceContextManager(clock=lambda: now[0])
    tunnels = [object(), object()]
    streams = [_StreamSink(), _StreamSink()]
    try:
        context_id = str(
            _ok(await _request(manager, "create", params={"workspace": str(tmp_path)}))["id"]
        )
        assert _ok(await _request(manager, "list_terminals", context_id=context_id))["data"] == []
        created = await asyncio.gather(
            *(
                _request(
                    manager,
                    "create_terminal",
                    context_id=context_id,
                    params={"session_key": "shared"},
                )
                for _ in range(2)
            )
        )
        terminal_id = _ok(created[0])["id"]
        assert _ok(created[1])["id"] == terminal_id
        [entry] = manager.registry.list_for_conversation(context_id)
        pane_pid = entry.instance.pane_pid_sync()
        assert pane_pid is not None
        for index in range(2):
            _ok(
                await _request(
                    manager,
                    "attach",
                    context_id=context_id,
                    params={"terminal_id": terminal_id, "channel_id": f"viewer-{index}"},
                    tunnel=tunnels[index],
                    send=streams[index].send,
                )
            )

        async def both_attached() -> None:
            while True:
                clients = await entry.instance._tmux_output("list-clients", "-F", "#{client_name}")
                if len(clients.splitlines()) == 2:
                    return
                await asyncio.sleep(0.01)

        await asyncio.wait_for(both_attached(), timeout=5)
        manager.receive(
            HostWorkspaceContextStreamFrame(
                channel_id="viewer-0",
                data=base64.b64encode(b"printf 'SHARED_VIEW_READY\\n'\r").decode("ascii"),
                binary=True,
            ),
            tunnel=tunnels[0],
        )
        await asyncio.gather(
            *(_wait_for_stream(stream, b"SHARED_VIEW_READY") for stream in streams)
        )
        _ok(
            await _request(
                manager,
                "handoff",
                context_id=context_id,
                params={"session_id": "session-shared", "workspace": str(tmp_path)},
            )
        )
        [resource] = _ok(await _request(manager, "list_terminals", context_id=context_id))["data"]
        assert resource["id"] == terminal_id
        assert resource["session_id"] == "session-shared"
        await manager.disconnect(tunnels[0])
        now[0] = LEASE_SECONDS + 1
        await manager.reap_expired()
        assert entry.instance.pane_pid_sync() == pane_pid
        assert await entry.instance.is_alive()
        assert not any(frame.close_code is not None for frame in streams[1].frames)
        await manager.disconnect(tunnels[1])
        now[0] += LEASE_SECONDS
        await manager.reap_expired()
        assert (await _request(manager, "describe", context_id=context_id)).error_status == 404
        await _wait_for_process_exit(pane_pid)
    finally:
        await manager.shutdown()


@pytest.mark.skipif(not _HAS_TMUX, reason="tmux not installed")
async def test_explicit_terminal_and_context_deletion_kill_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleting a terminal or discarding its context terminates the owned shell."""

    manager = WorkspaceContextManager()
    try:
        context_id = str(
            _ok(await _request(manager, "create", params={"workspace": str(tmp_path)}))["id"]
        )
        terminals: list[tuple[str, int]] = []
        for key in ("one", "two"):
            payload = _ok(
                await _request(
                    manager,
                    "create_terminal",
                    context_id=context_id,
                    params={"terminal": "bash", "session_key": key},
                )
            )
            entry = manager.registry.get(context_id, "bash", key)
            assert entry is not None
            pid = entry.pane_pid_sync()
            assert pid is not None
            terminals.append((str(payload["id"]), pid))

        monkeypatch.setattr("omnigent.host.workspace_contexts._MAX_TERMINALS_PER_CONTEXT", 2)
        terminal_limited = await _request(
            manager,
            "create_terminal",
            context_id=context_id,
            params={"terminal": "bash", "session_key": "three"},
        )
        assert (terminal_limited.status, terminal_limited.error_status) == ("error", 429)

        deleted = _ok(
            await _request(
                manager,
                "delete_terminal",
                context_id=context_id,
                params={"terminal_id": terminals[0][0]},
            )
        )
        assert deleted == {"id": terminals[0][0], "deleted": True}
        await _wait_for_process_exit(terminals[0][1])
        assert manager.registry.get(context_id, "bash", "one") is None
        assert manager.registry.get(context_id, "bash", "two") is not None

        discarded = _ok(await _request(manager, "delete", context_id=context_id))
        assert discarded == {"id": context_id, "deleted": True}
        await _wait_for_process_exit(terminals[1][1])
        assert manager.registry.list_for_conversation(context_id) == []
    finally:
        await manager.shutdown()


@pytest.mark.skipif(not _HAS_TMUX, reason="tmux not installed")
async def test_terminal_creation_requires_current_context_ownership(tmp_path: Path) -> None:
    """A stale draft view cannot add a shell after another view adopts its context."""

    manager = WorkspaceContextManager()
    try:
        adopted_context_id = str(
            _ok(await _request(manager, "create", params={"workspace": str(tmp_path)}))["id"]
        )
        original = _ok(
            await _request(
                manager,
                "create_terminal",
                context_id=adopted_context_id,
                params={"terminal": "bash", "session_key": "original", "session_id": None},
            )
        )
        original_instance = manager.registry.get(adopted_context_id, "bash", "original")
        assert original_instance is not None
        original_pid = original_instance.pane_pid_sync()
        assert original_pid is not None

        _ok(
            await _request(
                manager,
                "handoff",
                context_id=adopted_context_id,
                params={"session_id": "session-one", "workspace": str(tmp_path)},
            )
        )
        stale_create = await _request(
            manager,
            "create_terminal",
            context_id=adopted_context_id,
            params={"terminal": "bash", "session_key": "stale", "session_id": None},
        )
        assert (stale_create.status, stale_create.error_status) == ("error", 409)
        assert manager.registry.get(adopted_context_id, "bash", "original") is original_instance
        assert manager.registry.get(adopted_context_id, "bash", "stale") is None
        assert await original_instance.is_alive()
        wrong_session_create = await _request(
            manager,
            "create_terminal",
            context_id=adopted_context_id,
            params={
                "terminal": "bash",
                "session_key": "wrong-session",
                "session_id": "session-two",
            },
        )
        assert (wrong_session_create.status, wrong_session_create.error_status) == ("error", 409)
        assert manager.registry.get(adopted_context_id, "bash", "wrong-session") is None

        session_terminal = _ok(
            await _request(
                manager,
                "create_terminal",
                context_id=adopted_context_id,
                params={
                    "terminal": "bash",
                    "session_key": "session-shell",
                    "session_id": "session-one",
                },
            )
        )
        assert session_terminal["session_id"] == "session-one"

        draft_context_id = str(
            _ok(await _request(manager, "create", params={"workspace": str(tmp_path)}))["id"]
        )
        assert draft_context_id != adopted_context_id
        draft_terminal = _ok(
            await _request(
                manager,
                "create_terminal",
                context_id=draft_context_id,
                params={"terminal": "bash", "session_key": "new-draft", "session_id": None},
            )
        )
        draft_instance = manager.registry.get(draft_context_id, "bash", "new-draft")
        assert draft_instance is not None
        draft_pid = draft_instance.pane_pid_sync()
        assert draft_pid is not None
        assert draft_pid != original_pid
        assert draft_terminal["workspace_context_id"] == draft_context_id

        _ok(
            await _request(
                manager,
                "delete_terminal",
                context_id=adopted_context_id,
                params={"terminal_id": session_terminal["id"], "session_id": "session-one"},
            )
        )
        _ok(
            await _request(
                manager,
                "delete_terminal",
                context_id=adopted_context_id,
                params={"terminal_id": original["id"], "session_id": "session-one"},
            )
        )
        _ok(await _request(manager, "delete", context_id=draft_context_id))
    finally:
        await manager.shutdown()


@pytest.mark.skipif(not _HAS_TMUX, reason="tmux not installed")
async def test_stale_view_cannot_delete_context_after_other_view_handoff(
    tmp_path: Path,
) -> None:
    """A stale draft cleanup cannot remove another view's adopted shell."""

    manager = WorkspaceContextManager()
    adopting_view = object()
    stale_view = object()
    try:
        context_id = str(
            _ok(await _request(manager, "create", params={"workspace": str(tmp_path)}))["id"]
        )
        terminal = _ok(
            await _request(
                manager,
                "create_terminal",
                context_id=context_id,
                params={"terminal": "bash", "session_key": "shared"},
            )
        )
        instance = manager.registry.get(context_id, "bash", "shared")
        assert instance is not None
        pane_pid = instance.pane_pid_sync()
        assert pane_pid is not None

        adopted = _ok(
            await _request(
                manager,
                "handoff",
                context_id=context_id,
                params={"session_id": "session-shared", "workspace": str(tmp_path)},
                tunnel=adopting_view,
            )
        )
        assert adopted["session_id"] == "session-shared"
        rebound = await _request(
            manager,
            "handoff",
            context_id=context_id,
            params={"session_id": "session-other", "workspace": str(tmp_path)},
            tunnel=adopting_view,
        )
        assert (rebound.status, rebound.error_status) == ("error", 409)
        stale_delete = _ok(
            await _request(
                manager,
                "delete",
                context_id=context_id,
                tunnel=stale_view,
            )
        )
        assert stale_delete["deleted"] is False
        assert stale_delete["session_id"] == "session-shared"
        assert manager.registry.get(context_id, "bash", "shared") is instance
        assert await instance.is_alive()

        stale_terminal_delete = await _request(
            manager,
            "delete_terminal",
            context_id=context_id,
            params={"terminal_id": terminal["id"], "session_id": None},
            tunnel=stale_view,
        )
        assert (stale_terminal_delete.status, stale_terminal_delete.error_status) == (
            "error",
            409,
        )
        assert manager.registry.get(context_id, "bash", "shared") is instance
        assert await instance.is_alive()

        terminal_delete = _ok(
            await _request(
                manager,
                "delete_terminal",
                context_id=context_id,
                params={"terminal_id": terminal["id"], "session_id": "session-shared"},
                tunnel=adopting_view,
            )
        )
        assert terminal_delete["deleted"] is True
        assert terminal_delete["context_deleted"] is True
        await _wait_for_process_exit(pane_pid)
        assert context_id not in manager._contexts  # pyright: ignore[reportPrivateUsage]
    finally:
        await manager.shutdown()


@pytest.mark.skipif(not _HAS_TMUX, reason="tmux not installed")
@pytest.mark.parametrize("delete_before_handoff", [False, True])
async def test_repeated_adopted_terminal_deletion_does_not_exhaust_context_limit(
    tmp_path: Path,
    delete_before_handoff: bool,
) -> None:
    """Final-terminal cleanup around handoff releases its context quota slot."""

    manager = WorkspaceContextManager()
    try:
        for index in range(20):
            context_id = str(
                _ok(await _request(manager, "create", params={"workspace": str(tmp_path)}))["id"]
            )
            terminal = _ok(
                await _request(
                    manager,
                    "create_terminal",
                    context_id=context_id,
                    params={"terminal": "bash", "session_key": f"cycle-{index}"},
                )
            )
            if delete_before_handoff:
                deleted = _ok(
                    await _request(
                        manager,
                        "delete_terminal",
                        context_id=context_id,
                        params={"terminal_id": terminal["id"], "session_id": None},
                    )
                )
                assert deleted == {"id": terminal["id"], "deleted": True}
                assert context_id in manager._contexts  # pyright: ignore[reportPrivateUsage]
                handed_off = _ok(
                    await _request(
                        manager,
                        "handoff",
                        context_id=context_id,
                        params={"session_id": f"session-{index}", "workspace": str(tmp_path)},
                    )
                )
                assert handed_off["context_deleted"] is True
            else:
                _ok(
                    await _request(
                        manager,
                        "handoff",
                        context_id=context_id,
                        params={"session_id": f"session-{index}", "workspace": str(tmp_path)},
                    )
                )
                deleted = _ok(
                    await _request(
                        manager,
                        "delete_terminal",
                        context_id=context_id,
                        params={
                            "terminal_id": terminal["id"],
                            "session_id": f"session-{index}",
                        },
                    )
                )
                assert deleted == {
                    "id": terminal["id"],
                    "deleted": True,
                    "context_deleted": True,
                }
            assert context_id not in manager._contexts  # pyright: ignore[reportPrivateUsage]
        assert manager._contexts == {}  # pyright: ignore[reportPrivateUsage]
    finally:
        await manager.shutdown()


@pytest.mark.skipif(not _HAS_TMUX, reason="tmux not installed")
async def test_shutdown_closes_shell_and_refuses_new_work(tmp_path: Path) -> None:
    """Host shutdown terminates all shells and closes the manager to new requests."""

    manager = WorkspaceContextManager()
    context_id = str(
        _ok(await _request(manager, "create", params={"workspace": str(tmp_path)}))["id"]
    )
    _ok(
        await _request(
            manager,
            "create_terminal",
            context_id=context_id,
            params={"terminal": "bash", "session_key": "one"},
        )
    )
    instance = manager.registry.get(context_id, "bash", "one")
    assert instance is not None
    pid = instance.pane_pid_sync()
    assert pid is not None

    await manager.shutdown()
    await _wait_for_process_exit(pid)
    refused = await _request(manager, "create", params={"workspace": str(tmp_path)})
    assert (refused.status, refused.error_status) == ("error", 503)


async def test_limits_and_invalid_terminal_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Malformed terminal requests and per-owner context exhaustion are refused."""

    monkeypatch.setattr("omnigent.host.workspace_contexts._MAX_CONTEXTS_PER_USER", 1)
    manager = WorkspaceContextManager()
    try:
        context_id = str(
            _ok(await _request(manager, "create", params={"workspace": str(tmp_path)}))["id"]
        )
        limited = await _request(manager, "create", params={"workspace": str(tmp_path)})
        assert (limited.status, limited.error_status) == ("error", 429)

        for params in (
            {},
            {"terminal": "codex", "session_key": "one"},
            {"terminal": "bash", "session_key": "contains space"},
            {"terminal": "bash", "session_key": "x" * 81},
        ):
            invalid = await _request(
                manager, "create_terminal", context_id=context_id, params=params
            )
            assert (invalid.status, invalid.error_status) == ("error", 400)

        missing_terminal = await _request(
            manager,
            "attach",
            context_id=context_id,
            params={"terminal_id": "terminal_bash_missing", "channel_id": "channel"},
        )
        assert (missing_terminal.status, missing_terminal.error_status) == ("error", 404)
    finally:
        await manager.shutdown()

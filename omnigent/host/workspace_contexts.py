"""Host-owned shells that can exist before a conversation is created."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from fastapi import WebSocket

from omnigent.entities.session_resources import (
    resolve_terminal_entry_by_resource_id,
    session_resource_view_to_dict,
    terminal_resource_view,
)
from omnigent.host.frames import (
    HostWorkspaceContextRequestFrame,
    HostWorkspaceContextResultFrame,
    HostWorkspaceContextStreamFrame,
)
from omnigent.inner.datamodel import OSEnvSpec, TerminalEnvSpec
from omnigent.terminals.control_bridge import bridge_tmux_control_to_websocket
from omnigent.terminals.registry import TerminalListEntry, TerminalRegistry
from omnigent.util.subprocess_ownership import SubprocessOwnership

LEASE_SECONDS = 600
_MAX_CONTEXTS_PER_USER = 16
_MAX_TERMINALS_PER_CONTEXT = 16
_MAX_CHANNELS_PER_CONTEXT = 32
_logger = logging.getLogger(__name__)
StreamSender = Callable[[HostWorkspaceContextStreamFrame], Awaitable[None]]


class WorkspaceContextError(Exception):
    """An expected context operation refusal, suitable for an HTTP response."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class _Context:
    id: str
    user_id: str
    workspace: str
    touched_at: float
    session_id: str | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    channels: set[str] = field(default_factory=set)

    def payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "workspace": self.workspace,
            "session_id": self.session_id,
            "lease_seconds": LEASE_SECONDS,
        }


class _ChannelSocket:
    """Adapt a multiplexed host channel to the existing tmux bridge."""

    def __init__(self, channel_id: str, send: StreamSender, tunnel: object) -> None:
        self.channel_id = channel_id
        self.tunnel = tunnel
        self._send = send
        self.incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
        self.closed = False
        self.disconnect_requested = False
        self.task: asyncio.Task[None] | None = None

    async def receive(self) -> dict[str, Any]:
        return await self.incoming.get()

    async def send_bytes(self, data: bytes) -> None:
        await self._send(
            HostWorkspaceContextStreamFrame(
                channel_id=self.channel_id,
                data=base64.b64encode(data).decode("ascii"),
                binary=True,
            )
        )

    async def send_text(self, data: str) -> None:
        await self._send(HostWorkspaceContextStreamFrame(channel_id=self.channel_id, data=data))

    async def close(self, code: int = 1000, reason: str = "") -> None:
        del reason  # The multiplexed close carries a code only.
        if not self.closed:
            self.closed = True
            await self._send(
                HostWorkspaceContextStreamFrame(
                    channel_id=self.channel_id,
                    close_code=code,
                )
            )

    def disconnect(self) -> None:
        if self.task is not None and not self.disconnect_requested:
            self.disconnect_requested = True
            self.task.cancel()


@dataclass
class _PendingChannel:
    tunnel: object
    cancelled: bool = False


class _WorkspaceTerminalRegistry(TerminalRegistry):
    """Draft keys are registry namespaces, never conversation links."""

    def conversation_link_for_id(self, conversation_id: str) -> str:
        del conversation_id
        return ""


class WorkspaceContextManager:
    """Own pre-chat terminal state for the daemon lifetime, across tunnel reconnects."""

    def __init__(
        self,
        *,
        registry: TerminalRegistry | None = None,
        clock: Callable[[], float] = time.monotonic,
        tunnel_is_live: Callable[[object], bool] | None = None,
    ) -> None:
        self.registry = registry or _WorkspaceTerminalRegistry()
        self._clock = clock
        self._tunnel_is_live = tunnel_is_live or (lambda _tunnel: True)
        self._contexts: dict[str, _Context] = {}
        self._channels: dict[str, _ChannelSocket] = {}
        self._pending_channels: dict[str, _PendingChannel] = {}
        self._reaper: asyncio.Task[None] | None = None
        self._closed = False
        self.subprocess_ownership = SubprocessOwnership()
        self._cleanup_tasks: set[asyncio.Task[None]] = set()

    async def handle(
        self,
        frame: HostWorkspaceContextRequestFrame,
        *,
        send: StreamSender,
        tunnel: object,
    ) -> HostWorkspaceContextResultFrame:
        if self._closed:
            return HostWorkspaceContextResultFrame(
                frame.request_id,
                "error",
                error_status=503,
                error="Host is shutting down",
            )
        if self._reaper is None:
            self._reaper = asyncio.create_task(self._reap_loop(), name="workspace-context-leases")
        pending: _PendingChannel | None = None
        try:
            if frame.op == "attach" and frame.user_id:
                ctx = self._contexts.get(frame.context_id)
                if ctx is not None and ctx.user_id == frame.user_id:
                    channel_id = frame.params.get("channel_id")
                    if (
                        not isinstance(channel_id, str)
                        or not channel_id
                        or channel_id in self._channels
                        or channel_id in self._pending_channels
                    ):
                        raise WorkspaceContextError(400, "A unique channel id is required")
                    pending = _PendingChannel(tunnel)
                    self._pending_channels[channel_id] = pending
            with self.subprocess_ownership.scope():
                payload = await self._operate(frame, send=send, tunnel=tunnel, pending=pending)
            return HostWorkspaceContextResultFrame(frame.request_id, "ok", payload=payload)
        except WorkspaceContextError as exc:
            return HostWorkspaceContextResultFrame(
                frame.request_id,
                "error",
                error_status=exc.status,
                error=str(exc),
            )
        except Exception:
            _logger.exception("Workspace context operation %s failed", frame.op)
            return HostWorkspaceContextResultFrame(
                frame.request_id,
                "error",
                error_status=500,
                error="Host workspace operation failed",
            )
        finally:
            if pending is not None:
                channel_id = frame.params.get("channel_id")
                if (
                    isinstance(channel_id, str)
                    and self._pending_channels.get(channel_id) is pending
                ):
                    self._pending_channels.pop(channel_id)

    @staticmethod
    def _workspace(value: object) -> str:
        if not isinstance(value, str) or not value or "\x00" in value:
            raise WorkspaceContextError(400, "An absolute workspace directory is required")
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise WorkspaceContextError(400, "An absolute workspace directory is required")
        try:
            path = path.resolve(strict=True)
        except OSError as exc:
            raise WorkspaceContextError(404, "Workspace directory does not exist") from exc
        if not path.is_dir():
            raise WorkspaceContextError(400, "Workspace must be a directory")
        return str(path)

    @staticmethod
    def _terminal_payload(ctx: _Context, entry: TerminalListEntry) -> dict[str, Any]:
        payload = session_resource_view_to_dict(
            terminal_resource_view(ctx.session_id or "", entry)
        )
        payload.update(
            {
                "object": "workspace.resource",
                "workspace_context_id": ctx.id,
                "session_id": ctx.session_id,
            }
        )
        payload.pop("environment", None)
        return payload

    async def _operate(
        self,
        frame: HostWorkspaceContextRequestFrame,
        *,
        send: StreamSender,
        tunnel: object,
        pending: _PendingChannel | None,
    ) -> dict[str, Any]:
        if not frame.user_id:
            raise WorkspaceContextError(403, "Workspace context owner is required")
        if frame.op == "create":
            workspace = await asyncio.to_thread(self._workspace, frame.params.get("workspace"))
            if self._closed:
                raise WorkspaceContextError(503, "Host is shutting down")
            if (
                sum(c.user_id == frame.user_id for c in self._contexts.values())
                >= _MAX_CONTEXTS_PER_USER
            ):
                raise WorkspaceContextError(429, "Too many active workspace contexts")
            ctx = _Context(
                f"workspace_{secrets.token_hex(16)}", frame.user_id, workspace, self._clock()
            )
            self._contexts[ctx.id] = ctx
            return ctx.payload()
        ctx = self._contexts.get(frame.context_id)
        if ctx is None or ctx.user_id != frame.user_id:
            raise WorkspaceContextError(404, "Workspace context not found")
        async with ctx.lock:
            if self._contexts.get(ctx.id) is not ctx:
                raise WorkspaceContextError(404, "Workspace context not found")
            if not ctx.channels and self._clock() - ctx.touched_at >= LEASE_SECONDS:
                await self._delete(ctx)
                raise WorkspaceContextError(404, "Workspace context expired")
            if pending is not None and pending.cancelled:
                raise WorkspaceContextError(499, "Terminal attachment was cancelled")
            ctx.touched_at = self._clock()
            if frame.op in {"describe", "heartbeat"}:
                return ctx.payload()
            if frame.op == "delete":
                if ctx.session_id is not None:
                    payload = ctx.payload()
                    payload["deleted"] = False
                    return payload
                await self._delete(ctx)
                return {"id": ctx.id, "deleted": True}
            if frame.op == "handoff":
                session_id = frame.params.get("session_id")
                if not isinstance(session_id, str) or not session_id:
                    raise WorkspaceContextError(400, "Session id is required")
                workspace = await asyncio.to_thread(self._workspace, frame.params.get("workspace"))
                if workspace != ctx.workspace:
                    raise WorkspaceContextError(409, "Session workspace does not match the draft")
                if ctx.session_id is not None and ctx.session_id != session_id:
                    raise WorkspaceContextError(
                        409, "Workspace context already belongs to a session"
                    )
                ctx.session_id = session_id
                entries = self.registry.list_for_conversation(ctx.id)
                if not entries:
                    payload = ctx.payload()
                    await self._delete(ctx)
                    payload["context_deleted"] = True
                    return payload
                for entry in entries:
                    with contextlib.suppress(RuntimeError):
                        await entry.instance.set_conversation_link(f"/c/{session_id}")
                return ctx.payload()
            if frame.op == "list_terminals":
                resources = [
                    self._terminal_payload(ctx, entry)
                    for entry in self.registry.list_for_conversation(ctx.id)
                ]
                return {
                    "object": "list",
                    "data": resources,
                    "has_more": False,
                    "first_id": resources[0]["id"] if resources else None,
                    "last_id": resources[-1]["id"] if resources else None,
                }
            if frame.op == "create_terminal":
                name = frame.params.get("terminal", "bash")
                key = frame.params.get("session_key")
                if (
                    name != "bash"
                    or not isinstance(key, str)
                    or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", key)
                ):
                    raise WorkspaceContextError(
                        400, "A bash terminal and a valid session_key are required"
                    )
                if (
                    self.registry.get(ctx.id, name, key) is None
                    and len(self.registry.list_for_conversation(ctx.id))
                    >= _MAX_TERMINALS_PER_CONTEXT
                ):
                    raise WorkspaceContextError(
                        429, "Too many terminals in this workspace context"
                    )
                await self.registry.launch(
                    ctx.id,
                    name,
                    key,
                    TerminalEnvSpec(
                        command="bash", args=["-l"], os_env=OSEnvSpec(cwd=ctx.workspace)
                    ),
                )
                entry = next(
                    entry
                    for entry in self.registry.list_for_conversation(ctx.id)
                    if entry.session_key == key
                )
                if ctx.session_id is not None:
                    await entry.instance.set_conversation_link(f"/c/{ctx.session_id}")
                return self._terminal_payload(ctx, entry)
            terminal_id = frame.params.get("terminal_id")
            if not isinstance(terminal_id, str):
                raise WorkspaceContextError(400, "Terminal id is required")
            entry = resolve_terminal_entry_by_resource_id(ctx.id, terminal_id, self.registry)
            if entry is None:
                raise WorkspaceContextError(404, "Terminal not found")
            if frame.op == "delete_terminal":
                await self.registry.close(ctx.id, entry.terminal_name, entry.session_key)
                payload: dict[str, Any] = {"id": terminal_id, "deleted": True}
                if ctx.session_id is not None and not self.registry.list_for_conversation(ctx.id):
                    await self._delete(ctx)
                    payload["context_deleted"] = True
                return payload
            if frame.op == "attach":
                channel_id = frame.params.get("channel_id")
                if (
                    not isinstance(channel_id, str)
                    or not channel_id
                    or pending is None
                    or self._pending_channels.get(channel_id) is not pending
                ):
                    raise WorkspaceContextError(400, "A unique channel id is required")
                if len(ctx.channels) >= _MAX_CHANNELS_PER_CONTEXT:
                    raise WorkspaceContextError(429, "Too many terminal attachments")
                is_alive = entry.instance.running and await entry.instance.is_alive()
                if not is_alive:
                    raise WorkspaceContextError(404, "Terminal is no longer running")
                if not self._tunnel_is_live(tunnel):
                    raise WorkspaceContextError(503, "Host tunnel disconnected during attachment")
                if pending.cancelled:
                    raise WorkspaceContextError(499, "Terminal attachment was cancelled")
                socket = _ChannelSocket(channel_id, send, tunnel)
                self._channels[channel_id] = socket
                ctx.channels.add(channel_id)

                async def bridge() -> None:
                    try:
                        await bridge_tmux_control_to_websocket(
                            cast(WebSocket, socket),
                            socket_path=str(entry.instance.socket_path),
                            tmux_target=entry.instance.tmux_target,
                            read_only=frame.params.get("read_only") is True,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        _logger.exception("Workspace terminal attachment ended")
                    finally:
                        self._channels.pop(channel_id, None)
                        ctx.channels.discard(channel_id)
                        ctx.touched_at = self._clock()
                        with contextlib.suppress(Exception):
                            await socket.close()

                socket.task = asyncio.create_task(
                    bridge(), name=f"workspace-terminal-{channel_id}"
                )

                def release_channel(_task: asyncio.Task[None]) -> None:
                    # Cancellation before the coroutine starts skips its finally block.
                    self._channels.pop(channel_id, None)
                    ctx.channels.discard(channel_id)
                    ctx.touched_at = self._clock()

                socket.task.add_done_callback(release_channel)
                return {"channel_id": channel_id}
            raise WorkspaceContextError(400, "Unknown workspace context operation")

    def receive(self, frame: HostWorkspaceContextStreamFrame, *, tunnel: object) -> None:
        socket = self._channels.get(frame.channel_id)
        if socket is None:
            pending = self._pending_channels.get(frame.channel_id)
            if pending is not None and pending.tunnel is tunnel and frame.close_code is not None:
                pending.cancelled = True
            return
        if socket.tunnel is not tunnel:
            return
        if frame.close_code is not None:
            socket.disconnect()
            return
        try:
            if len(frame.data) > 256 * 1024:
                raise ValueError("terminal frame too large")
            message: dict[str, Any] = {"type": "websocket.receive"}
            if frame.binary:
                message["bytes"] = base64.b64decode(frame.data, validate=True)
            else:
                message["text"] = frame.data
            socket.incoming.put_nowait(message)
        except (ValueError, asyncio.QueueFull):
            socket.disconnect()

    async def disconnect(self, tunnel: object) -> None:
        tasks = []
        for pending in self._pending_channels.values():
            if pending.tunnel is tunnel:
                pending.cancelled = True
        for socket in list(self._channels.values()):
            if socket.tunnel is tunnel:
                socket.disconnect()
                if socket.task is not None:
                    tasks.append(socket.task)
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _delete(self, ctx: _Context) -> None:
        self._contexts.pop(ctx.id, None)
        with self.subprocess_ownership.scope():
            cleanup = asyncio.create_task(
                self._close_context(ctx), name="workspace-context-cleanup"
            )
        self._cleanup_tasks.add(cleanup)
        cleanup.add_done_callback(self._cleanup_tasks.discard)
        await asyncio.shield(cleanup)

    async def _close_context(self, ctx: _Context) -> None:
        tasks = []
        for channel_id in list(ctx.channels):
            socket = self._channels.get(channel_id)
            if socket is not None:
                socket.disconnect()
                if socket.task is not None:
                    tasks.append(socket.task)
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.registry.cleanup_conversation(ctx.id)

    async def reap_expired(self) -> None:
        for ctx in list(self._contexts.values()):
            async with ctx.lock:
                if not ctx.channels and self._clock() - ctx.touched_at >= LEASE_SECONDS:
                    await self._delete(ctx)

    async def _reap_loop(self) -> None:
        while True:
            await asyncio.sleep(15)
            try:
                await self.reap_expired()
            except Exception:
                _logger.exception("Workspace context lease cleanup failed")

    async def shutdown(self) -> None:
        self._closed = True
        if self._reaper is not None:
            self._reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reaper
            self._reaper = None
        for ctx in list(self._contexts.values()):
            async with ctx.lock:
                await self._delete(ctx)
        await asyncio.gather(*self._cleanup_tasks, return_exceptions=True)
        with self.subprocess_ownership.scope():
            await self.registry.shutdown()

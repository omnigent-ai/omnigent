"""Codex WebSocket-JSON-RPC implementation of :class:`NativeServerTransport`.

The second concrete transport (with
:class:`omnigent.opencode_http_transport.OpenCodeHttpTransport`) that
proves the native-server abstraction generalizes across wire protocols.
It adapts the existing Codex app-server client + bridge state into the
:class:`NativeServerTransport` protocol — the same injection logic the
battle-tested :class:`omnigent.inner.codex_native_executor.CodexNativeExecutor`
uses (``turn/start`` vs ``turn/steer``, ``turn/interrupt``), without
disturbing that production path.

The Codex server's *lifecycle* (process launch, event forwarding) stays
runner-owned (see ``_auto_create_codex_terminal``); the methods that would
duplicate it (``start_server`` / ``events``) raise to make that ownership
explicit. The injection core (``send_prompt`` / ``abort`` /
``create_or_resume_session`` / ``build_tui_attach_command``) is fully
implemented and is what the conformance suite exercises.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable, Mapping
from pathlib import Path
from typing import Any

from omnigent.native_server_transport import (
    NativeEvent,
    NativeLaunchConfig,
    NativePermissionDecision,
    NativePrompt,
    NativeServerHandle,
)

_logger = logging.getLogger(__name__)

# A Codex app-server client exposing async ``connect`` / ``request`` /
# ``close`` (``omnigent.codex_native_app_server.CodexAppServerClient``).
CodexClientFactory = Callable[[], Any]

# Public surface of this transport module. ``CodexClientFactory`` is the
# documented annotation for ``CodexWsTransport(client_factory=...)``; export
# it so the alias reads as intended public API (its only other use is a
# PEP 563 stringified annotation, which static analysis can't see as a load).
__all__ = ["CodexClientFactory", "CodexWsTransport"]


def _prompt_to_input_items(prompt: NativePrompt) -> list[dict[str, Any]]:
    """
    Build Codex app-server input items from a :class:`NativePrompt`.

    :param prompt: The normalized prompt.
    :returns: Codex ``turn/start`` / ``turn/steer`` input items.
    """
    items: list[dict[str, Any]] = []
    if prompt.text:
        items.append({"type": "text", "text": prompt.text})
    for attachment in prompt.attachments:
        path = attachment.get("path") or attachment.get("local_path")
        if isinstance(path, str) and path:
            items.append({"type": "localImage", "path": path})
    return items


class CodexWsTransport:
    """
    WebSocket-JSON-RPC transport for codex-native.

    :param bridge_dir: Codex bridge dir to read thread/socket/turn state.
    :param client_factory: Builds a Codex app-server client; ``None`` uses
        ``client_for_transport`` against the bridge socket.
    """

    descriptor_id = "codex-native"

    def __init__(
        self,
        *,
        bridge_dir: Path | None = None,
        client_factory: CodexClientFactory | None = None,
    ) -> None:
        self._bridge_dir = bridge_dir
        self._client_factory = client_factory

    def _read_state(self) -> Any:
        """
        Read codex bridge state.

        :returns: The bridge state, or ``None``.
        """
        if self._bridge_dir is None:
            return None
        from omnigent.codex_native_bridge import read_bridge_state

        return read_bridge_state(self._bridge_dir)

    def _client(self, socket_path: str | None = None) -> Any:
        """
        Build a Codex app-server client.

        :param socket_path: Transport endpoint (unix:// or ws://).
        :returns: A Codex app-server client.
        :raises RuntimeError: When no endpoint is resolvable.
        """
        if self._client_factory is not None:
            return self._client_factory()
        from omnigent.codex_native_app_server import client_for_transport

        if socket_path is None:
            state = self._read_state()
            socket_path = state.socket_path if state is not None else None
        if not socket_path:
            raise RuntimeError("CodexWsTransport has no socket path / client factory")
        return client_for_transport(socket_path, client_name="omnigent-codex-ws-transport")

    async def start_server(self, launch: NativeLaunchConfig) -> NativeServerHandle:
        """Codex server lifecycle is runner-owned (see ``_auto_create_codex_terminal``)."""
        raise NotImplementedError("Codex app-server lifecycle is runner-owned")

    async def stop_server(self) -> None:
        """No-op: the runner owns the Codex app-server process."""
        return

    async def create_or_resume_session(self, launch: NativeLaunchConfig) -> str:
        """Return the resume thread id, or the bridge's thread id."""
        if launch.external_session_id:
            return launch.external_session_id
        state = self._read_state()
        if state is not None:
            return str(state.thread_id)
        raise RuntimeError("CodexWsTransport cannot resolve a thread id")

    async def send_prompt(self, session_id: str, prompt: NativePrompt) -> Mapping[str, Any]:
        """Inject a turn via ``turn/start`` (or ``turn/steer`` if active)."""
        from omnigent.codex_native_bridge import update_active_turn_id

        input_items = _prompt_to_input_items(prompt)
        state = self._read_state()
        active_turn_id = state.active_turn_id if state is not None else None
        client = self._client(state.socket_path if state is not None else None)
        await client.connect()
        try:
            if active_turn_id is not None:
                response = await client.request(
                    "turn/steer",
                    {
                        "threadId": session_id,
                        "expectedTurnId": active_turn_id,
                        "input": input_items,
                    },
                )
                turn_id = response.get("result", {}).get("turnId")
            else:
                response = await client.request(
                    "turn/start",
                    {"threadId": session_id, "input": input_items},
                )
                turn_id = response.get("result", {}).get("turn", {}).get("id")
            if isinstance(turn_id, str) and turn_id and self._bridge_dir is not None:
                update_active_turn_id(self._bridge_dir, turn_id)
            return dict(response)
        finally:
            await client.close()

    async def abort(self, session_id: str) -> bool:
        """Abort via ``turn/interrupt`` for the active turn."""
        state = self._read_state()
        active_turn_id = state.active_turn_id if state is not None else None
        if active_turn_id is None:
            return False
        client = self._client(state.socket_path if state is not None else None)
        await client.connect()
        try:
            await client.request(
                "turn/interrupt",
                {"threadId": session_id, "turnId": active_turn_id},
            )
        finally:
            await client.close()
        return True

    async def events(self, session_id: str) -> AsyncIterator[NativeEvent]:
        """Codex event forwarding is runner-owned (WS notification stream)."""
        del session_id
        raise NotImplementedError("Codex event forwarding is runner-owned")
        yield  # pragma: no cover - makes this an async generator

    async def list_history(self, session_id: str) -> list[Mapping[str, Any]]:
        """Codex history lives in rollout files / app-server; runner-owned."""
        raise NotImplementedError("Codex history is runner-owned")

    async def fork(self, session_id: str, *, at_message_id: str | None = None) -> str:
        """Codex forks rebuild rollouts in the runner; not a transport op."""
        raise NotImplementedError("Codex fork is runner-owned")

    async def reply_permission(self, decision: NativePermissionDecision) -> None:
        """Codex uses elicitation resolution, not a permission reply API."""
        raise NotImplementedError("Codex permissions use elicitation resolution")

    def build_tui_attach_command(
        self, launch: NativeLaunchConfig, session_id: str
    ) -> tuple[list[str], Mapping[str, str]]:
        """Build the ``codex ... --remote`` attach argv (no env override)."""
        from omnigent.codex_native_app_server import build_codex_remote_args

        remote_url = launch.server_url or ""
        argv = build_codex_remote_args(
            codex_args=tuple(launch.terminal_launch_args),
            thread_id=session_id,
            remote_url=remote_url,
        )
        return argv, {}

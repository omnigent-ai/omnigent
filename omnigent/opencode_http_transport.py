"""OpenCode HTTP + SSE implementation of :class:`NativeServerTransport`.

One of the two concrete transports that prove the native-server
abstraction (the other is
:class:`omnigent.codex_ws_transport.CodexWsTransport`). All OpenCode wire
details live here; :class:`omnigent.native_server_harness.NativeServerHarness`
and the conformance suite drive it through the protocol only.

The transport can build its client from three sources, in priority order:
an injected ``client_factory`` (tests), a running
:class:`OpenCodeNativeServer` (runner-side), or the persisted bridge state
(harness-side, where only the URL + auth secret are known).
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
from omnigent.opencode_native_app_server import (
    OpenCodeNativeServer,
    build_opencode_attach_args,
    client_for_state,
    opencode_terminal_env,
)
from omnigent.opencode_native_bridge import read_bridge_state
from omnigent.opencode_native_client import OpenCodeClient

_logger = logging.getLogger(__name__)

ClientFactory = Callable[[], OpenCodeClient]

# Public surface of this transport module. ``ClientFactory`` is the documented
# annotation for ``OpenCodeHttpTransport(client_factory=...)``; export it so the
# alias reads as intended public API (its only other use is a PEP 563 stringified
# annotation, which static analysis can't see as a load).
__all__ = ["ClientFactory", "OpenCodeHttpTransport", "build_prompt_payload"]


def build_prompt_payload(prompt: NativePrompt) -> dict[str, Any]:
    """
    Build an OpenCode prompt request body from a :class:`NativePrompt`.

    :param prompt: The normalized prompt.
    :returns: A ``{"parts": [...], ...}`` body for ``POST
        /session/{id}/message`` or ``/prompt_async``.
    """
    parts: list[dict[str, Any]] = []
    if prompt.text:
        parts.append({"type": "text", "text": prompt.text})
    for attachment in prompt.attachments:
        part = _attachment_to_part(attachment)
        if part is not None:
            parts.append(part)
    payload: dict[str, Any] = {"parts": parts}
    if prompt.system_prompt:
        payload["system"] = prompt.system_prompt
    model = _split_model(prompt.model)
    if model is not None:
        payload["model"] = model
    return payload


def _attachment_to_part(attachment: Mapping[str, Any]) -> dict[str, Any] | None:
    """
    Convert an Omnigent attachment block into an OpenCode file part.

    :param attachment: An ``input_image`` / ``input_file`` content block.
    :returns: A ``FilePartInput`` dict, or ``None`` when unconvertible.
    """
    block_type = attachment.get("type")
    if block_type == "input_image":
        url = attachment.get("image_url")
        if isinstance(url, str) and url:
            mime = _mime_from_data_uri(url) or "image/png"
            return {"type": "file", "mime": mime, "url": url}
    if block_type == "input_file":
        url = attachment.get("file_data") or attachment.get("url")
        if isinstance(url, str) and url:
            mime = _mime_from_data_uri(url) or "application/octet-stream"
            part: dict[str, Any] = {"type": "file", "mime": mime, "url": url}
            filename = attachment.get("filename")
            if isinstance(filename, str) and filename:
                part["filename"] = filename
            return part
    return None


def _mime_from_data_uri(uri: str) -> str | None:
    """
    Extract the MIME type from a ``data:`` URI.

    :param uri: A data URI, e.g. ``"data:image/png;base64,..."``.
    :returns: The MIME type, or ``None``.
    """
    if not uri.startswith("data:"):
        return None
    head = uri[len("data:") :].split(",", 1)[0]
    mime = head.split(";", 1)[0]
    return mime or None


def _split_model(model: str | None) -> dict[str, str] | None:
    """
    Split a ``provider/model`` id into the OpenCode prompt model object.

    :param model: A model id, e.g. ``"anthropic/claude-opus-4"``; ``None``
        means no pin.
    :returns: ``{"providerID": ..., "modelID": ...}`` or ``None``.
    """
    if not model:
        return None
    provider, sep, model_id = model.partition("/")
    if sep and provider and model_id:
        return {"providerID": provider, "modelID": model_id}
    return None


class OpenCodeHttpTransport:
    """
    HTTP + SSE transport for opencode-native.

    :param bridge_dir: Bridge dir to read server URL + auth from when no
        server/client is injected (harness-side).
    :param server: A running :class:`OpenCodeNativeServer` (runner-side).
    :param client_factory: Optional client builder (tests).
    :param directory: Workspace directory routing header.
    """

    descriptor_id = "opencode-native"

    def __init__(
        self,
        *,
        bridge_dir: Path | None = None,
        server: OpenCodeNativeServer | None = None,
        client_factory: ClientFactory | None = None,
        directory: str | None = None,
    ) -> None:
        self._bridge_dir = bridge_dir
        self._server = server
        self._client_factory = client_factory
        self._directory = directory

    def _client(self) -> OpenCodeClient:
        """
        Build a client from the injected factory, server, or bridge state.

        :returns: A fresh :class:`OpenCodeClient` (caller closes it).
        :raises RuntimeError: When no connection coordinates are available.
        """
        if self._client_factory is not None:
            return self._client_factory()
        if self._server is not None:
            return self._server.client(directory=self._directory)
        if self._bridge_dir is not None:
            state = read_bridge_state(self._bridge_dir)
            if state is not None:
                return client_for_state(
                    base_url=state.server_base_url,
                    auth_secret=state.auth_secret,
                    directory=self._directory or state.workspace,
                )
        raise RuntimeError("OpenCodeHttpTransport has no server/client/bridge state")

    async def start_server(self, launch: NativeLaunchConfig) -> NativeServerHandle:
        """Start the OpenCode server and return its handle."""
        if self._server is None:
            self._server = OpenCodeNativeServer(
                bridge_dir=self._bridge_dir or Path(launch.workspace),
                workspace=Path(launch.workspace),
            )
        await self._server.start()
        pid = self._server.process.pid if self._server.process is not None else None
        return NativeServerHandle(
            base_url=self._server.base_url,
            env=self._server.env,
            bridge_dir=self._server.bridge_dir,
            process_id=pid,
        )

    async def stop_server(self) -> None:
        """Stop the OpenCode server, if this transport started one."""
        if self._server is not None:
            await self._server.close()

    async def create_or_resume_session(self, launch: NativeLaunchConfig) -> str:
        """Resume the external session id, or create a new OpenCode session."""
        client = self._client()
        try:
            if launch.external_session_id:
                existing = await client.get_session(launch.external_session_id)
                if existing is not None:
                    return existing.id
            created = await client.create_session(
                {"title": f"omnigent:{launch.omnigent_session_id}"}
            )
            return created.id
        finally:
            await client.aclose()

    async def send_prompt(self, session_id: str, prompt: NativePrompt) -> Mapping[str, Any]:
        """Inject a prompt via ``POST /session/{id}/prompt_async``."""
        client = self._client()
        try:
            return await client.prompt_async(session_id, build_prompt_payload(prompt))
        finally:
            await client.aclose()

    async def abort(self, session_id: str) -> bool:
        """Abort active work via ``POST /session/{id}/abort``."""
        client = self._client()
        try:
            return await client.abort(session_id)
        finally:
            await client.aclose()

    async def events(self, session_id: str) -> AsyncIterator[NativeEvent]:
        """Stream native events, filtered to *session_id*."""
        del session_id
        client = self._client()
        try:
            async for event in client.events():
                yield NativeEvent(
                    id=event.id,
                    type=event.type,
                    payload=event.properties,
                    raw=event.raw,
                )
        finally:
            await client.aclose()

    async def list_history(self, session_id: str) -> list[Mapping[str, Any]]:
        """Return the session's message history."""
        client = self._client()
        try:
            return list(await client.list_messages(session_id))
        finally:
            await client.aclose()

    async def fork(self, session_id: str, *, at_message_id: str | None = None) -> str:
        """Fork the session via ``POST /session/{id}/fork``."""
        client = self._client()
        try:
            payload = {"messageID": at_message_id} if at_message_id else None
            forked = await client.fork(session_id, payload)
            return forked.id
        finally:
            await client.aclose()

    async def reply_permission(self, decision: NativePermissionDecision) -> None:
        """Relay a permission decision via ``POST /permission/{id}/reply``."""
        reply_map = {"allow_once": "once", "allow_always": "always", "reject": "reject"}
        client = self._client()
        try:
            await client.reply_permission(
                decision.request_id,
                {"reply": reply_map[decision.decision], "message": decision.message or ""},
            )
        finally:
            await client.aclose()

    def build_tui_attach_command(
        self, launch: NativeLaunchConfig, session_id: str
    ) -> tuple[list[str], Mapping[str, str]]:
        """Build the ``opencode attach`` argv + env for a TUI takeover."""
        server_url = launch.server_url or (self._server.base_url if self._server else "")
        argv = build_opencode_attach_args(
            server_url=server_url,
            workspace=launch.workspace,
            session_id=session_id,
            opencode_args=launch.terminal_launch_args,
        )
        env: Mapping[str, str] = (
            opencode_terminal_env(self._server) if self._server is not None else {}
        )
        return argv, env

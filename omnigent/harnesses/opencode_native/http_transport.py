"""OpenCode HTTP + SSE implementation of :class:`NativeServerTransport`.

All OpenCode wire details live here;
:class:`omnigent.native.native_server_harness.NativeServerHarness` drives it through
the transport protocol only.

The transport can build its client from three sources, in priority order:
an injected ``client_factory`` (tests), a running
:class:`OpenCodeNativeServer` (runner-side), or the persisted bridge state
(harness-side, where only the URL + auth secret are known).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from pathlib import Path
from typing import TypeAlias, TypedDict

from omnigent.harnesses.opencode_native.app_server import (
    OpenCodeNativeServer,
    client_for_state,
)
from omnigent.harnesses.opencode_native.bridge import (
    read_bridge_state,
    update_last_applied_model,
)
from omnigent.harnesses.opencode_native.client import OpenCodeClient
from omnigent.native.native_server_transport import (
    NativeEvent,
    NativeLaunchConfig,
    NativePermissionDecision,
    NativePrompt,
    NativeServerHandle,
)

_logger = logging.getLogger(__name__)

ClientFactory = Callable[[], OpenCodeClient]

_JsonMapping: TypeAlias = Mapping[str, object]

# Public surface of this transport module. ``ClientFactory`` is the documented
# annotation for ``OpenCodeHttpTransport(client_factory=...)``; export it so the
# alias reads as intended public API (its only other use is a PEP 563 stringified
# annotation, which static analysis can't see as a load).
__all__ = ["ClientFactory", "OpenCodeHttpTransport", "PromptPayload", "build_prompt_payload"]


class PromptPayload(TypedDict):
    """Keyword arguments for :meth:`OpenCodeClient.prompt`."""

    text: str
    files: list[dict[str, str]]
    delivery: str


def build_prompt_payload(
    text: str,
    attachments: Sequence[Mapping[str, object]],
    *,
    delivery: str = "steer",
) -> PromptPayload:
    """
    Build the ``POST /api/session/{id}/prompt`` fields for one prompt.

    Attachments carrying a ``data:`` URI become ``files`` entries; OpenCode
    decodes them server-side (images and PDFs as media, ``text/plain``
    inlined as text). The system prompt and model are not prompt fields in
    v2: instructions ship in the config and the model is switched per session.

    :param text: User text.
    :param attachments: ``input_image`` / ``input_file`` content blocks.
    :param delivery: ``"steer"`` for a normal turn, ``"queue"`` for an
        enqueued one.
    :returns: ``{"text": ..., "files": [{"uri": ..., "name"?: ...}], "delivery": ...}``.
    """
    files: list[dict[str, str]] = []
    for attachment in attachments:
        entry = _attachment_to_file(attachment)
        if entry is not None:
            files.append(entry)
    return {"text": text, "files": files, "delivery": delivery}


def _attachment_to_file(attachment: Mapping[str, object]) -> dict[str, str] | None:
    """
    Convert an Omnigent attachment block into an OpenCode ``files`` entry.

    :param attachment: An ``input_image`` / ``input_file`` content block.
    :returns: ``{"uri": "data:...", "name"?: ...}``, or ``None`` when the block
        has no inline ``data:`` URI (OpenCode reads only ``data:`` and local
        ``file:`` URIs, and a runner-side path is meaningless to it).
    """
    block_type = attachment.get("type")
    if block_type == "input_image":
        uri = attachment.get("image_url")
    elif block_type == "input_file":
        uri = attachment.get("file_data") or attachment.get("url")
    else:
        return None
    if not isinstance(uri, str) or not uri.startswith("data:"):
        return None
    entry = {"uri": uri}
    filename = attachment.get("filename")
    if isinstance(filename, str) and filename:
        entry["name"] = filename
    return entry


def _split_model_id(model: str) -> tuple[str, str, str | None] | None:
    """
    Split a qualified model ref into provider, model id, and optional variant.

    The provider splits off at the first ``/`` (so a provider whose own model
    id contains slashes, e.g. ``"openrouter/acme/model-x"``, keeps them); the
    remainder then splits at the LAST ``#`` for an optional variant suffix.

    :param model: e.g. ``"openrouter/acme/model-x#high"``.
    :returns: ``("openrouter", "acme/model-x", "high")``, or ``None`` when
        *model* has no provider prefix.
    """
    provider, sep, rest = model.partition("/")
    if not sep or not provider or not rest:
        return None
    model_id, hash_sep, variant = rest.rpartition("#")
    if not hash_sep:
        return provider, rest, None
    return provider, model_id, variant or None


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
        # Fallback for when self._bridge_dir is None (e.g. tests using
        # client_factory): the last model pushed via POST /model.
        self._last_applied_model: str | None = None

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
                title=f"omnigent:{launch.omnigent_session_id}",
                directory=launch.workspace,
            )
            return created.id
        finally:
            await client.aclose()

    async def send_prompt(self, session_id: str, prompt: NativePrompt) -> _JsonMapping:
        """Switch the model if needed, then inject via ``POST /api/session/{id}/prompt``."""
        delivery = "queue" if prompt.metadata.get("delivery") == "queue" else "steer"
        payload = build_prompt_payload(prompt.text, prompt.attachments, delivery=delivery)
        client = self._client()
        try:
            if prompt.model:
                await self._apply_model(client, session_id, prompt.model)
            return await client.prompt(
                session_id,
                text=payload["text"],
                files=payload["files"],
                delivery=payload["delivery"],
            )
        finally:
            await client.aclose()

    async def _apply_model(self, client: OpenCodeClient, session_id: str, model: str) -> None:
        """
        Switch the OpenCode session to *model* when it differs from the last one applied.

        The last applied model lives in bridge state so a respawned harness
        process does not resend an unchanged switch every turn.

        :param client: Open client for the session's server.
        :param session_id: OpenCode session id.
        :param model: Qualified ``provider/model`` id, e.g. ``"opencode/big-pickle"``.
        :raises OpenCodeClientError: When OpenCode rejects the switch.
        """
        split = _split_model_id(model)
        if split is None:
            _logger.warning("opencode-native: ignoring unqualified model id %r", model)
            return
        state = read_bridge_state(self._bridge_dir) if self._bridge_dir is not None else None
        last_applied = state.last_applied_model if state is not None else self._last_applied_model
        if last_applied == model:
            return
        provider_id, model_id, variant = split
        await client.set_model(
            session_id, provider_id=provider_id, model_id=model_id, variant=variant
        )
        self._last_applied_model = model
        if self._bridge_dir is not None:
            update_last_applied_model(self._bridge_dir, model)

    async def abort(self, session_id: str) -> bool:
        """Interrupt active work via ``POST /api/session/{id}/interrupt``."""
        client = self._client()
        try:
            return await client.interrupt(session_id)
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

    async def list_history(self, session_id: str) -> list[_JsonMapping]:
        """Return the session's message history."""
        client = self._client()
        try:
            return list(await client.list_messages(session_id))
        finally:
            await client.aclose()

    async def fork(self, session_id: str, *, at_message_id: str | None = None) -> str:
        """Fork the session via ``POST /api/session/{id}/fork``."""
        client = self._client()
        try:
            forked = await client.fork(session_id, before=at_message_id)
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

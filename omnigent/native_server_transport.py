"""Transport abstraction for native-server harnesses.

A *native-server* harness drives a per-conversation server process the
runner owns: the runner starts the server + an event forwarder, and the
harness injects web turns over this transport. OpenCode speaks HTTP + SSE
(:class:`omnigent.opencode_http_transport.OpenCodeHttpTransport`);
:class:`NativeServerTransport` is the seam so the orchestration in
:class:`omnigent.native_server_harness.NativeServerHarness` stays
protocol-agnostic.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable


@dataclass(frozen=True)
class NativeLaunchConfig:
    """
    Inputs needed to start/resume a native server for one conversation.

    :param omnigent_session_id: Omnigent conversation id, e.g.
        ``"conv_abc123"``.
    :param workspace: Working directory the server runs in.
    :param model_override: Persisted model override, or ``None``.
    :param terminal_launch_args: Pass-through CLI args for the TUI/server.
    :param external_session_id: Native session id to resume, or ``None``.
    :param server_url: Existing server URL when reusing one, or ``None``.
    :param auth_headers: Auth headers for the native server.
    """

    omnigent_session_id: str
    workspace: str
    model_override: str | None = None
    terminal_launch_args: tuple[str, ...] = ()
    external_session_id: str | None = None
    server_url: str | None = None
    auth_headers: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class NativeServerHandle:
    """
    A running native server's connection coordinates.

    :param base_url: Server base URL / transport endpoint.
    :param env: Environment used to launch the server.
    :param bridge_dir: The per-session bridge directory.
    :param process_id: OS pid of the server process, when known.
    """

    base_url: str
    env: Mapping[str, str]
    bridge_dir: Path
    process_id: int | None = None


@dataclass(frozen=True)
class NativePrompt:
    """
    A normalized prompt to inject into a native session.

    :param text: The user text.
    :param attachments: Attachment descriptors (image/file blocks).
    :param system_prompt: Optional per-prompt system override.
    :param model: Optional per-prompt model id.
    :param metadata: Transport-specific extras.
    """

    text: str
    attachments: tuple[Mapping[str, Any], ...] = ()
    system_prompt: str | None = None
    model: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def is_empty(self) -> bool:
        """:returns: ``True`` when there is nothing to send."""
        return not self.text and not self.attachments


@dataclass(frozen=True)
class NativeEvent:
    """
    A transport-neutral native server event.

    :param id: Optional event id.
    :param type: Event discriminator.
    :param payload: The event's properties/params.
    :param raw: The full raw envelope.
    """

    id: str | None
    type: str
    payload: Mapping[str, Any]
    raw: Mapping[str, Any]


@dataclass(frozen=True)
class NativePermissionDecision:
    """
    A permission decision to relay to the native server.

    :param request_id: Native permission request id.
    :param decision: Normalized decision.
    :param message: Optional human-readable note.
    """

    request_id: str
    decision: Literal["allow_once", "allow_always", "reject"]
    message: str | None = None


@runtime_checkable
class NativeServerTransport(Protocol):
    """
    Protocol every native-server transport implements.

    Implementations encapsulate all wire details (process launch, session
    lifecycle, prompt injection, abort, event stream, fork, permission
    replies, TUI attach). The shared
    :class:`~omnigent.native_server_harness.NativeServerHarness` calls only
    these methods.
    """

    descriptor_id: str

    async def start_server(self, launch: NativeLaunchConfig) -> NativeServerHandle:
        """Start (or attach to) the native server; return its handle."""
        raise NotImplementedError

    async def stop_server(self) -> None:
        """Stop the native server process this transport started."""
        raise NotImplementedError

    async def create_or_resume_session(self, launch: NativeLaunchConfig) -> str:
        """Resume ``launch.external_session_id`` or create a new session id."""
        raise NotImplementedError

    async def send_prompt(self, session_id: str, prompt: NativePrompt) -> Mapping[str, Any]:
        """Inject a prompt into the native session."""
        raise NotImplementedError

    async def abort(self, session_id: str) -> bool:
        """Abort the native session's active work."""
        raise NotImplementedError

    def events(self, session_id: str) -> AsyncIterator[NativeEvent]:
        """Stream native events for *session_id*."""
        raise NotImplementedError

    async def list_history(self, session_id: str) -> list[Mapping[str, Any]]:
        """Return the native session's message history."""
        raise NotImplementedError

    async def fork(self, session_id: str, *, at_message_id: str | None = None) -> str:
        """Fork the native session; return the new session id."""
        raise NotImplementedError

    async def reply_permission(self, decision: NativePermissionDecision) -> None:
        """Relay a permission decision to the native server."""
        raise NotImplementedError

    def build_tui_attach_command(
        self, launch: NativeLaunchConfig, session_id: str
    ) -> tuple[list[str], Mapping[str, str]]:
        """Build the ``(argv, env)`` for a terminal TUI takeover."""
        raise NotImplementedError

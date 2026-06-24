"""Conformance: NativeServerHarness drives any NativeServerTransport.

The native-server abstraction's correctness check is *two* real transports
behaving identically through the shared base. These tests run
:class:`~omnigent.native_server_harness.NativeServerHarness` over (a) an
in-memory fake transport, (b) the real
:class:`~omnigent.opencode_http_transport.OpenCodeHttpTransport` backed by a
fake OpenCode HTTP server, and (c) the real
:class:`~omnigent.codex_ws_transport.CodexWsTransport` backed by a fake
Codex app-server client — asserting the same run-turn / abort contract for
each.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from omnigent.codex_ws_transport import CodexWsTransport
from omnigent.inner.executor import ExecutorEvent, TurnComplete
from omnigent.native_server_harness import NativeServerHarness
from omnigent.native_server_transport import (
    NativeEvent,
    NativeLaunchConfig,
    NativePermissionDecision,
    NativePrompt,
    NativeServerHandle,
)
from omnigent.opencode_http_transport import OpenCodeHttpTransport
from omnigent.opencode_native_client import OpenCodeClient
from omnigent.runtime.harness_descriptors import HARNESS_DESCRIPTORS

_OPENCODE = HARNESS_DESCRIPTORS["opencode-native"]
_CODEX = HARNESS_DESCRIPTORS["codex-native"]


def _build_prompt(content: Any) -> NativePrompt | None:
    """Minimal content→prompt builder for the contract tests."""
    if isinstance(content, str) and content:
        return NativePrompt(text=content)
    return None


async def _drive(harness: NativeServerHarness) -> list[ExecutorEvent]:
    """Run one turn and collect the emitted events."""
    events: list[ExecutorEvent] = []
    async for event in harness.run_turn([{"role": "user", "content": "hello"}], [], ""):
        events.append(event)
    return events


class _FakeTransport:
    """In-memory transport recording the protocol calls it receives."""

    descriptor_id = "opencode-native"

    def __init__(self) -> None:
        self.prompts: list[tuple[str, NativePrompt]] = []
        self.aborted: list[str] = []
        self.permissions: list[NativePermissionDecision] = []

    async def start_server(self, launch: NativeLaunchConfig) -> NativeServerHandle:
        return NativeServerHandle(base_url="http://fake", env={}, bridge_dir=Path("/tmp"))

    async def stop_server(self) -> None:
        return

    async def create_or_resume_session(self, launch: NativeLaunchConfig) -> str:
        return "ses_fake"

    async def send_prompt(self, session_id: str, prompt: NativePrompt) -> dict[str, Any]:
        self.prompts.append((session_id, prompt))
        return {}

    async def abort(self, session_id: str) -> bool:
        self.aborted.append(session_id)
        return True

    async def events(self, session_id: str):  # pragma: no cover - unused here
        if False:
            yield  # type: ignore[unreachable]

    async def list_history(self, session_id: str) -> list[dict[str, Any]]:
        return []

    async def fork(self, session_id: str, *, at_message_id: str | None = None) -> str:
        return "ses_fork"

    async def reply_permission(self, decision: NativePermissionDecision) -> None:
        self.permissions.append(decision)

    def build_tui_attach_command(
        self, launch: NativeLaunchConfig, session_id: str
    ) -> tuple[list[str], dict[str, str]]:
        return (["attach"], {})


async def test_base_harness_runs_turn_over_fake_transport() -> None:
    """run_turn injects the latest user prompt and yields TurnComplete."""
    transport = _FakeTransport()

    async def resolve() -> str | None:
        return "ses_fake"

    harness = NativeServerHarness(
        descriptor=_OPENCODE,
        transport=transport,
        resolve_session_id=resolve,
        build_prompt=_build_prompt,
    )
    events = await _drive(harness)

    assert [type(e) for e in events] == [TurnComplete]
    assert transport.prompts == [("ses_fake", NativePrompt(text="hello"))]


async def test_base_harness_interrupt_over_fake_transport() -> None:
    """interrupt_session routes to transport.abort."""
    transport = _FakeTransport()

    async def resolve() -> str | None:
        return "ses_fake"

    harness = NativeServerHarness(
        descriptor=_OPENCODE,
        transport=transport,
        resolve_session_id=resolve,
        build_prompt=_build_prompt,
    )
    assert await harness.interrupt_session("k") is True
    assert transport.aborted == ["ses_fake"]


async def test_base_harness_errors_when_session_never_resolves() -> None:
    """A never-ready session yields an ExecutorError, not a hang."""
    transport = _FakeTransport()

    async def resolve() -> str | None:
        return None

    harness = NativeServerHarness(
        descriptor=_OPENCODE,
        transport=transport,
        resolve_session_id=resolve,
        build_prompt=_build_prompt,
        boot_poll_attempts=1,
        boot_poll_delay=0.0,
    )
    events = await _drive(harness)
    assert [type(e).__name__ for e in events] == ["ExecutorError"]
    assert transport.prompts == []


def _opencode_http_handler(seen: dict[str, int]):
    """Build a MockTransport handler for a fake OpenCode server."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/prompt_async"):
            seen["prompt"] = seen.get("prompt", 0) + 1
            return httpx.Response(200, json={})
        if path.endswith("/abort"):
            seen["abort"] = seen.get("abort", 0) + 1
            return httpx.Response(200, json=True)
        return httpx.Response(404, json={})

    return handler


async def test_opencode_http_transport_send_and_abort() -> None:
    """The real HTTP transport injects + aborts via the base harness."""
    seen: dict[str, int] = {}

    def client_factory() -> OpenCodeClient:
        mock = httpx.AsyncClient(
            base_url="http://opencode.test",
            transport=httpx.MockTransport(_opencode_http_handler(seen)),
        )
        return OpenCodeClient("http://opencode.test", client=mock)

    transport = OpenCodeHttpTransport(client_factory=client_factory)

    async def resolve() -> str | None:
        return "ses_http"

    harness = NativeServerHarness(
        descriptor=_OPENCODE,
        transport=transport,
        resolve_session_id=resolve,
        build_prompt=_build_prompt,
    )
    events = await _drive(harness)
    assert [type(e) for e in events] == [TurnComplete]
    assert seen.get("prompt") == 1

    assert await transport.abort("ses_http") is True
    assert seen.get("abort") == 1


class _FakeCodexClient:
    """Minimal Codex app-server client recording JSON-RPC requests."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.connected = False
        self.closed = False

    async def connect(self) -> None:
        self.connected = True

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.requests.append((method, params))
        if method == "turn/start":
            return {"result": {"turn": {"id": "turn_1"}}}
        if method == "turn/steer":
            return {"result": {"turnId": "turn_steer"}}
        return {"result": {}}

    async def close(self) -> None:
        self.closed = True


async def test_codex_ws_transport_send_prompt_over_base(tmp_path: Path) -> None:
    """The real Codex WS transport injects a turn via the base harness."""
    from omnigent.codex_native_bridge import CodexNativeBridgeState, write_bridge_state

    write_bridge_state(
        tmp_path,
        CodexNativeBridgeState(
            session_id="conv_1",
            socket_path="ws://127.0.0.1:9",
            thread_id="thread_1",
            codex_home=str(tmp_path / "home"),
            active_turn_id=None,
        ),
    )
    client = _FakeCodexClient()
    transport = CodexWsTransport(bridge_dir=tmp_path, client_factory=lambda: client)

    async def resolve() -> str | None:
        return "thread_1"

    harness = NativeServerHarness(
        descriptor=_CODEX,
        transport=transport,
        resolve_session_id=resolve,
        build_prompt=_build_prompt,
    )
    events = await _drive(harness)
    assert [type(e) for e in events] == [TurnComplete]
    assert client.requests[0][0] == "turn/start"
    assert client.requests[0][1]["threadId"] == "thread_1"


async def test_codex_ws_transport_abort(tmp_path: Path) -> None:
    """The Codex WS transport aborts the active turn via turn/interrupt."""
    from omnigent.codex_native_bridge import CodexNativeBridgeState, write_bridge_state

    write_bridge_state(
        tmp_path,
        CodexNativeBridgeState(
            session_id="conv_1",
            socket_path="ws://127.0.0.1:9",
            thread_id="thread_1",
            codex_home=str(tmp_path / "home"),
            active_turn_id="turn_active",
        ),
    )
    client = _FakeCodexClient()
    transport = CodexWsTransport(bridge_dir=tmp_path, client_factory=lambda: client)

    assert await transport.abort("thread_1") is True
    assert client.requests == [
        ("turn/interrupt", {"threadId": "thread_1", "turnId": "turn_active"})
    ]


def test_both_transports_satisfy_protocol() -> None:
    """Both concrete transports are structural NativeServerTransports."""
    from omnigent.native_server_transport import NativeServerTransport

    assert isinstance(OpenCodeHttpTransport(), NativeServerTransport)
    assert isinstance(CodexWsTransport(), NativeServerTransport)
    assert OpenCodeHttpTransport().descriptor_id == "opencode-native"
    assert CodexWsTransport().descriptor_id == "codex-native"


def _unused_native_event() -> NativeEvent:
    """Keep NativeEvent imported for type-completeness of the contract."""
    return NativeEvent(id=None, type="x", payload={}, raw={})

"""Unit tests for :class:`omnigent.native_server_harness.NativeServerHarness`.

Drives the transport-agnostic base directly over an in-memory fake transport,
covering the run-turn / interrupt / enqueue orchestration (boot-poll, model
pinning, and the error branches) independent of any concrete harness.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from omnigent.inner.executor import ExecutorConfig, ExecutorError, TurnComplete
from omnigent.native_server_harness import NativeServerHarness
from omnigent.native_server_transport import NativePrompt
from omnigent.runtime.harness_descriptors import HARNESS_DESCRIPTORS

# Base a fake descriptor on the real opencode-native one; tests flip
# ``supports_enqueue`` / ``id`` via ``dataclasses.replace`` as needed.
_BASE_DESCRIPTOR = dataclasses.replace(HARNESS_DESCRIPTORS["opencode-native"], id="fake-native")


class _FakeTransport:
    """Records ``send_prompt`` / ``abort`` calls; optionally raises."""

    def __init__(self, *, send_raises: bool = False, abort_raises: bool = False) -> None:
        self.prompts: list[tuple[str, NativePrompt]] = []
        self.aborted: list[str] = []
        self._send_raises = send_raises
        self._abort_raises = abort_raises

    async def send_prompt(self, session_id: str, prompt: NativePrompt) -> dict[str, Any]:
        if self._send_raises:
            raise RuntimeError("inject boom")
        self.prompts.append((session_id, prompt))
        return {"ok": True}

    async def abort(self, session_id: str) -> bool:
        if self._abort_raises:
            raise RuntimeError("abort boom")
        self.aborted.append(session_id)
        return True


def _build_prompt(content: Any) -> NativePrompt | None:
    return NativePrompt(text=content) if isinstance(content, str) and content else None


def _harness(
    transport: _FakeTransport,
    *,
    session_id: str | None = "ses_1",
    resolver: Any = None,
    supports_enqueue: bool = True,
) -> NativeServerHarness:
    resolve = resolver if resolver is not None else _const_resolver(session_id)
    return NativeServerHarness(
        descriptor=dataclasses.replace(_BASE_DESCRIPTOR, supports_enqueue=supports_enqueue),
        transport=transport,  # type: ignore[arg-type]
        resolve_session_id=resolve,
        build_prompt=_build_prompt,
        boot_poll_attempts=2,
        boot_poll_delay=0.0,
    )


def _const_resolver(session_id: str | None) -> Any:
    async def _resolve() -> str | None:
        return session_id

    return _resolve


async def _drive(harness: NativeServerHarness, content: Any = "hello", config: Any = None) -> list:
    return [
        e async for e in harness.run_turn([{"role": "user", "content": content}], [], "", config)
    ]


# ── capabilities ────────────────────────────────────────────────────────────


def test_capabilities() -> None:
    harness = _harness(_FakeTransport())
    assert harness.supports_streaming() is False
    assert harness.handles_tools_internally() is True
    assert harness.supports_live_message_queue() is True
    assert (
        _harness(_FakeTransport(), supports_enqueue=False).supports_live_message_queue() is False
    )


# ── run_turn ────────────────────────────────────────────────────────────────


async def test_run_turn_injects_and_completes() -> None:
    transport = _FakeTransport()
    events = await _drive(_harness(transport))
    assert [type(e) for e in events] == [TurnComplete]
    assert transport.prompts == [("ses_1", NativePrompt(text="hello"))]


async def test_run_turn_pins_config_model_when_prompt_unset() -> None:
    transport = _FakeTransport()
    await _drive(_harness(transport), config=ExecutorConfig(model="anthropic/claude-opus-4"))
    assert transport.prompts[0][1].model == "anthropic/claude-opus-4"


async def test_run_turn_no_user_input_errors() -> None:
    events = await _drive(_harness(_FakeTransport()), content="")
    assert [type(e) for e in events] == [ExecutorError]
    assert "no user input" in events[0].message


async def test_run_turn_missing_session_errors_after_boot_poll() -> None:
    # Resolver always None → boot-poll exhausts → bridge-missing error.
    events = await _drive(_harness(_FakeTransport(), resolver=_const_resolver(None)))
    assert [type(e) for e in events] == [ExecutorError]
    assert "bridge state is missing" in events[0].message


async def test_run_turn_boot_poll_recovers_session() -> None:
    seq = [None, "ses_late"]

    async def _resolve() -> str | None:
        return seq.pop(0)

    transport = _FakeTransport()
    events = await _drive(_harness(transport, resolver=_resolve))
    assert [type(e) for e in events] == [TurnComplete]
    assert transport.prompts[0][0] == "ses_late"


async def test_run_turn_send_failure_becomes_error_event() -> None:
    events = await _drive(_harness(_FakeTransport(send_raises=True)))
    assert [type(e) for e in events] == [ExecutorError]
    assert "executor error" in events[0].message


# ── interrupt_session ───────────────────────────────────────────────────────


async def test_interrupt_aborts() -> None:
    transport = _FakeTransport()
    assert await _harness(transport).interrupt_session("k") is True
    assert transport.aborted == ["ses_1"]


async def test_interrupt_no_session_returns_false() -> None:
    assert await _harness(_FakeTransport(), session_id=None).interrupt_session("k") is False


async def test_interrupt_swallows_abort_error() -> None:
    assert await _harness(_FakeTransport(abort_raises=True)).interrupt_session("k") is False


# ── enqueue_session_message ─────────────────────────────────────────────────


async def test_enqueue_injects_prompt() -> None:
    transport = _FakeTransport()
    assert await _harness(transport).enqueue_session_message("k", "steer") is True
    assert transport.prompts == [("ses_1", NativePrompt(text="steer"))]


async def test_enqueue_empty_content_returns_false() -> None:
    assert await _harness(_FakeTransport()).enqueue_session_message("k", "") is False


async def test_enqueue_no_session_returns_false() -> None:
    assert (
        await _harness(_FakeTransport(), session_id=None).enqueue_session_message("k", "x")
        is False
    )


async def test_enqueue_swallows_send_error() -> None:
    assert (
        await _harness(_FakeTransport(send_raises=True)).enqueue_session_message("k", "x") is False
    )

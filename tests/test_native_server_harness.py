"""Unit tests for :class:`omnigent.native_server_harness.NativeServerHarness`.

Drives the transport-agnostic base directly over an in-memory fake transport,
covering the run-turn / interrupt / enqueue orchestration (boot-poll, model
pinning, and the error branches) independent of any concrete harness.
"""

from __future__ import annotations

from typing import Any

from omnigent.inner.executor import ExecutorConfig, ExecutorError, TurnComplete
from omnigent.native_server_harness import NativeServerHarness
from omnigent.native_server_transport import NativePrompt


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
        harness_id="fake-native",
        supports_enqueue=supports_enqueue,
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


# ── system_prompt gating (opt-in via _gate_system_prompt) ───────────────────


class _GatingHarness(NativeServerHarness):
    """Test double that opts into per-turn system-prompt delivery."""

    def _gate_system_prompt(self, system_prompt: str) -> str | None:
        return system_prompt or None


def _gating_harness(
    transport: _FakeTransport, *, session_id: str | None = "ses_1"
) -> _GatingHarness:
    return _GatingHarness(
        harness_id="fake-native-gated",
        supports_enqueue=True,
        transport=transport,  # type: ignore[arg-type]
        resolve_session_id=_const_resolver(session_id),
        build_prompt=_build_prompt,
        boot_poll_attempts=2,
        boot_poll_delay=0.0,
    )


async def test_run_turn_discards_system_prompt_by_default() -> None:
    """The base class discards system_prompt unless a subclass opts in."""
    transport = _FakeTransport()
    harness = _harness(transport)
    events = [
        e
        async for e in harness.run_turn(
            [{"role": "user", "content": "hello"}], [], "Some instructions", None
        )
    ]
    assert [type(e) for e in events] == [TurnComplete]
    assert transport.prompts[0][1].system_prompt is None


async def test_run_turn_attaches_gated_system_prompt_when_opted_in() -> None:
    """An opted-in subclass attaches the gated value to every normal turn."""
    transport = _FakeTransport()
    harness = _gating_harness(transport)
    events = [
        e
        async for e in harness.run_turn(
            [{"role": "user", "content": "hello"}], [], "Be concise.", None
        )
    ]
    assert [type(e) for e in events] == [TurnComplete]
    assert transport.prompts[0][1].system_prompt == "Be concise."


async def test_run_turn_neither_case_omits_system_prompt() -> None:
    """A genuinely empty system_prompt omits the field, not a fabricated one."""
    transport = _FakeTransport()
    harness = _gating_harness(transport)
    events = [
        e async for e in harness.run_turn([{"role": "user", "content": "hello"}], [], "", None)
    ]
    assert [type(e) for e in events] == [TurnComplete]
    assert transport.prompts[0][1].system_prompt is None


async def test_enqueue_before_any_normal_turn_omits_system_prompt() -> None:
    """No prior normal turn → no cached value → the field is omitted."""
    transport = _FakeTransport()
    harness = _gating_harness(transport)
    assert await harness.enqueue_session_message("k", "steer") is True
    assert transport.prompts[0][1].system_prompt is None


async def test_enqueue_reuses_last_normal_turns_system_prompt() -> None:
    """A promoted queued message reuses the most recent normal turn's value."""
    transport = _FakeTransport()
    harness = _gating_harness(transport)
    async for _ in harness.run_turn(
        [{"role": "user", "content": "hello"}], [], "Be concise.", None
    ):
        pass

    assert await harness.enqueue_session_message("k", "steer") is True

    assert transport.prompts[1][1].system_prompt == "Be concise."


async def test_later_instruction_free_turn_clears_cached_system_prompt() -> None:
    """A later turn with nothing to say clears a previously populated cache."""
    transport = _FakeTransport()
    harness = _gating_harness(transport)
    async for _ in harness.run_turn(
        [{"role": "user", "content": "hello"}], [], "Be concise.", None
    ):
        pass
    async for _ in harness.run_turn([{"role": "user", "content": "again"}], [], "", None):
        pass

    assert await harness.enqueue_session_message("k", "steer") is True

    assert transport.prompts[-1][1].system_prompt is None


async def test_run_turn_and_enqueue_share_lock_no_lost_update() -> None:
    """Concurrent run_turn + enqueue serialize under the shared inject lock.

    A resolver hook forces a real interleaving opportunity: it yields
    control right after ``run_turn`` has cached ``_last_system_prompt`` but
    *before* it sends — the exact window ``_inject_lock`` must hold across.
    A concurrently-started ``enqueue_session_message`` is given several
    scheduler ticks during that window to attempt (and, without the lock,
    complete) its own send. With the lock held for the whole critical
    section (write + send), enqueue cannot even acquire it until run_turn's
    send has already landed, so sends stay in start order. Without the
    lock, enqueue's short critical section finishes first and its message
    is sent BEFORE run_turn's, even though run_turn started first and
    already held the "current" system prompt. The observable failure is that
    order inversion, not a malformed prompt: plain attribute writes cannot
    tear under asyncio's cooperative scheduling, so a well-formedness check
    holds with or without the lock, so it distinguishes nothing.
    """
    import asyncio

    transport = _FakeTransport()
    run_turn_write_done = asyncio.Event()
    let_run_turn_send = asyncio.Event()
    resolve_calls = 0

    async def resolve_session_id() -> str | None:
        nonlocal resolve_calls
        resolve_calls += 1
        if resolve_calls == 1:
            # This is run_turn's own resolve call, reached AFTER it wrote
            # _last_system_prompt but BEFORE it sends. Yield here so a
            # concurrent enqueue gets a real chance to interleave.
            run_turn_write_done.set()
            await let_run_turn_send.wait()
        return "ses_1"

    harness = _GatingHarness(
        harness_id="fake-native-gated",
        supports_enqueue=True,
        transport=transport,  # type: ignore[arg-type]
        resolve_session_id=resolve_session_id,
        build_prompt=_build_prompt,
        boot_poll_attempts=2,
        boot_poll_delay=0.0,
    )

    async def _turn() -> None:
        async for _ in harness.run_turn(
            [{"role": "user", "content": "hello"}], [], "Be concise.", None
        ):
            pass

    async def _enqueue() -> None:
        await run_turn_write_done.wait()
        await harness.enqueue_session_message("k", "steer")

    turn_task = asyncio.create_task(_turn())
    enqueue_task = asyncio.create_task(_enqueue())

    await run_turn_write_done.wait()
    # Give the enqueue task every opportunity to run as far as it can
    # (attempt lock acquisition, or — unlocked — race all the way through
    # its own send) before run_turn is allowed to proceed to its send.
    for _ in range(20):
        await asyncio.sleep(0)
    let_run_turn_send.set()

    await asyncio.gather(turn_task, enqueue_task)

    # The turn that started FIRST must also have SENT first: enqueue may
    # only run after run_turn's own send has completed, never interleaved
    # into the middle of it.
    assert [p.text for _session_id, p in transport.prompts] == ["hello", "steer"]

"""Unit tests for per-turn context providers (omnigent.runtime.context_providers)."""

from __future__ import annotations

import asyncio
import sys
import threading
import time
import types

import pytest

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runtime.context_providers import (
    ContextProviderInput,
    _last_user_text,
    input_from_turn,
    run_context_providers,
)
from omnigent.spec.types import AgentSpec, FunctionRef


def _spec(refs: list[FunctionRef] | None) -> AgentSpec:
    return AgentSpec(spec_version=1, context_providers=refs)


# Register provider callables in a throwaway module so dotted-path resolution
# (importlib) can find them by string, exactly as a real spec would.
_MOD = types.ModuleType("ctxprov_test_mod")


def _sync_provider(ctx: ContextProviderInput) -> str:
    return f"sync:{ctx.last_user_message}"


async def _async_provider(ctx: ContextProviderInput) -> str:
    return f"async:{ctx.conversation_id}"


def _empty_provider(ctx: ContextProviderInput) -> None:
    return None


def _boom_provider(ctx: ContextProviderInput) -> str:
    raise RuntimeError("boom")


def _factory(prefix: str):
    def provider(ctx: ContextProviderInput) -> str:
        return f"{prefix}:{ctx.last_user_message}"

    return provider


def _slow_factory(prefix: str):
    """Build a provider that takes ~0.3s, to expose serial execution."""

    async def provider(ctx: ContextProviderInput) -> str:
        await asyncio.sleep(0.3)
        return f"{prefix}:{ctx.last_user_message}"

    return provider


def _thread_provider(ctx: ContextProviderInput) -> str:
    """Report the thread it ran on, so the caller can prove it left the loop."""
    return threading.current_thread().name


# Released by the deadline test so the abandoned worker thread exits promptly
# instead of pinning the interpreter until its own sleep expires.
_UNBLOCK = threading.Event()


def _blocking_provider(ctx: ContextProviderInput) -> str:
    """Block the worker thread past any sane deadline, never yielding."""
    _UNBLOCK.wait(30)
    return "never"


async def _slow_async_provider(ctx: ContextProviderInput) -> str:
    await asyncio.sleep(30)
    return "never"


def _refused_provider(ctx: ContextProviderInput) -> str:
    raise OmnigentError("provider refused", code=ErrorCode.INVALID_INPUT)


def _dict_provider(ctx: ContextProviderInput) -> dict[str, int]:
    return {"a": 1}


for _name in (
    "_sync_provider",
    "_async_provider",
    "_empty_provider",
    "_boom_provider",
    "_factory",
    "_slow_factory",
    "_thread_provider",
    "_blocking_provider",
    "_slow_async_provider",
    "_refused_provider",
    "_dict_provider",
):
    setattr(_MOD, _name, globals()[_name])
sys.modules["ctxprov_test_mod"] = _MOD


async def test_no_providers_returns_none() -> None:
    assert await run_context_providers(_spec(None), ContextProviderInput()) is None
    assert await run_context_providers(_spec([]), ContextProviderInput()) is None


async def test_sync_provider() -> None:
    out = await run_context_providers(
        _spec([FunctionRef(path="ctxprov_test_mod._sync_provider")]),
        ContextProviderInput(last_user_message="hi"),
    )
    assert out == "sync:hi"


async def test_async_provider() -> None:
    out = await run_context_providers(
        _spec([FunctionRef(path="ctxprov_test_mod._async_provider")]),
        ContextProviderInput(conversation_id="conv_1"),
    )
    assert out == "async:conv_1"


async def test_factory_with_arguments() -> None:
    out = await run_context_providers(
        _spec([FunctionRef(path="ctxprov_test_mod._factory", arguments={"prefix": "P"})]),
        ContextProviderInput(last_user_message="q"),
    )
    assert out == "P:q"


async def test_errors_and_empties_are_skipped() -> None:
    out = await run_context_providers(
        _spec(
            [
                FunctionRef(path="ctxprov_test_mod._boom_provider"),
                FunctionRef(path="ctxprov_test_mod._empty_provider"),
                FunctionRef(path="ctxprov_test_mod._sync_provider"),
            ]
        ),
        ContextProviderInput(last_user_message="x"),
    )
    assert out == "sync:x"


async def test_multiple_outputs_joined() -> None:
    out = await run_context_providers(
        _spec(
            [
                FunctionRef(path="ctxprov_test_mod._sync_provider"),
                FunctionRef(path="ctxprov_test_mod._async_provider"),
            ]
        ),
        ContextProviderInput(conversation_id="c", last_user_message="m"),
    )
    assert out == "sync:m\n\nasync:c"


async def test_sync_provider_runs_off_the_event_loop() -> None:
    """A sync provider is dispatched to a worker thread.

    Sync providers do network I/O (memory recall is the motivating case);
    calling one inline would stall every other session sharing the runner's
    event loop.
    """
    out = await run_context_providers(
        _spec([FunctionRef(path="ctxprov_test_mod._thread_provider")]),
        ContextProviderInput(),
    )
    assert out != threading.current_thread().name


async def test_providers_run_concurrently_not_serially() -> None:
    """Total latency tracks the slowest provider, not the sum.

    Three providers each sleeping ~0.3s must finish in well under the 0.9s a
    serial loop would take.
    """
    started = time.monotonic()
    out = await run_context_providers(
        _spec(
            [
                FunctionRef(path="ctxprov_test_mod._slow_factory", arguments={"prefix": p})
                for p in "abc"
            ]
        ),
        ContextProviderInput(last_user_message="q"),
    )
    elapsed = time.monotonic() - started
    # Declaration order survives concurrent completion.
    assert out == "a:q\n\nb:q\n\nc:q"
    assert elapsed < 0.9


@pytest.mark.parametrize("path", ["_blocking_provider", "_slow_async_provider"])
async def test_provider_deadline_does_not_hang_the_turn(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """A wedged provider is abandoned at its deadline and the turn continues.

    Covers both hang shapes: a sync provider blocking its worker thread and an
    async provider that never resolves. Without a deadline either one holds
    pre-stream turn setup open indefinitely, with nothing shown to the user.
    """
    monkeypatch.setattr("omnigent.runtime.context_providers._PROVIDER_TIMEOUT_SEC", 0.1)
    _UNBLOCK.clear()
    started = time.monotonic()
    try:
        out = await run_context_providers(
            _spec(
                [
                    FunctionRef(path=f"ctxprov_test_mod.{path}"),
                    FunctionRef(path="ctxprov_test_mod._sync_provider"),
                ]
            ),
            ContextProviderInput(last_user_message="hi"),
        )
    finally:
        _UNBLOCK.set()
    elapsed = time.monotonic() - started
    # The healthy provider's text still lands; the wedged one is dropped.
    assert out == "sync:hi"
    assert elapsed < 5


async def test_refusal_is_not_swallowed() -> None:
    """An OmnigentError from a provider propagates instead of being skipped.

    Refusal is a platform decision, not a provider fault. Swallowing it is what
    made an unvetted provider's failure invisible, so it must never be folded
    into the best-effort path.
    """
    with pytest.raises(OmnigentError, match="provider refused"):
        await run_context_providers(
            _spec([FunctionRef(path="ctxprov_test_mod._refused_provider")]),
            ContextProviderInput(),
        )


async def test_non_string_return_is_dropped() -> None:
    """A provider breaking the ``str | None`` contract contributes nothing.

    Stringifying would splice ``"{'a': 1}"`` into the system prompt unnoticed.
    """
    out = await run_context_providers(
        _spec(
            [
                FunctionRef(path="ctxprov_test_mod._dict_provider"),
                FunctionRef(path="ctxprov_test_mod._sync_provider"),
            ]
        ),
        ContextProviderInput(last_user_message="hi"),
    )
    assert out == "sync:hi"


async def test_provider_failure_is_logged_once_per_session(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A broken provider warns once, not once per turn.

    A typo in a dotted path fails identically forever; a traceback per turn is
    noise that hides the one line an operator needs.
    """
    caplog.set_level("WARNING", logger="omnigent.runtime.context_providers")
    spec = _spec([FunctionRef(path="ctxprov_test_mod._boom_provider")])
    ctx = ContextProviderInput(conversation_id="conv_warn_once")
    for _ in range(3):
        assert await run_context_providers(spec, ctx) is None
    warnings = [r for r in caplog.records if "_boom_provider" in r.getMessage()]
    assert len(warnings) == 1


def test_last_user_text_string() -> None:
    assert _last_user_text({"content": "hello"}) == "hello"


def test_last_user_text_blocks() -> None:
    body = {"content": [{"type": "input_text", "text": "a"}, {"type": "input_text", "text": "b"}]}
    assert _last_user_text(body) == "a\nb"


def test_last_user_text_message_items() -> None:
    body = {
        "content": [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}
        ]
    }
    assert _last_user_text(body) == "hi"


def test_last_user_text_missing() -> None:
    assert _last_user_text({}) is None


def test_input_from_turn() -> None:
    ci = input_from_turn("conv_9", {"content": "yo"})
    assert ci.conversation_id == "conv_9"
    assert ci.last_user_message == "yo"

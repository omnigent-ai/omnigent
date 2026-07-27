"""Unit tests for per-turn context providers (omnigent.runtime.context_providers)."""

from __future__ import annotations

import sys
import types

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


for _name in (
    "_sync_provider",
    "_async_provider",
    "_empty_provider",
    "_boom_provider",
    "_factory",
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

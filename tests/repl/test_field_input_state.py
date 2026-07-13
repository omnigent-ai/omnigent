"""Tests for ``_FieldInputState`` in ``omnigent.repl._repl``.

Covers the future-based field input collection used to prompt
schema fields interactively in the REPL.
"""

from __future__ import annotations

import asyncio
from typing import Any

from omnigent.repl._repl import _FieldInputState, _SessionsChatReplAdapter


def test_not_pending_initially() -> None:
    state = _FieldInputState()
    assert not state.pending
    assert state.field_name is None


def test_begin_creates_pending_future() -> None:
    state = _FieldInputState()
    loop = asyncio.new_event_loop()
    try:
        fut = loop.run_until_complete(_begin(state, "name"))
        assert state.pending
        assert state.field_name == "name"
        assert not fut.done()
    finally:
        loop.close()


def test_resolve_completes_future() -> None:
    state = _FieldInputState()

    async def _run() -> str:
        fut = state.begin("email")
        assert state.pending
        state.resolve("test@example.com")
        return await fut

    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(_run())
        assert result == "test@example.com"
        assert not state.pending
        assert state.field_name is None
    finally:
        loop.close()


def test_resolve_returns_false_when_no_pending() -> None:
    state = _FieldInputState()
    assert not state.resolve("value")


def test_cancel_resolves_with_empty_string() -> None:
    state = _FieldInputState()

    async def _run() -> str:
        fut = state.begin("field")
        state.cancel()
        return await fut

    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(_run())
        assert result == ""
        assert not state.pending
    finally:
        loop.close()


def test_begin_replaces_previous_future() -> None:
    state = _FieldInputState()

    async def _run() -> tuple[str, str]:
        fut1 = state.begin("first")
        fut2 = state.begin("second")
        assert fut1.done()
        assert await fut1 == ""
        state.resolve("val2")
        return await fut1, await fut2

    loop = asyncio.new_event_loop()
    try:
        r1, r2 = loop.run_until_complete(_run())
        assert r1 == ""
        assert r2 == "val2"
    finally:
        loop.close()


async def _begin(state: _FieldInputState, name: str) -> asyncio.Future[str]:
    return state.begin(name)


# --------------------------------------------------------------------------
# Abort contract: cancel() must be distinguishable from an empty submit.
# --------------------------------------------------------------------------


def test_aborted_false_initially() -> None:
    state = _FieldInputState()
    assert not state.aborted


def test_cancel_sets_aborted() -> None:
    state = _FieldInputState()

    async def _run() -> bool:
        state.begin("field")
        state.cancel()
        return state.aborted

    loop = asyncio.new_event_loop()
    try:
        assert loop.run_until_complete(_run())
    finally:
        loop.close()


def test_begin_clears_aborted() -> None:
    """A fresh prompt starts un-aborted even after a prior cancel."""
    state = _FieldInputState()

    async def _run() -> bool:
        state.begin("a")
        state.cancel()
        assert state.aborted
        state.begin("b")
        return state.aborted

    loop = asyncio.new_event_loop()
    try:
        assert not loop.run_until_complete(_run())
    finally:
        loop.close()


# --------------------------------------------------------------------------
# _prompt_schema_fields: parsing, validation, re-prompting, abort, markup.
# --------------------------------------------------------------------------


class _FakeHost:
    """Records rendered output instead of writing to a terminal."""

    def __init__(self) -> None:
        self.outputs: list[Any] = []

    def output(self, renderable: Any) -> None:
        self.outputs.append(renderable)


class _FakeFmt:
    accent = "cyan"
    muted = "grey50"
    warning = "yellow"


def _outputs_text(host: _FakeHost) -> str:
    """Concatenate the ``.plain`` of every rendered Rich ``Text``."""
    return "\n".join(getattr(o, "plain", str(o)) for o in host.outputs)


async def _drive_prompt(
    schema: dict[str, Any],
    answers: list[str],
    *,
    abort_at: int | None = None,
    state: _FieldInputState | None = None,
) -> tuple[dict[str, Any] | None, _FakeHost]:
    """Run ``_prompt_schema_fields`` feeding scripted user answers.

    :param answers: Values fed in order each time a field is pending.
    :param abort_at: 1-based prompt index at which to ``cancel()``
        (simulating Esc) instead of resolving.
    :param state: Optional pre-built state (``None`` builds a fresh one;
        pass an adapter-detached run by leaving the adapter's state unset).
    """
    fis = state if state is not None else _FieldInputState()
    host = _FakeHost()
    adapter = _SessionsChatReplAdapter.__new__(_SessionsChatReplAdapter)
    adapter._field_input_state = fis  # type: ignore[attr-defined]
    adapter._host = host  # type: ignore[attr-defined]
    adapter._fmt = _FakeFmt()  # type: ignore[attr-defined]

    task = asyncio.create_task(adapter._prompt_schema_fields(schema))
    answer_iter = iter(answers)
    prompts = 0
    guard = 0
    while not task.done():
        await asyncio.sleep(0)
        guard += 1
        assert guard < 200, "prompt loop did not converge"
        if fis.pending:
            prompts += 1
            if abort_at is not None and prompts == abort_at:
                fis.cancel()
                continue
            fis.resolve(next(answer_iter, ""))
    result = await task
    return result, host


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_prompt_collects_string_and_integer() -> None:
    schema = {
        "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
        "required": ["name", "age"],
    }
    result, _ = _run(_drive_prompt(schema, ["alice", "42"]))
    assert result == {"name": "alice", "age": 42}


def test_prompt_boolean_parsing() -> None:
    schema = {"properties": {"flag": {"type": "boolean"}}, "required": ["flag"]}
    assert _run(_drive_prompt(schema, ["yes"]))[0] == {"flag": True}
    assert _run(_drive_prompt(schema, ["nope"]))[0] == {"flag": False}


def test_prompt_uses_default_on_empty() -> None:
    schema = {"properties": {"verbose": {"type": "boolean", "default": False}}}
    result, _ = _run(_drive_prompt(schema, [""]))
    assert result == {"verbose": False}


def test_prompt_skips_optional_on_empty() -> None:
    schema = {"properties": {"note": {"type": "string"}}}
    result, _ = _run(_drive_prompt(schema, [""]))
    assert result == {}


def test_prompt_required_empty_reprompts_then_accepts() -> None:
    schema = {"properties": {"name": {"type": "string"}}, "required": ["name"]}
    result, host = _run(_drive_prompt(schema, ["", "bob"]))
    assert result == {"name": "bob"}
    assert "required" in _outputs_text(host)


def test_prompt_invalid_integer_reprompts() -> None:
    schema = {"properties": {"age": {"type": "integer"}}, "required": ["age"]}
    result, host = _run(_drive_prompt(schema, ["abc", "7"]))
    assert result == {"age": 7}
    assert "whole number" in _outputs_text(host)


def test_prompt_enum_rejects_then_accepts() -> None:
    schema = {
        "properties": {"choice": {"type": "string", "enum": ["yes", "no"]}},
        "required": ["choice"],
    }
    result, host = _run(_drive_prompt(schema, ["maybe", "yes"]))
    assert result == {"choice": "yes"}
    assert "choose one of" in _outputs_text(host)


def test_prompt_one_of_const_rejects_then_accepts() -> None:
    schema = {
        "properties": {"mode": {"oneOf": [{"const": "a"}, {"const": "b"}]}},
        "required": ["mode"],
    }
    result, _ = _run(_drive_prompt(schema, ["z", "b"]))
    assert result == {"mode": "b"}


def test_prompt_abort_returns_none() -> None:
    """Esc mid-prompt declines the whole form rather than advancing."""
    schema = {
        "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
        "required": ["a", "b"],
    }
    result, _ = _run(_drive_prompt(schema, [], abort_at=1))
    assert result is None


def test_prompt_markup_in_schema_text_is_safe() -> None:
    """Bracketed server text must not be parsed as Rich markup.

    A description containing an unbalanced closing tag would raise
    ``MarkupError`` under ``Text.from_markup`` and crash the prompt;
    here it must render literally and collection must succeed.
    """
    schema = {
        "properties": {"x": {"type": "string", "description": "use [bold] or [/red]"}},
        "required": ["x"],
    }
    result, host = _run(_drive_prompt(schema, ["v"]))
    assert result == {"x": "v"}
    assert "[/red]" in _outputs_text(host)


def test_prompt_returns_none_when_state_unavailable() -> None:
    schema = {"properties": {"x": {"type": "string"}}}
    adapter = _SessionsChatReplAdapter.__new__(_SessionsChatReplAdapter)
    adapter._field_input_state = None  # type: ignore[attr-defined]
    adapter._host = None  # type: ignore[attr-defined]
    adapter._fmt = None  # type: ignore[attr-defined]
    assert _run(adapter._prompt_schema_fields(schema)) is None

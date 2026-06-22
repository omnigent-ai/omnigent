"""Tests for ``_FieldInputState`` in ``omnigent.repl._repl``.

Covers the future-based field input collection used to prompt
schema fields interactively in the REPL.
"""

from __future__ import annotations

import asyncio

import pytest

from omnigent.repl._repl import _FieldInputState


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

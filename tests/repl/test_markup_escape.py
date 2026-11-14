"""Regression tests for Rich markup escaping of runtime values in ``_repl``.

Runtime strings (model names, tool names/args, user/assistant text,
exception text, tool output) must be rendered literally even when they
contain Rich markup syntax such as ``[/dim]``. Static application markup
is intentionally left untouched.
"""

from __future__ import annotations

import pytest
from rich.console import Console

from omnigent.repl._repl import _render_overview_event, _render_overview_message_event


class _Fmt:
    muted = "dim"
    accent = "cyan"
    error = "red"


def _text(lines) -> str:
    renderables = lines if isinstance(lines, list) else [lines]
    console = Console(width=1000, no_color=True, file=None)
    with console.capture() as cap:
        for renderable in renderables:
            console.print(renderable)
    return cap.get()


PAYLOADS = [
    "[/dim]",
    "[dim]",
    "[bold]hello[/bold]",
    "foo[/red]",
    "[",
    "]",
    "[/]",
    "[unknown-tag]",
]


@pytest.mark.parametrize("payload", PAYLOADS)
def test_overview_message_model_header_escapes_markup(payload: str) -> None:
    item = {"role": "assistant", "model": f"gpt-{payload}", "content": []}
    text = _text(_render_overview_message_event(1, item, _Fmt()))
    assert f"model=gpt-{payload}" in text


@pytest.mark.parametrize("payload", PAYLOADS)
def test_overview_message_text_content_escapes_markup(payload: str) -> None:
    item = {
        "role": "user",
        "content": [{"type": "input_text", "text": f"hello {payload} world"}],
    }
    text = _text(_render_overview_message_event(2, item, _Fmt()))
    assert f"hello {payload} world" in text


def test_model_header_confirmed_bug() -> None:
    model = "gpt-[/dim]-4o"
    item = {"role": "assistant", "model": model, "content": []}
    text = _text(_render_overview_message_event(1, item, _Fmt()))
    assert "model=gpt-[/dim]-4o" in text


def test_multiline_tool_output_with_markup() -> None:
    item = {
        "type": "function_call_output",
        "call_id": "c1",
        "output": "line1\n[/dim]\nline3",
    }
    text = _text(_render_overview_event(1, item, {}, _Fmt()))
    assert "[/dim]" in text


def test_tool_name_with_markup() -> None:
    item = {"type": "function_call", "name": "tool-[/dim]", "arguments": "{}", "call_id": "c1"}
    text = _text(_render_overview_event(1, item, {}, _Fmt()))
    assert "tool-[/dim]" in text


def test_exception_text_with_markup() -> None:
    item = {"role": "assistant", "content": [{"type": "output_text", "text": "Error: [/dim]"}]}
    text = _text(_render_overview_message_event(1, item, _Fmt()))
    assert "Error: [/dim]" in text

"""Wrapped terminal links retain their complete destinations on every row."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from omnigent_ui_sdk.terminal._formatter import (
    FormattedItem,
    StreamingText,
    StreamLive,
    StreamReplace,
)
from omnigent_ui_sdk.terminal._host import TerminalHost
from omnigent_ui_sdk.terminal._linkify import linkify_ansi
from rich.console import Console, Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.style import Style
from rich.table import Table
from rich.text import Text

_URL = "https://example.com/" + "long-path/" * 8 + "?query=complete&other=value#end"


def _table(url: str) -> Table:
    table = Table()
    table.add_column("URL", overflow="fold")
    table.add_row(url)
    return table


def _styled_text(url: str) -> Text:
    text = Text(url, style="bold")
    text.stylize("cyan", 15, 55)
    return text


def _linked_targets(output: str) -> set[str]:
    text = Text.from_ansi(output)
    console = Console()
    return {
        text.get_style_at_offset(console, offset).link
        for offset in range(len(text.plain))
        if text.get_style_at_offset(console, offset).link
    }


def _assert_link(output: str, url: str, *, label: str | None = None) -> None:
    text = Text.from_ansi(output)
    console = Console()
    linked_characters = [
        (character, text.get_style_at_offset(console, offset).link)
        for offset, character in enumerate(text.plain)
        if not character.isspace() and text.get_style_at_offset(console, offset).link
    ]
    assert {target for _, target in linked_characters} == {url}
    assert "".join(character for character, _ in linked_characters) == (label or url)


@pytest.mark.parametrize("width", [24, 40, 80])
@pytest.mark.parametrize(
    "build_renderable",
    [
        pytest.param(Text, id="text"),
        pytest.param(str, id="string"),
        pytest.param(_styled_text, id="styled"),
        pytest.param(lambda url: Panel(Text(url)), id="panel"),
        pytest.param(lambda url: Group(Text(url)), id="group"),
        pytest.param(_table, id="table"),
        pytest.param(Markdown, id="markdown"),
        pytest.param(lambda url: Markdown(f"`{url}`"), id="inline-code"),
        pytest.param(lambda url: StreamLive(Text(url)), id="live"),
        pytest.param(lambda url: StreamReplace(Text(url)), id="replace"),
    ],
)
def test_wrapped_rich_links_keep_complete_target(
    build_renderable: Callable[[str], FormattedItem],
    width: int,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("omnigent_ui_sdk.terminal._host._term_width", lambda: width)
    monkeypatch.setattr("omnigent_ui_sdk.terminal._host._term_height", lambda: 100)
    host = TerminalHost(model_name="test")
    host.output(build_renderable(_URL))
    output = capsys.readouterr().out
    assert output.count("\n") > 1
    _assert_link(output, _URL)


@pytest.mark.parametrize("flush", ["newline", "space", "end"])
def test_streaming_link_split_across_chunks_keeps_complete_target(
    flush: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("omnigent_ui_sdk.terminal._host._term_width", lambda: 40)
    host = TerminalHost(model_name="test")
    for start in range(0, len(_URL), 7):
        host.output(StreamingText(_URL[start : start + 7]))
        assert capsys.readouterr().out == ""
    if flush == "end":
        host.output(Text("done"))
    else:
        host.output(StreamingText("\n" if flush == "newline" else " "))
    _assert_link(capsys.readouterr().out, _URL)


def test_wrapped_explicit_link_keeps_destination_and_does_not_mutate_text(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("omnigent_ui_sdk.terminal._host._term_width", lambda: 40)
    destination = "https://example.org/explicit-target"
    text = Text(_URL, style=Style(link=destination, bold=True))
    original = text.copy()
    host = TerminalHost(model_name="test")
    host.output(text)
    _assert_link(capsys.readouterr().out, destination, label=_URL)
    assert text == original


def test_automatic_link_preserves_explicit_spans_and_does_not_mutate_text(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("omnigent_ui_sdk.terminal._host._term_width", lambda: 40)
    destination = "https://example.org/explicit-target"
    text = Text(_URL)
    text.stylize(Style(link=destination, italic=True), 0, len(_URL))
    original = text.copy()
    TerminalHost(model_name="test").output(text)
    _assert_link(capsys.readouterr().out, destination, label=_URL)
    assert text == original


def test_wrapped_link_excludes_trailing_punctuation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("omnigent_ui_sdk.terminal._host._term_width", lambda: 40)
    text = _styled_text(_URL + ".")
    original = text.copy()
    TerminalHost(model_name="test").output(text)
    _assert_link(capsys.readouterr().out, _URL)
    assert text == original


def test_real_newline_does_not_join_unrelated_text_into_link(
    capsys: pytest.CaptureFixture[str],
) -> None:
    host = TerminalHost(model_name="test")
    host.output(Text("https://example.com/first\nnot-a-url"))
    _assert_link(capsys.readouterr().out, "https://example.com/first")


def test_elided_url_fragment_is_not_linked(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """
    A URL cut by display elision (…) must not be hyperlinked: its
    full destination is unknowable, so a link would open a wrong
    page. The tool-call summary line elides its args to 80 chars
    this way.
    """
    monkeypatch.setattr("omnigent_ui_sdk.terminal._host._term_width", lambda: 120)
    summary = '⏵ sys_os_shell({"command": "echo Visit https://example.com/long-path/long-path/lo…'
    host = TerminalHost(model_name="test")
    host.output(Text(summary))
    output = capsys.readouterr().out
    assert not _linked_targets(output)
    assert "…" in Text.from_ansi(output).plain


def test_complete_url_is_linked_when_elision_cuts_later_text(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The elision guard only fires when … abuts the URL itself."""
    monkeypatch.setattr("omnigent_ui_sdk.terminal._host._term_width", lambda: 120)
    host = TerminalHost(model_name="test")
    host.output(Text("Visit https://example.com/docs before the cut he…"))
    _assert_link(capsys.readouterr().out, "https://example.com/docs")


def test_linkify_ansi_skips_elided_fragment_but_links_complete_urls() -> None:
    """The raw-ANSI fallback applies the same elision rule."""
    out = linkify_ansi("see https://example.com/long-path/lo… and https://example.org/ok")
    assert "\x1b]8;;https://example.org/ok\x1b\\" in out
    assert "\x1b]8;;https://example.com" not in out
    assert "https://example.com/long-path/lo…" in out

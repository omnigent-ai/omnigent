"""The REPL composer tints a slash-command token as it is typed.

Until Enter is pressed, ``/code-review`` and ``send this to the agent`` render
identically in the terminal composer, so there is no signal that the line is
about to be dispatched as a command rather than sent to the model. The web
composer already tints the token via its highlight overlay
(``splitSlashCommand`` + ``text-brand-accent`` in ``ChatPage.tsx``); these
tests pin the terminal to the same treatment. Each surface uses its own
palette, so what matches is the treatment, not the hex.
"""

from __future__ import annotations

import pytest
from omnigent_ui_sdk.terminal import RichBlockFormatter
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import StyleAndTextTuples

from omnigent.repl._repl import (
    _BANG_INPUT_STYLE,
    _SLASH_COMMAND_STYLE,
    _ComposerLexer,
    _slash_command_token,
)


def _fragments(text: str, lineno: int = 0) -> StyleAndTextTuples:
    """Lex *text* and return line *lineno*'s ``(style, text)`` fragments."""
    return list(_ComposerLexer().lex_document(Document(text))(lineno))


def _spans(text: str, lineno: int = 0) -> list[tuple[str, str]]:
    """The fragments that carry text, so assertions pin styling not padding.

    An empty fragment is an artefact of where the token happens to end; only
    ``test_fragments_reproduce_the_line_exactly`` should care about those.
    """
    return [(style, part) for style, part in _fragments(text, lineno) if part]


def test_slash_style_is_the_terminal_accent() -> None:
    """The tint is the accent the rest of the REPL already paints with.

    The literal mirrors the ``_BANG_GREEN`` precedent in the same module
    rather than reaching into the formatter at import time; this pins the two
    together so a re-themed accent fails here instead of drifting.
    """
    assert f"fg:{RichBlockFormatter().accent}" == _SLASH_COMMAND_STYLE


@pytest.mark.parametrize(
    ("text", "token"),
    [
        ("/code-review", "/code-review"),  # a host-scope skill command
        ("/help", "/help"),  # a built-in
        ("  /help", "/help"),  # the dispatcher splits, so indent is tolerated
        # A Claude plugin's skill, namespaced by its plugin. These now reach
        # the REPL as well as the web menu, so the tint has to accept the ":".
        ("/superpowers:using-superpowers", "/superpowers:using-superpowers"),
        ("/", "/"),  # bare "/" still dispatches (and reports unknown)
        ("/Users/me/shot.png", ""),  # a pasted path is a message, not a command
        ("//", ""),  # more separators — not a command token
        ("tell me about /help", ""),  # only the first token can be a command
        ("", ""),  # empty composer
    ],
)
def test_slash_command_token_matches_the_dispatcher(text: str, token: str) -> None:
    """The highlight and the dispatch decision come from one predicate."""
    assert _slash_command_token(text) == token


def test_command_token_is_tinted_and_args_are_not() -> None:
    """The token carries the accent; the argument stays in the default color.

    An argument is frequently a path or URL, which reads badly when it is
    swept into the command's color.
    """
    assert _spans("/code-review is this diff correct?") == [
        (_SLASH_COMMAND_STYLE, "/code-review"),
        ("", " is this diff correct?"),
    ]


def test_indentation_is_not_part_of_the_token() -> None:
    """Only the token is tinted, not the whitespace the dispatcher skips.

    Invisible today, since the accent sets a foreground and nothing else, but
    the span should say what it means: an underline or reverse accent later
    would otherwise trail off to the left of the ``/``.
    """
    assert _spans("   /help me") == [
        ("", "   "),
        (_SLASH_COMMAND_STYLE, "/help"),
        ("", " me"),
    ]


def test_a_namespaced_plugin_skill_is_tinted_whole() -> None:
    """The ``:`` is inside the command name, not a separator before an argument.

    Claude registers a plugin's skill as ``<plugin>:<skill>``, so tinting up to
    the colon would split one command into two coloured halves.
    """
    assert _spans("/superpowers:using-superpowers go") == [
        (_SLASH_COMMAND_STYLE, "/superpowers:using-superpowers"),
        ("", " go"),
    ]


def test_a_pasted_path_stays_unstyled() -> None:
    """``/Users/...`` is sent to the agent, so tinting it would be a lie."""
    assert _spans("/Users/me/shot.png") == [("", "/Users/me/shot.png")]


def test_bang_still_wins_over_the_slash_tint() -> None:
    """``!/usr/bin/env`` runs in the shell — it is green, not accent, in full."""
    assert _spans("!/usr/bin/env") == [(_BANG_INPUT_STYLE, "!/usr/bin/env")]


def test_only_the_first_line_carries_the_tint() -> None:
    """A pasted second line is argument text, not a second command."""
    assert _spans("/code-review\n/help", 0) == [(_SLASH_COMMAND_STYLE, "/code-review")]
    assert _spans("/code-review\n/help", 1) == [("", "/help")]


def test_a_leading_blank_line_does_not_lose_the_tint() -> None:
    """A paste can open with a newline, and the dispatcher looks past it.

    ``str.split`` treats the newline as whitespace, so ``"\\n/help"`` is
    dispatched as ``/help``. The tint has to follow the token onto the line it
    is actually on, or the composer stays silent about a routing that happens.
    """
    assert _spans("\n/help me", 0) == []
    assert _spans("\n/help me", 1) == [(_SLASH_COMMAND_STYLE, "/help"), ("", " me")]


def test_an_unregistered_command_is_still_tinted() -> None:
    """The tint means "the dispatcher takes this", not "this command exists".

    ``/does-not-exist`` is intercepted and answered with ``Unknown command``;
    it never reaches the model, so painting it as a message would be the lie.
    Shape-based, like the web composer.
    """
    assert _spans("/does-not-exist") == [(_SLASH_COMMAND_STYLE, "/does-not-exist")]


@pytest.mark.parametrize(
    "text",
    [
        "/code-review some args",
        "  /help",
        "plain message",
        "",
        "/Users/me/x.png",
        "\n/help",
        "/日本語 テスト",
    ],
)
def test_fragments_reproduce_the_line_exactly(text: str) -> None:
    """A lexer must never drop or duplicate a glyph — the composer shows this.

    The token is a character-exact prefix of the line, so a wide or combining
    character can only ever sit wholly inside one fragment; prompt_toolkit
    measures display width after this split.
    """
    lexer = _ComposerLexer().lex_document(Document(text))
    rejoined = "".join(
        fragment[1] for lineno in range(len(text.split("\n"))) for fragment in lexer(lineno)
    )
    assert rejoined == text.replace("\n", "")

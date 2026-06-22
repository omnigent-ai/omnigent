"""
Regression tests for the overlay-builder multi-line content
contract documented on :class:`Overlay.builder`.

Bug from kasey_uhlenhuth's report:

    Overlay builder must return Rich
    ``Group(*(Text.from_markup(line) for line in lines))``,
    NOT ``Text.from_markup("\\n".join(lines))`` — the latter
    renders raw markup tags as text. Not documented anywhere;
    only discoverable by reading the Ctrl+O implementation.

The fix is two-fold:

1. The ``Overlay.builder`` docstring now explicitly recommends
   the ``Group`` pattern with an inline example, so future
   builder authors don't have to spelunk the reference
   implementation in :mod:`omnigent.repl._repl`.
2. These tests pin the recommended pattern: a builder returning
   ``Group(*(Text.from_markup(line) for line in lines))`` is
   rendered correctly by the host's overlay-content pipeline,
   each line's markup parsed into ANSI styles with no literal
   tag fragments leaking through.

What breaks if these fail:

- The overlay-content pipeline regresses to a state where a
  Group of styled :class:`Text` rows no longer renders cleanly
  (e.g. ``Console.print`` stops walking Group children with
  markup parsing applied, dropping their style spans).
- The Overlay docstring's recommended example is removed or
  rewritten in a way that doesn't preserve the idiom shape —
  re-introducing the kasey discoverability gap.
"""

from __future__ import annotations

import asyncio
import io
import re

from omnigent_ui_sdk import Overlay, OverlayTarget, TerminalHost
from rich.console import Console, Group
from rich.text import Text


def _render_via_pipeline(renderable: object, *, width: int = 60) -> str:
    """
    Mirror what :meth:`TerminalHost._render_overlay_content`
    does to a builder's return value: print through a temporary
    truecolor :class:`Console` at a fixed pane width and return
    the resulting ANSI string.

    Avoids spinning up an actual ``TerminalHost`` (which needs
    a real TTY) — the builder contract is about what
    :class:`Console.print` does to the renderable, so testing
    that step in isolation is the right layer.

    :param renderable: The object the builder would return —
        a :class:`str`, :class:`Text`, :class:`Group`, etc.
    :param width: Pane width to render at, e.g. ``60``. Wide
        enough that the test inputs don't soft-wrap.
    :returns: ANSI-coloured string, the same shape the host
        would line-split before handing to prompt-toolkit.
    """
    buf = io.StringIO()
    console = Console(
        file=buf,
        force_terminal=True,
        width=width,
        highlight=False,
        color_system="truecolor",
    )
    console.print(renderable)
    return buf.getvalue()


def _strip_ansi(text: str) -> str:
    """
    Drop CSI escape sequences from *text* so assertions can
    target the visible characters only.

    Lets us check whether literal ``[bold]``-style markup tags
    leaked through to the output (the kasey symptom) without
    coupling to the exact ANSI byte sequences Rich emits.

    :param text: ANSI-decorated string from
        :func:`_render_via_pipeline`.
    :returns: The same text with CSI sequences removed.
    """
    # CSI sequences: ESC + ``[`` + parameter bytes + final byte.
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)


def test_recommended_group_pattern_renders_each_line_styled() -> None:
    """
    The documented multi-line pattern — ``Group`` of
    ``Text.from_markup`` per line — produces ANSI output where
    each line's markup is parsed into style escapes and NO
    literal markup tags remain in the visible text.

    What breaks if this fails:
      - ``Console.print`` stops walking ``Group`` children with
        markup parsing applied (would drop the style spans).
      - A future refactor of ``Group`` rendering decides to
        join children with ``\\n`` before parsing, re-introducing
        the kasey footgun.
    """
    lines = [
        "[bold]Title[/bold]: example",
        "  [dim]status[/dim]: ok",
        "  [red]error[/red]: none",
    ]
    rendered = _render_via_pipeline(
        Group(*(Text.from_markup(line) for line in lines)),
    )

    visible = _strip_ansi(rendered)
    # No raw markup leaked through. ``[bold]`` etc. should be
    # gone — replaced by ANSI style escapes that survive in
    # ``rendered`` but not in ``visible``.
    assert "[bold]" not in visible, (
        f"Literal '[bold]' tag in visible output — markup parsing did not run. visible={visible!r}"
    )
    assert "[/bold]" not in visible
    assert "[dim]" not in visible
    assert "[red]" not in visible

    # Each line's payload survives. Catches a regression where
    # markup parsing accidentally consumes the surrounding text.
    assert "Title: example" in visible
    assert "status: ok" in visible
    assert "error: none" in visible

    # Line count: Group preserves one row per child. Catches a
    # regression where Group rendering collapses children onto
    # one line.
    nonempty_lines = [ln for ln in visible.split("\n") if ln.strip()]
    assert len(nonempty_lines) == 3, (
        f"Expected 3 visible lines (one per Group child); got "
        f"{len(nonempty_lines)}: {nonempty_lines!r}"
    )

    # Style escapes ARE present in the raw rendered output —
    # confirming markup parsing actually ran. If we see ``[1m``
    # for bold, ``[2m`` for dim, ``[31m`` for red, the parser
    # did its job.
    assert "\x1b[1m" in rendered, "bold style escape missing"
    assert "\x1b[2m" in rendered, "dim style escape missing"
    assert "\x1b[31m" in rendered, "red style escape missing"


def test_overlay_builder_contract_documented() -> None:
    """
    The :class:`Overlay` class docstring documents the
    recommended multi-line pattern. Pins the doc fix from
    kasey's bug report — a future docstring rewrite that drops
    the example re-introduces the discoverability gap.

    Asserts the actual idiom shape is present, not just that
    the words ``Group`` and ``from_markup`` appear somewhere.
    A bare keyword check would pass on any docstring rewrite
    that mentions either word, even if the example pattern
    were quietly deleted; the regex below pins the specific
    invocation shape callers are supposed to copy.
    """
    doc = Overlay.__doc__ or ""
    # Match ``Group(*(...from_markup...))`` — the canonical
    # idiom. ``re.DOTALL`` lets the example span the docstring's
    # line breaks. If the docstring switches to a different
    # pattern (or the example block is removed entirely), this
    # fails — which is the desired behaviour: someone deleting
    # the example must update this test consciously, not by
    # accident.
    pattern = re.compile(r"Group\s*\(\s*\*\s*\(.*?\bfrom_markup\b", re.DOTALL)
    assert pattern.search(doc) is not None, (
        f"Overlay docstring lost its Group(*(...from_markup...)) "
        f"example. Multi-line builder authors will fall back to the "
        f"joined-Text antipattern kasey reported. Docstring excerpt:"
        f"\n{doc[:1000]}"
    )


def test_recommended_pattern_via_real_overlay_object() -> None:
    """
    Full smoke: register an :class:`Overlay` whose builder uses
    the recommended pattern, invoke its builder coroutine
    directly, and pipe the result through the same pipeline
    the host uses. Proves the documented pattern is callable
    end-to-end through the public API.
    """
    lines = [
        "[bold]One[/bold]",
        "[red]Two[/red]",
    ]

    async def _builder(target: OverlayTarget | None) -> Group:
        return Group(*(Text.from_markup(line) for line in lines))

    overlay = Overlay(trigger="c-x", builder=_builder, title="Test")
    # Builder is async; run it through the same pipeline the
    # host's _render_overlay_content uses.
    result = asyncio.run(overlay.builder(None))
    rendered = _render_via_pipeline(result)
    visible = _strip_ansi(rendered)

    # Both lines' content survived; tags did not leak.
    assert "One" in visible
    assert "Two" in visible
    assert "[bold]" not in visible
    assert "[red]" not in visible

    # Sanity: TerminalHost is importable + Overlay is usable
    # alongside it. Catches accidental SDK API breakage where
    # the public surface stops accepting the documented shape.
    assert TerminalHost is not None

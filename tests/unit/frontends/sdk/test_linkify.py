"""
Tests for the OSC 8 URL-linkification helper used by
:class:`omnigent_ui_sdk.terminal.TerminalHost`.

Covers:

- bare URLs in plain text get wrapped
- already-linkified content is preserved verbatim (idempotent)
- trailing punctuation is excluded from the link region
- URLs inside parentheses / quotes / angle brackets terminate
  cleanly before the delimiter
- composes with surrounding ANSI SGR styling (Rich-rendered
  content is the typical input)
- empty / no-URL strings pass through unchanged

Why this gets a focused unit test rather than only being
covered by the higher-level ``TerminalHost`` tests: the regex
and the trailing-punctuation peel are subtle, and a regression
silently downgrades clickable-URL rendering for every agent
that emits links. A dedicated test file makes the contract
unambiguous and catches a subtle regex break in one shot.
"""

from __future__ import annotations

from omnigent_ui_sdk.terminal._linkify import linkify_ansi

# OSC 8 escape components — duplicated here from the module
# (deliberately) so the test fails loudly if the module's
# escape format drifts. The contract this test pins is the
# byte-level wire format, so spelling it out in the test is
# load-bearing.
_OSC_OPEN = "\x1b]8;;"
_OSC_CLOSE = "\x1b\\"


def _wrap(url: str) -> str:
    """
    Build the expected OSC 8 byte sequence for ``url``.

    Helper to keep individual assertions readable; the byte
    soup is the same per-URL pattern repeated.

    :param url: The URL to wrap, e.g. ``"https://example.com"``.
    :returns: The OSC 8 byte sequence the linkifier should emit
        for this URL.
    """
    return f"{_OSC_OPEN}{url}{_OSC_CLOSE}{url}{_OSC_OPEN}{_OSC_CLOSE}"


def test_linkify_wraps_bare_https_url() -> None:
    """
    A plain HTTPS URL in prose gets wrapped in OSC 8 escapes.

    Regression target: the URL regex stops matching ``https://``
    schemes, or the OSC 8 envelope shape changes.
    """
    out = linkify_ansi("See https://example.com here")
    assert out == f"See {_wrap('https://example.com')} here"


def test_linkify_wraps_bare_http_url() -> None:
    """
    HTTP URLs are linkified too (not HTTPS-only). Some legacy
    services still serve HTTP; the agent's tool output may
    surface them.
    """
    out = linkify_ansi("Open http://localhost:8080/foo and click")
    assert out == f"Open {_wrap('http://localhost:8080/foo')} and click"


def test_linkify_strips_trailing_period() -> None:
    """
    Sentence-final punctuation isn't part of the URL.

    "Visit https://example.com." should produce a link over
    ``https://example.com`` followed by a literal period, not
    a link over ``https://example.com.`` (which would 404).
    """
    out = linkify_ansi("Visit https://example.com.")
    assert out == f"Visit {_wrap('https://example.com')}."


def test_linkify_strips_trailing_comma_between_urls() -> None:
    """
    A list of URLs separated by ``, `` produces independent
    links — neither URL absorbs the comma.
    """
    out = linkify_ansi("URLs: https://a.com, https://b.com")
    assert out == f"URLs: {_wrap('https://a.com')}, {_wrap('https://b.com')}"


def test_linkify_stops_at_closing_paren() -> None:
    """
    A URL inside parentheses ends at the close-paren —
    "(see https://example.com)" wraps the URL but not the
    closing ``)``.

    Trade-off: legitimate URLs containing parens (e.g.
    Wikipedia disambiguation pages) lose their close-paren
    in the linked region. Acceptable: the sentential-paren
    case is overwhelmingly more common in agent text than
    paren-bearing URLs.
    """
    out = linkify_ansi("(see https://example.com)")
    assert out == f"(see {_wrap('https://example.com')})"


def test_linkify_handles_query_string() -> None:
    """
    Query strings (``?key=val&other=val``) are part of the URL
    and stay inside the link.
    """
    url = "https://example.com/path?q=1&r=2"
    out = linkify_ansi(f"go to {url}")
    assert out == f"go to {_wrap(url)}"


def test_linkify_is_idempotent_on_existing_osc_8_block() -> None:
    """
    A URL already wrapped in OSC 8 escapes is left UNTOUCHED —
    we don't re-wrap and produce malformed nested escapes.

    Regression target: the OSC 8 block recognizer breaks (e.g.
    closer pattern stops matching) so the regex falls through
    to the bare-URL branch and double-wraps. The double-wrap
    failure mode is visually catastrophic in supporting
    terminals (the link target becomes garbled escape bytes)
    so this is the main idempotence assertion.
    """
    already = f"see {_wrap('https://x.com')} end"
    out = linkify_ansi(already)
    assert out == already


def test_linkify_handles_mixed_already_and_bare() -> None:
    """
    Strings containing both already-linkified URLs and bare
    URLs only wrap the bare ones; the existing OSC 8 blocks
    pass through verbatim.
    """
    bare = "https://a.com"
    already = _wrap("https://b.com")
    bare2 = "https://c.com"
    inp = f"ab {bare} cd {already} ef {bare2}"
    out = linkify_ansi(inp)
    assert out == f"ab {_wrap(bare)} cd {already} ef {_wrap(bare2)}"


def test_linkify_composes_with_sgr_styling() -> None:
    """
    Rich-rendered content typically contains SGR color codes
    around or near URLs. The linkifier's OSC 8 wrapper sits
    OUTSIDE those — the URL text stays styled, and the wrapper
    bytes don't break the SGR sequence.

    Concretely: a Rich-rendered ``[bold]See [/bold]https://x.com``
    yields ``\\x1b[1mSee \\x1b[0mhttps://x.com``; after
    linkification the URL gets wrapped but the bold prefix is
    untouched.
    """
    inp = "\x1b[1mSee \x1b[0mhttps://example.com"
    out = linkify_ansi(inp)
    assert out == f"\x1b[1mSee \x1b[0m{_wrap('https://example.com')}"


def test_linkify_no_urls_passes_through_unchanged() -> None:
    """
    Strings without any URLs are returned byte-identical. No
    mutation, no spurious bytes inserted.
    """
    inp = "This text has no links, just words. Period."
    assert linkify_ansi(inp) == inp


def test_linkify_empty_string() -> None:
    """Empty input → empty output. Trivial but guards against
    edge-case regressions in the regex/sub fast path."""
    assert linkify_ansi("") == ""


def test_linkify_url_with_multiple_trailing_punct() -> None:
    """
    Multiple trailing punctuation chars (``!?``, ``...``) all
    stay outside the link.

    Edge case worth pinning: URL endings like ``foo.com!?`` —
    the linker peels ALL trailing-punct chars in a loop, not
    just one.
    """
    out = linkify_ansi("Wow https://example.com!?")
    assert out == f"Wow {_wrap('https://example.com')}!?"


def test_linkify_url_at_end_of_string() -> None:
    """A URL that's the last thing in the string — no trailing
    text, no terminator. Common for tool output that prints a
    URL on its own line."""
    out = linkify_ansi("https://example.com")
    assert out == _wrap("https://example.com")


def test_linkify_skips_naked_domains() -> None:
    """
    Naked domain references like ``example.com`` (no scheme)
    are NOT linkified. We require ``http(s)://`` to avoid
    false positives on things like ``agent.py`` or
    version strings (``1.2.3``).
    """
    inp = "see example.com or agent.py:42"
    assert linkify_ansi(inp) == inp


def test_linkify_idempotent_on_rich_osc_8_with_sgr_display_text() -> None:
    """
    Rich's Markdown renderer wraps ``[text](url)`` links in OSC 8
    with SGR color/underline codes INSIDE the display text, e.g.::

        \\x1b]8;id=123;https://x.com\\x1b\\\\
        \\x1b[4;34mtext\\x1b[0m
        \\x1b]8;;\\x1b\\\\

    The display region between OSC 8 opener and closer contains
    ``\\x1b[...m`` SGR sequences. ``linkify_ansi`` must recognize
    this as a pre-existing OSC 8 block and leave it untouched.

    Regression target (2026-05-13): the ``_OSC_8_BLOCK`` regex
    used ``[^\\x1b]*`` for display text, which rejected any
    ``\\x1b`` — so Rich's styled blocks went unrecognized, the
    bare-URL branch fired, and the URL got double-wrapped,
    producing corrupted escape sequences that broke clickable
    links in every terminal.
    """
    # Simulate Rich's actual output: OSC 8 with id param, SGR-styled text.
    rich_osc_8 = (
        "\x1b]8;id=12345;https://example.com/docs\x1b\\\x1b[4;34mthe docs\x1b[0m\x1b]8;;\x1b\\"
    )
    inp = f"See {rich_osc_8} for more"
    out = linkify_ansi(inp)
    assert out == inp, (
        f"Rich's OSC 8 block with SGR display text was mutated by "
        f"linkify_ansi (double-wrap regression). "
        f"Input: {inp!r}, output: {out!r}"
    )


def test_linkify_mixed_rich_osc_8_and_bare_url() -> None:
    """
    When a string contains both a Rich-emitted OSC 8 block (with
    SGR display text) AND a bare URL, ``linkify_ansi`` must wrap
    the bare URL while leaving the Rich block untouched.
    """
    rich_block = "\x1b]8;id=1;https://example.com\x1b\\\x1b[4;34mlinked text\x1b[0m\x1b]8;;\x1b\\"
    inp = f"See {rich_block} and also https://bare.example.com here"
    out = linkify_ansi(inp)
    # Rich block preserved verbatim.
    assert rich_block in out, f"Rich OSC 8 block was mutated. Output: {out!r}"
    # Bare URL wrapped.
    assert _wrap("https://bare.example.com") in out, f"Bare URL was not wrapped. Output: {out!r}"

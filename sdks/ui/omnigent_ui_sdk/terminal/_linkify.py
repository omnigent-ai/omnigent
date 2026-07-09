"""
Wrap ``http://`` / ``https://`` URLs in OSC 8 hyperlink escapes
so terminals that support shell-integration semantics (iTerm2,
Ghostty, kitty, Alacritty, WezTerm, modern Terminal.app, Windows
Terminal) render them as ⌘-clickable links.

OSC 8 format:

    \\x1b]8;;<url>\\x1b\\<display_text>\\x1b]8;;\\x1b\\

Terminals that don't understand OSC 8 just see the display text;
the escape bytes are silently consumed without disrupting the
visible output. This is the standard "graceful degradation"
behavior every modern terminal honors per the OSC 8 spec.

Why this lives here, not as a Rich highlighter or markup
transformation: Rich's ``Console`` only auto-applies a
``highlighter`` to plain-string args of ``Console.print`` —
pre-built ``Text``/``Panel``/``Group`` renderables (which is
what ``TerminalHost.output`` mostly receives) bypass the
highlighter pass entirely. Rather than walk every Rich
renderable type to find ``Text`` leaves and stylize them,
we post-process the rendered ANSI string. One byte-level pass
applies uniformly to every render path (streaming text,
non-streaming Rich, mid-paragraph rewrites) without entangling
us with Rich's renderable internals.
"""

from __future__ import annotations

import re

# Match ``http://`` / ``https://`` URLs, stopping at:
#   - whitespace
#   - closing brackets / quotes / angle that commonly delimit URLs
#     in prose ("(see https://example.com)", "<https://...>",
#     '"https://..."').
#   - ANY C0 control byte or DEL (``\x00-\x1f``, ``\x7f``) — critically the
#     ESC (``\x1b``) that introduces an SGR/OSC escape. Rich renders an
#     autolinked URL as ``\x1b[..m<url>\x1b[0m`` (styled + reset); without
#     excluding ESC, the URL class swallows the trailing ``\x1b[0m`` reset
#     and embeds it INSIDE the OSC 8 link target built below
#     (``\x1b]8;;<url>\x1b[0m\x1b\\``), a malformed hyperlink that terminals
#     render by leaking the reset's tail ``0m`` as visible text before the
#     URL. Real URLs never contain raw control bytes (they are
#     percent-encoded), so excluding them is always safe.
# Trailing punctuation (``.,;:!?``) is stripped in the substitution
# callback rather than excluded by the regex — e.g. "Visit
# https://example.com." should fire OSC 8 over the URL but leave
# the period after the close-OSC8.
_URL = r"https?://[^\s\)\]\>\"'<\x00-\x1f\x7f]+"

# Match a complete pre-existing OSC 8 hyperlink block so we don't
# re-wrap a URL that was already linkified (some agent paths emit
# OSC 8 themselves; the legacy inner Session has been known to).
# Pattern captures: opener ``\x1b]8;<params>;<url>(\x07|\x1b\)``,
# the display text, then the closer ``\x1b]8;;(\x07|\x1b\)``.
#
# The display text between opener and closer may contain SGR
# escape sequences (``\x1b[...m`` for bold, underline, color)
# emitted by Rich's Markdown renderer — e.g. Rich wraps
# ``[text](url)`` in ``\x1b[4;34m...\x1b[0m`` (underlined blue).
# The previous regex used ``[^\x1b]*`` which rejected any ``\x1b``
# in display text, causing Rich's styled OSC 8 blocks to go
# unrecognized and get double-wrapped. The fix: allow ``\x1b``
# followed by ``[`` (SGR introducer) inside display text, while
# still stopping at ``\x1b]`` (OSC introducer, signals the closer).
# Concretely: ``(?:[^\x1b]|\x1b(?!\]))*`` matches any character
# that is not ``\x1b``, or ``\x1b`` when NOT followed by ``]``.
_OSC_8_BLOCK = (
    r"\x1b\]8;[^\x07\x1b]*(?:\x07|\x1b\\)"  # opener (params + URL)
    r"(?:[^\x1b]|\x1b(?!\]))*"  # display text (allows SGR escapes)
    r"\x1b\]8;;(?:\x07|\x1b\\)"  # closer
)

# Combined alternation — order matters: match an existing OSC 8
# block FIRST so we skip over its URL without re-processing.
_LINKIFY_RE = re.compile(f"({_OSC_8_BLOCK})|({_URL})")

# Trailing punctuation that's almost never part of a URL — strip
# back to the URL proper, then put the punct AFTER the OSC 8 close.
_TRAILING_PUNCT = ".,;:!?"

# OSC 8 escape components.
_OSC_OPEN = "\x1b]8;;"
_OSC_CLOSE = "\x1b\\"


def linkify_ansi(text: str) -> str:
    """
    Wrap ``http(s)://`` URLs in OSC 8 hyperlink escapes.

    Idempotent: URLs already inside an OSC 8 block are left
    untouched. Trailing punctuation (``.,;:!?``) is excluded
    from the linked region so "Visit https://example.com."
    renders as ``[https://example.com].`` (period follows the
    link, not part of it).

    Safe to apply to any string the terminal might display —
    plain text, Rich-rendered ANSI, or partially-linkified
    strings. Composes with arbitrary SGR styling: the OSC 8
    wrapper sits outside the Rich-emitted color codes.

    :param text: ANSI text bound for the terminal,
        e.g. ``"See https://example.com here"`` or a
        Rich-rendered Panel containing URLs in a tool result.
    :returns: The same text with HTTP(S) URLs wrapped in OSC 8
        hyperlink escapes. Unsupported terminals silently
        ignore the wrappers.
    """

    def _replace(match: re.Match[str]) -> str:
        # Group 1: existing OSC 8 block — leave verbatim.
        if match.group(1):
            return match.group(1)
        # Group 2: bare URL — wrap, peeling off trailing punct.
        # The URL is guaranteed non-empty (regex starts with
        # ``https?://``), and the prefix chars are not in
        # ``_TRAILING_PUNCT``, so the loop can't reduce to empty.
        # An IndexError here would mean the regex changed in a
        # way that no longer guarantees the prefix — fail loud.
        url = match.group(2)
        trailing = ""
        while url[-1] in _TRAILING_PUNCT:
            trailing = url[-1] + trailing
            url = url[:-1]
        return f"{_OSC_OPEN}{url}{_OSC_CLOSE}{url}{_OSC_OPEN}{_OSC_CLOSE}{trailing}"

    return _LINKIFY_RE.sub(_replace, text)

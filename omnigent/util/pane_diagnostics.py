"""Harness-agnostic diagnostics for a TUI pane that never became usable.

Every native harness has the same failure: a readiness gate polls
``capture-pane`` for the CLI's input box, the box never appears, and the turn
times out. The pane text is the only evidence of why, and each harness can
recognize a handful of screens it knows about — but the interesting failures are
the ones nobody enumerated yet, and those collapse into "unknown".

Two measurements make an unrecognized screen diagnosable without anyone adding
a marker for it first:

:func:`pane_shape`
    Structural facts that hold for any CLI: was a TUI even drawn, is the screen
    waiting on input, is there error text, is a URL on display. Enough to tell
    "the program never started" from "it is asking the person something".

:func:`pane_fingerprint`
    A stable short hash of the normalized tail. The same screen fingerprints
    identically across machines and users, so grouping failures by it surfaces
    the top unrecognized screens by volume — the enumeration falls out of the
    data instead of being guessed in advance. It is a hash, so grouping needs
    none of the pane text (which carries the person's paths) exported anywhere.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

# Lines of pane tail that feed the fingerprint. Enough to identify a screen,
# few enough that scrollback above it does not perturb the hash.
_FINGERPRINT_TAIL_LINES = 8
_FINGERPRINT_HEX_CHARS = 8

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][A-B0-9]")
# Box-drawing and block glyphs. Stripped for the fingerprint (a repaint can
# change frame width without changing the screen) but counted for the shape.
_BOX_GLYPHS = "─│┌┐└┘├┤┬┴┼━┃╭╮╯╰╱╲═║╔╗╚╝▀▄█▌▐░▒▓"
# Volatile substrings replaced before hashing, most specific first. Without
# these the same screen hashes differently for every user and every run.
_FINGERPRINT_SCRUBBERS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}"), "<email>"),
    (re.compile(r"\b[a-z][a-z0-9+.-]*://\S+"), "<url>"),
    (re.compile(r"\b[a-z]:\\[^\s\"']*"), "<path>"),
    (re.compile(r"(?:/(?:users|home)/[^\s\"':]+)"), "<path>"),
    (re.compile(r"(?:~?/[\w.-]+){2,}/?"), "<path>"),
    (re.compile(r"\b[0-9a-f]{6,}\b"), "<id>"),
    (re.compile(r"\d+"), "<n>"),
)

_AWAITING_INPUT_MARKERS = (
    "[y/n]",
    "(y/n)",
    "press enter",
    "enter to confirm",
    "enter to continue",
    "esc to cancel",
)
_ERROR_MARKERS = ("traceback", "exception", "error:", "error ", "fatal", "panic:")


@dataclass(frozen=True)
class PaneDiagnosis:
    """What a stuck pane looked like, independent of any known cause.

    :param shape: Structural flags from :func:`pane_shape`, e.g.
        ``("no-tui-frame", "awaiting-input")``.
    :param fingerprint: Stable short hash from :func:`pane_fingerprint`.
    """

    shape: tuple[str, ...]
    fingerprint: str

    def describe(self) -> str:
        """Render as ``shape=a,b pane=<hash>`` for a log message or error text.

        :returns: A single-line, space-separated summary.
        """
        return f"shape={','.join(self.shape)} pane={self.fingerprint}"


def _visible_lines(pane: str) -> list[str]:
    """Return the pane's non-blank lines with escape sequences removed."""
    stripped = _ANSI_RE.sub("", pane)
    return [line.rstrip() for line in stripped.splitlines() if line.strip()]


def pane_shape(pane: str) -> tuple[str, ...]:
    """Describe a pane structurally, without knowing which CLI drew it.

    These are the questions worth asking of a screen nobody has classified:
    did the program draw a TUI at all (if not, it very likely never started),
    is it waiting on a keypress, did it print an error, is it showing a link
    the person was supposed to follow.

    :param pane: Captured pane text; may be empty.
    :returns: Ordered flags, always at least one. ``("blank",)`` when nothing
        was captured.
    """
    lines = _visible_lines(pane)
    if not lines:
        return ("blank",)
    haystack = "\n".join(lines).lower()
    flags: list[str] = []
    # A frame means the CLI's own UI is on screen; its absence means the pane is
    # still showing whatever ran before it (a shell, a launcher, a prompt).
    framed = any(
        len(line.strip()) >= 3 and sum(ch in _BOX_GLYPHS for ch in line) >= len(line.strip()) / 2
        for line in lines
    )
    flags.append("tui-frame" if framed else "no-tui-frame")
    last = lines[-1].strip().lower()
    if last.endswith((":", "?")) or any(m in haystack for m in _AWAITING_INPUT_MARKERS):
        flags.append("awaiting-input")
    if any(m in haystack for m in _ERROR_MARKERS):
        flags.append("error-text")
    if "://" in haystack:
        flags.append("url-shown")
    return tuple(flags)


def pane_fingerprint(pane: str) -> str:
    """Hash a pane's tail so the same screen groups across users and runs.

    Normalizes away everything that differs between two reports of the same
    screen — escape sequences, box glyphs, paths, URLs, emails, hex ids,
    numbers, and all whitespace — then hashes what is left. Lines are joined
    with single spaces rather than newlines so a narrower terminal, which wraps
    the same text across more rows, still fingerprints the same.

    :param pane: Captured pane text; may be empty.
    :returns: Short lowercase hex digest, or ``"blank"`` for an empty pane.
    """
    lines = _visible_lines(pane)
    if not lines:
        return "blank"
    text = " ".join(lines[-_FINGERPRINT_TAIL_LINES:]).lower()
    text = text.translate({ord(glyph): " " for glyph in _BOX_GLYPHS})
    for pattern, replacement in _FINGERPRINT_SCRUBBERS:
        text = pattern.sub(replacement, text)
    text = " ".join(text.split())
    if not text:
        return "blank"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:_FINGERPRINT_HEX_CHARS]


def diagnose_pane(pane: str) -> PaneDiagnosis:
    """Measure a stuck pane both ways.

    :param pane: Captured pane text; may be empty.
    :returns: The pane's structural shape and its fingerprint.
    """
    return PaneDiagnosis(shape=pane_shape(pane), fingerprint=pane_fingerprint(pane))

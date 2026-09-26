"""Tests for the terminal prompt's modified-key (Kitty CSI-u) handling.

The host opts into the Kitty keyboard protocol (to get Shift+Enter etc.), so
modified keys arrive as CSI-u sequences (``\\x1b[<code>;<mod>u``). Any sequence
that isn't registered leaks its literal tail (``[127;3u``) into the prompt. This
suite covers the registered set and the behaviors that matter for Claude Code /
readline parity:

- Option/Alt+Backspace and Ctrl+Backspace delete the previous WORD
  (regression for the ``[127;3u`` leak and the Ctrl+Backspace one-char bug).
- Option/Alt+Enter and Ctrl+Enter insert a newline (regression for the
  ``[13;3u`` / ``[13;5u`` leaks).
- Shift+Tab decodes to back-tab (regression for the ``[9;2u`` leak).
- Plain Backspace/Enter/Tab are unchanged (we didn't over-broaden).
"""

from __future__ import annotations

import asyncio

import pytest
from omnigent_ui_sdk.terminal._host import TerminalHost, _install_csi_u_sequences
from prompt_toolkit.application import Application
from prompt_toolkit.application.current import create_app_session
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document
from prompt_toolkit.input.vt100_parser import Vt100Parser
from prompt_toolkit.key_binding.defaults import load_key_bindings
from prompt_toolkit.key_binding.key_processor import KeyProcessor
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import Layout, Window
from prompt_toolkit.layout.controls import BufferControl

# Populate the global ANSI_SEQUENCES table the Vt100Parser reads (idempotent).
_install_csi_u_sequences()


def _parse_raw(raw: str) -> list:
    """Decode a raw terminal byte string into prompt_toolkit KeyPress objects
    via the real Vt100Parser (which reads the registered ANSI_SEQUENCES)."""
    presses: list = []
    parser = Vt100Parser(presses.append)
    parser.feed(raw)
    parser.flush()
    return presses


async def _apply_raw(start_text: str, raw: str, *, times: int = 1) -> str:
    """Feed ``raw`` (repeated ``times``) into a buffer holding ``start_text``
    with the cursor at the end, through the default emacs key bindings, and
    return the resulting buffer text. Drives the full real input pipeline."""
    buf = Buffer(document=Document(start_text, len(start_text)))
    app = Application(
        layout=Layout(Window(BufferControl(buffer=buf))),
        key_bindings=load_key_bindings(),
    )
    with create_app_session() as session:
        session.app = app
        processor = KeyProcessor(app.key_bindings)
        for _ in range(times):
            for press in _parse_raw(raw):
                processor.feed(press)
        processor.process_keys()
        await asyncio.sleep(0)  # let the processor settle on the running loop
    return buf.text


# ── decode: every fixed CSI-u sequence resolves to one real key (no leak) ──


@pytest.mark.parametrize(
    ("label", "raw", "expected"),
    [
        # Fixed by this change:
        ("Option/Alt+Backspace", "\x1b[127;3u", Keys.ControlW),
        ("Ctrl+Backspace", "\x1b[127;5u", Keys.ControlW),
        ("Option/Alt+Enter", "\x1b[13;3u", Keys.F20),
        ("Ctrl+Enter", "\x1b[13;5u", Keys.F20),
        ("Shift+Tab", "\x1b[9;2u", Keys.BackTab),
        # Guards — unchanged plain keys:
        ("plain Backspace", "\x1b[127u", Keys.Backspace),
        ("plain Enter", "\x1b[13u", Keys.ControlM),
        ("plain Tab", "\x1b[9u", Keys.ControlI),
    ],
)
def test_csi_u_sequence_decodes_to_single_key(label: str, raw: str, expected: Keys) -> None:
    """Each sequence decodes to exactly one key — proving both the correct
    target and the absence of a literal-text leak (a leak yields Escape plus
    several printable keys, i.e. len > 1)."""
    presses = _parse_raw(raw)
    assert len(presses) == 1, f"{label}: expected 1 key, got {[str(p.key) for p in presses]}"
    assert presses[0].key == expected, label


# ── functional: backward word-delete on Option+/Ctrl+Backspace ──


@pytest.mark.parametrize("raw", ["\x1b[127;3u", "\x1b[127;5u"])
async def test_modified_backspace_deletes_previous_word(raw: str) -> None:
    """Option/Alt+Backspace and Ctrl+Backspace both delete the previous word
    end-to-end (raw bytes → parser → buffer)."""
    assert await _apply_raw("hello world", raw) == "hello "


async def test_plain_backspace_still_deletes_one_char() -> None:
    """Guard: plain Backspace (CSI-u) deletes a single char, not a word."""
    assert await _apply_raw("hello world", "\x1b[127u") == "hello worl"


@pytest.mark.parametrize(
    ("start", "times", "expected"),
    [
        ("hello world", 1, "hello "),  # trailing word
        ("one two three", 2, "one "),  # repeated kills successive words
        ("foo.bar baz", 1, "foo.bar "),  # whitespace boundary keeps punctuation
        ("word", 1, ""),  # single word → empty
        ("", 1, ""),  # empty buffer → no crash, stays empty
        ("hello world ", 1, "hello "),  # trailing space is consumed with the word
    ],
)
async def test_word_delete_edge_cases(start: str, times: int, expected: str) -> None:
    """Backward word-delete (via Option+Backspace) across boundary/edge cases."""
    assert await _apply_raw(start, "\x1b[127;3u", times=times) == expected


# ── decode: extended Ctrl/Alt coverage (i/j/m/v/x, punctuation, Ignore) ──


@pytest.mark.parametrize(
    ("label", "raw", "expected"),
    [
        # Ctrl+<letter>, previously missing i/j/m/v/x — Kitty reports the
        # base letter codepoint under Ctrl, not the legacy C0 code, so
        # Ctrl+I/J/M arrive as [105;5u] / [106;5u] / [109;5u].
        ("Ctrl+I", "\x1b[105;5u", Keys.ControlI),
        ("Ctrl+J", "\x1b[106;5u", Keys.ControlJ),
        ("Ctrl+M", "\x1b[109;5u", Keys.ControlM),
        ("Ctrl+V", "\x1b[118;5u", Keys.ControlV),
        ("Ctrl+X", "\x1b[120;5u", Keys.ControlX),
        # Ctrl+Shift+letter: xterm sends the same byte as plain Ctrl+letter.
        ("Ctrl+Shift+I", "\x1b[105;6u", Keys.ControlI),
        # Ctrl+<punctuation> matching legacy ASCII control-masking.
        ("Ctrl+Space", "\x1b[32;5u", Keys.ControlAt),
        ("Ctrl+/", "\x1b[47;5u", Keys.ControlUnderscore),
        ("Ctrl+[", "\x1b[91;5u", Keys.Escape),
        ("Ctrl+\\", "\x1b[92;5u", Keys.ControlBackslash),
        ("Ctrl+]", "\x1b[93;5u", Keys.ControlSquareClose),
        # Ignore fallback — no dedicated mapping, no literal-text leak.
        # Ctrl+Z/Ctrl+Shift+Z land here deliberately: prompt-toolkit's
        # built-in "c-z" binding re-inserts the raw matched sequence, so
        # mapping "z" to ControlZ would itself leak; Ignore is silent.
        ("Ctrl+Z", "\x1b[122;5u", Keys.Ignore),
        ("Ctrl+Shift+Z", "\x1b[122;6u", Keys.Ignore),
        ("Ctrl+1 (unbound digit)", "\x1b[49;5u", Keys.Ignore),
        ("Ctrl+- (unbound punct)", "\x1b[45;5u", Keys.Ignore),
        ("Super+A", "\x1b[97;9u", Keys.Ignore),
        ("Alt+Esc", "\x1b[27;3u", Keys.Ignore),
        ("Ctrl+Shift+Enter", "\x1b[13;6u", Keys.Ignore),
        ("Alt+Tab", "\x1b[9;3u", Keys.Ignore),
        ("Alt+Shift+, (layout-dependent)", "\x1b[44;4u", Keys.Ignore),
    ],
)
def test_extended_sequence_decodes_to_single_key(label: str, raw: str, expected: Keys) -> None:
    """Same contract as ``test_csi_u_sequence_decodes_to_single_key`` above,
    for the sequences added/fixed in the CSI-u leak follow-up."""
    presses = _parse_raw(raw)
    assert len(presses) == 1, f"{label}: expected 1 key, got {[str(p.key) for p in presses]}"
    assert presses[0].key == expected, label


# ── decode: Alt / Alt+Shift / Ctrl+Alt(+Shift) resolve to (Escape, <key>) ──


@pytest.mark.parametrize(
    ("label", "raw", "expected"),
    [
        # Alt+<printable> — the leaks the bug report reproduced.
        ("Alt+B", "\x1b[98;3u", (Keys.Escape, "b")),
        ("Alt+F", "\x1b[102;3u", (Keys.Escape, "f")),
        ("Alt+1", "\x1b[49;3u", (Keys.Escape, "1")),
        ("Alt+.", "\x1b[46;3u", (Keys.Escape, ".")),
        # Alt+Shift+<letter> — base codepoint, upper-cased locally.
        ("Alt+Shift+B", "\x1b[98;4u", (Keys.Escape, "B")),
        ("Alt+Shift+Z", "\x1b[122;4u", (Keys.Escape, "Z")),
        # Ctrl+Alt(+Shift)+<letter> — Escape prefix in front of Ctrl+letter.
        # (Escape, ControlA) has no emacs binding, so on a real host the
        # Escape half falls through to the host's own escape→cancel
        # binding — same as legacy ESC-prefix parity, a documented
        # limitation rather than a leak.
        ("Ctrl+Alt+B", "\x1b[98;7u", (Keys.Escape, Keys.ControlB)),
        ("Ctrl+Alt+Shift+A", "\x1b[97;8u", (Keys.Escape, Keys.ControlA)),
    ],
)
def test_alt_sequence_decodes_to_escape_pair(
    label: str, raw: str, expected: tuple[Keys | str, Keys | str]
) -> None:
    """Each sequence decodes to exactly the two keypresses ``(Escape, X)`` —
    the same shape a legacy ESC-prefixed terminal produces. A leak instead
    yields Escape plus several extra printable keys, i.e. more than 2 presses."""
    presses = _parse_raw(raw)
    assert len(presses) == 2, f"{label}: expected 2 keys, got {[str(p.key) for p in presses]}"
    assert presses[0].key == expected[0], label
    assert presses[1].key == expected[1], label


# ── functional: Alt+B / Alt+F drive real emacs word-movement bindings ──


async def _apply_raw_cursor(start_text: str, raw: str, *, cursor: int | None = None) -> int:
    """Like ``_apply_raw`` but returns the resulting cursor position instead
    of the buffer text — word-movement doesn't change the text, only where
    the cursor ends up. ``cursor`` defaults to end-of-text."""
    buf = Buffer(document=Document(start_text, cursor if cursor is not None else len(start_text)))
    app = Application(
        layout=Layout(Window(BufferControl(buffer=buf))),
        key_bindings=load_key_bindings(),
    )
    with create_app_session() as session:
        session.app = app
        processor = KeyProcessor(app.key_bindings)
        for press in _parse_raw(raw):
            processor.feed(press)
        processor.process_keys()
        await asyncio.sleep(0)
    return buf.cursor_position


@pytest.mark.parametrize(
    ("label", "raw", "start_cursor", "expected_cursor"),
    [
        ("Alt+B backward-word", "\x1b[98;3u", None, 6),
        ("Alt+F forward-word", "\x1b[102;3u", 0, 5),
    ],
)
async def test_alt_sequence_drives_emacs_word_movement(
    label: str, raw: str, start_cursor: int | None, expected_cursor: int
) -> None:
    """Alt+B/Alt+F fire prompt-toolkit's real ``backward-word``/``forward-word``
    emacs bindings end-to-end, not just decode to the right key."""
    cursor = await _apply_raw_cursor("hello world", raw, cursor=start_cursor)
    assert cursor == expected_cursor, label


# ── host-level: real TerminalHost, real key_processor, no cancel misfire ──


async def _feed_host(host: TerminalHost, raw: str) -> None:
    """Feed ``raw`` through the real, already-constructed prompt session's
    key processor — the same object ``TerminalHost.run`` drives — so these
    tests exercise the host's own key bindings layered on prompt-toolkit's
    defaults, not a bare test harness."""
    app = host._prompt.app
    with create_app_session() as session:
        session.app = app
        kp = app.key_processor
        kp.reset()
        for press in _parse_raw(raw):
            kp.feed(press)
        kp.process_keys()
        await asyncio.sleep(0)


async def test_host_alt_b_moves_cursor_without_cancelling() -> None:
    """On a real :class:`TerminalHost`, Alt+B moves the cursor back a word
    and does not trigger the host's Escape/cancel path."""
    host = TerminalHost()
    cancelled = False

    def _cancel() -> None:
        nonlocal cancelled
        cancelled = True

    host.cancel = _cancel  # type: ignore[method-assign]
    buf = host._prompt.default_buffer
    buf.text = "hello world"
    buf.cursor_position = len(buf.text)

    await _feed_host(host, "\x1b[98;3u")

    assert buf.cursor_position == 6
    assert not cancelled


async def test_host_ignore_fallback_inserts_nothing_and_does_not_cancel() -> None:
    """A sequence with no dedicated mapping (Super+A) decodes to
    :class:`Keys.Ignore` on a real host: no text inserted, no cancel."""
    host = TerminalHost()
    cancelled = False

    def _cancel() -> None:
        nonlocal cancelled
        cancelled = True

    host.cancel = _cancel  # type: ignore[method-assign]
    buf = host._prompt.default_buffer
    buf.text = "hello world"
    buf.cursor_position = len(buf.text)

    await _feed_host(host, "\x1b[97;9u")  # Super+A

    assert buf.text == "hello world"
    assert not cancelled

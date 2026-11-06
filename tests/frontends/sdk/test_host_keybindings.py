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
- Every Ctrl+letter decodes to its control key; the host binds Ctrl+J to
  "insert newline" (regression for the ``[106;5u`` leak).
- Alt+<key> decodes to Escape + the key, exactly like the legacy ``ESC <key>``
  encoding, so the emacs word motions fire (regression for the ``[98;3u`` /
  ``[102;3u`` leaks).
- Plain Backspace/Enter/Tab are unchanged (we didn't over-broaden).
"""

from __future__ import annotations

import asyncio
import string

import pytest
from omnigent_ui_sdk.terminal._host import _install_csi_u_sequences
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


async def _drive_raw(
    start_text: str, raw: str, *, cursor: int | None = None, times: int = 1
) -> Buffer:
    """Feed ``raw`` (repeated ``times``) into a buffer holding ``start_text``
    (cursor at the end unless ``cursor`` is given), through the default emacs
    key bindings, and return the buffer. Drives the full real input pipeline."""
    position = len(start_text) if cursor is None else cursor
    buf = Buffer(document=Document(start_text, position))
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
    return buf


async def _apply_raw(start_text: str, raw: str, *, times: int = 1) -> str:
    """The buffer text left behind by :func:`_drive_raw`."""
    return (await _drive_raw(start_text, raw, times=times)).text


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
        ("Ctrl+J", "\x1b[106;5u", Keys.ControlJ),
        ("Ctrl+V", "\x1b[118;5u", Keys.ControlV),
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


def test_every_ctrl_letter_decodes_to_its_control_key() -> None:
    """The terminal encodes any Ctrl+letter as ``<cp>;5u`` once the host has
    pushed the protocol, so every letter must resolve (a gap leaks text)."""
    for ch in string.ascii_lowercase:
        presses = _parse_raw(f"\x1b[{ord(ch)};5u")
        expected = getattr(Keys, f"Control{ch.upper()}")
        assert [p.key for p in presses] == [expected], f"Ctrl+{ch.upper()}"


# ── decode: Alt+<key> resolves to Escape + key, like the legacy ESC prefix ──


@pytest.mark.parametrize(
    ("label", "raw", "char"),
    [
        ("Alt+B", "\x1b[98;3u", "b"),
        ("Alt+F", "\x1b[102;3u", "f"),
        ("Alt+D", "\x1b[100;3u", "d"),
        ("Alt+.", "\x1b[46;3u", "."),
    ],
)
def test_csi_u_alt_key_decodes_like_legacy_escape_prefix(label: str, raw: str, char: str) -> None:
    """Alt+<key> yields the same two presses as the legacy ``ESC <key>`` bytes,
    so every ``escape <key>`` binding keeps working on CSI-u terminals."""
    csi_u_keys = [p.key for p in _parse_raw(raw)]
    legacy_keys = [p.key for p in _parse_raw("\x1b" + char)]
    assert csi_u_keys == legacy_keys == [Keys.Escape, char], label


# ── functional: Alt+B / Alt+F / Alt+D act on words instead of leaking text ──


async def test_alt_b_moves_back_a_word() -> None:
    buf = await _drive_raw("hello world", "\x1b[98;3u")
    assert (buf.text, buf.cursor_position) == ("hello world", len("hello "))


async def test_alt_f_moves_forward_a_word() -> None:
    buf = await _drive_raw("hello world", "\x1b[102;3u", cursor=0)
    assert (buf.text, buf.cursor_position) == ("hello world", len("hello"))


async def test_alt_d_kills_the_next_word() -> None:
    buf = await _drive_raw("hello world", "\x1b[100;3u", cursor=0)
    assert buf.text == " world"


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

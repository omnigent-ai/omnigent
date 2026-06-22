"""
Unit tests for the Kitty Keyboard Protocol (CSI-u) escape
sequence registration in
:mod:`omnigent_ui_sdk.terminal._host`.

The host installs a set of ``\\x1b[<cp>;<modifier>u`` →
:class:`prompt_toolkit.keys.Keys` translations at construction
time so terminals running CSI-u (Kitty, WezTerm, Ghostty,
iTerm2 with the option enabled, modern xterm) see their
``Ctrl+letter`` / ``Shift+Enter`` keystrokes recognized
instead of silently dropped. Without the install, prompt-
toolkit's vt100 parser doesn't know about CSI-u and those
keys never fire any binding.

These tests pin two contracts:

1. The expected sequences land in
   ``prompt_toolkit.input.vt100_parser.ANSI_SEQUENCES`` after
   :class:`TerminalHost` construction.
2. The install is idempotent — multiple host constructions in
   the same process don't error or re-mutate the parser dict
   redundantly.
"""

from __future__ import annotations

from omnigent_ui_sdk import TerminalHost


def _ansi_sequences() -> dict[str, object]:
    """Return prompt-toolkit's ANSI_SEQUENCES dict.

    Imported lazily so a future prompt-toolkit version that
    moves the symbol surfaces as a clear ImportError at test
    collection time, not as a confusing AttributeError mid-
    test.

    :returns: The mutable mapping prompt-toolkit's vt100
        parser consults to resolve escape sequences to keys.
    """
    from prompt_toolkit.input.vt100_parser import (  # type: ignore[attr-defined]
        ANSI_SEQUENCES,
    )

    return ANSI_SEQUENCES


def test_csi_u_install_registers_ctrl_letter_keys() -> None:
    """
    Constructing a :class:`TerminalHost` installs the
    ``Ctrl+letter`` CSI-u sequences.

    The user-impact case: on a Kitty-protocol terminal,
    pressing Ctrl+C in the REPL emits ``\\x1b[3;5u``. Without
    this registration, prompt-toolkit's vt100 parser doesn't
    recognize the sequence and the keystroke is silently
    dropped — Ctrl+C would appear to do nothing.
    """
    from prompt_toolkit.keys import Keys

    TerminalHost()
    seqs = _ansi_sequences()

    # Spot-check the codepoint-keyed entries (Ctrl+C, D, etc.)
    assert seqs.get("\x1b[3;5u") == Keys.ControlC
    assert seqs.get("\x1b[4;5u") == Keys.ControlD
    # And the letter-keyed entries (Ctrl+A through Ctrl+Y).
    assert seqs.get(f"\x1b[{ord('a')};5u") == Keys.ControlA
    assert seqs.get(f"\x1b[{ord('r')};5u") == Keys.ControlR


def test_csi_u_install_registers_shift_enter_as_f20() -> None:
    """
    Shift+Enter under CSI-u (``\\x1b[13;2u``) maps to
    :attr:`Keys.F20`.

    This is what makes Shift+Enter work for newline insertion
    in the REPL prompt — the host's ``f20`` binding then turns
    the F20 key event into ``buffer.insert_text("\\n")``. If
    this assertion fails, Shift+Enter on Kitty-protocol
    terminals stops inserting newlines (the CSI-u sequence
    becomes an unrecognized escape and prompt-toolkit's
    parser drops the keystroke).
    """
    from prompt_toolkit.keys import Keys

    TerminalHost()
    seqs = _ansi_sequences()

    assert seqs.get("\x1b[13;2u") == Keys.F20


def test_csi_u_install_registers_special_keys() -> None:
    """
    Escape, Backspace variants, Delete, plain Enter, and
    focus-reporting sequences are recognized.

    Pinning the less-common sequences so a regression that
    drops one of them doesn't go unnoticed — Ctrl+Backspace
    deleting words is muscle memory for shell users, and a
    silent drop on a Kitty terminal is the kind of thing
    nobody bothers to file a bug about because they assume
    their terminal config is wrong.
    """
    from prompt_toolkit.keys import Keys

    TerminalHost()
    seqs = _ansi_sequences()

    assert seqs.get("\x1b[27u") == Keys.Escape
    assert seqs.get("\x1b[127;5u") == Keys.ControlH  # Ctrl+Backspace
    assert seqs.get("\x1b[127;2u") == Keys.Backspace  # Shift+Backspace
    assert seqs.get("\x1b[3;2~") == Keys.Delete  # Shift+Delete
    assert seqs.get("\x1b[13u") == Keys.ControlM  # plain Enter via CSI-u
    assert seqs.get("\x1b[I") == Keys.Ignore  # xterm/iTerm focus-in
    assert seqs.get("\x1b[O") == Keys.Ignore  # xterm/iTerm focus-out


def test_csi_u_install_is_idempotent() -> None:
    """
    Constructing two :class:`TerminalHost` instances back-to-
    back doesn't error, and the second construction is a
    no-op (the install guard short-circuits).

    Pytest runs many tests in one process, so this also
    indirectly verifies that creating a host in test N+1
    doesn't fail because the install ran in test N.
    """
    TerminalHost()
    TerminalHost()
    # If we got here, both constructions succeeded. The
    # state-equality check below confirms the second install
    # didn't blow away the first install's entries.
    seqs = _ansi_sequences()
    from prompt_toolkit.keys import Keys

    assert seqs.get("\x1b[3;5u") == Keys.ControlC

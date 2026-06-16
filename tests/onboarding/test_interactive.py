"""Tests for the shared onboarding interactive selectors.

:mod:`omnigent.onboarding.interactive` provides the theme-picker-styled
``select`` arrow-key menu and the ``prompt_text`` text input. The
raw-termios TTY path cannot be exercised in a headless test runner (no
controlling terminal), so these tests cover the **non-TTY numbered
fallback** — the path pipes, CI, and the CLI test suite actually hit.

Each test forces a non-TTY by monkeypatching ``sys.stdin.isatty`` to
``False`` and feeds input via ``click``'s isolated input stream, then
asserts on the returned index / string so a regression in the fallback
parsing surfaces here.
"""

from __future__ import annotations

import io
import sys

import pytest

from omnigent.onboarding import interactive


@pytest.fixture()
def non_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the interactive module onto its non-TTY numbered fallback.

    Patches ``sys.stdin.isatty`` to report ``False`` so :func:`select`
    and :func:`prompt_text` take the ``click.prompt`` path instead of the
    raw-termios loop (which needs a real terminal).

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)


def _feed(monkeypatch: pytest.MonkeyPatch, lines: list[str]) -> None:
    """Route *lines* to ``click.prompt`` as if typed at the console.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param lines: The lines to feed, one per ``click.prompt`` call, e.g.
        ``["2"]``.
    :returns: None.
    """
    fed = iter(lines)

    def _fake_prompt(_text: str) -> str:
        # click calls visible_prompt_func(" ") — the arg is the echoed
        # space, not a readline size, so ignore it and pop the next
        # scripted line (mirrors a user typing at the prompt).
        return next(fed)

    monkeypatch.setattr("click.termui.visible_prompt_func", _fake_prompt)


def _feed_hidden(monkeypatch: pytest.MonkeyPatch, lines: list[str]) -> None:
    """Route *lines* to hidden ``click.prompt`` input without echoing them.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param lines: The hidden lines to feed, one per ``click.prompt`` call.
    :returns: None.
    """
    fed = iter(lines)

    def _fake_prompt(_text: str) -> str:
        return next(fed)

    monkeypatch.setattr("click.termui.hidden_prompt_func", _fake_prompt)


def test_select_fallback_returns_chosen_index(
    non_tty: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``select`` non-TTY returns the zero-based index of the typed number.

    Feeds ``"2"`` against three options; the fallback must return index
    ``1`` (the second option). A failure means the 1-based→0-based
    conversion is wrong or the wrong option was selected — which would
    map a user's pick to the wrong provider/action downstream.
    """
    _feed(monkeypatch, ["2"])
    result = interactive.select("Pick one", ["alpha", "beta", "gamma"])
    # Typed "2" → second option → zero-based index 1. If 0 or 2, the
    # off-by-one fallback parsing regressed.
    assert result == 1
    # The numbered list rendered every option so the user could choose.
    out = capsys.readouterr().out
    assert "1. alpha" in out
    assert "2. beta" in out


def test_select_fallback_reprompts_on_invalid_then_accepts(
    non_tty: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``select`` non-TTY rejects an out-of-range pick, then accepts a valid one.

    Feeds ``"9"`` (out of range for two options) followed by ``"1"``. The
    fallback must reject the first, print the invalid-selection notice,
    and return index ``0`` for the second. A failure means invalid input
    was silently accepted (returning a bogus index) or the reprompt loop
    is broken.
    """
    _feed(monkeypatch, ["9", "1"])
    result = interactive.select("Pick one", ["alpha", "beta"])
    # "9" is out of range and must be rejected; "1" then selects the
    # first option (index 0). If the first read had been accepted, this
    # would not equal 0.
    assert result == 0
    out = capsys.readouterr().out
    # The reject path printed the invalid-selection notice exactly once
    # for the bad "9" entry.
    assert "Invalid selection." in out


def test_select_rejects_mismatched_descriptions(non_tty: None) -> None:
    """``select`` fails loud when descriptions length differs from options.

    A mismatch is a caller bug (the selected description would index out
    of range during render), so it must raise rather than silently
    truncating. A failure here means the guard was dropped.
    """
    with pytest.raises(ValueError):
        interactive.select("Pick", ["a", "b"], descriptions=["only one"])


def test_select_empty_options_raises(non_tty: None) -> None:
    """``select`` rejects an empty option list.

    There is nothing to choose from, so the function must raise rather
    than return a meaningless index.
    """
    with pytest.raises(ValueError):
        interactive.select("Pick", [])


def test_select_fallback_skips_header_rows_in_numbering(
    non_tty: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Non-selectable header rows are unnumbered; numbers map to originals.

    A grouped tree passes header rows (harness names) interleaved with
    selectable provider rows. The numbered fallback must number ONLY the
    selectable rows and map the typed number back to the original index.
    Here ``selectable=[F, T, T, F, T]``: typing ``"2"`` picks the second
    *selectable* row, whose original index is 2. A failure means headers
    leaked into the numbering (so the returned index would be wrong and
    the user's pick would land on the wrong provider).
    """
    _feed(monkeypatch, ["2"])
    options = ["Claude", "  anthropic", "  claude-sub", "Codex", "  openai"]
    selectable = [False, True, True, False, True]
    result = interactive.select("Configure harness", options, selectable=selectable)
    # 2nd selectable row = "  claude-sub" at original index 2. If headers
    # were numbered, "2" would have hit "Claude"/index 0 region instead.
    assert result == 2
    out = capsys.readouterr().out
    # Headers print as plain labels (no leading "N."); providers are numbered.
    assert "1.   anthropic" in out
    assert "2.   claude-sub" in out
    assert "3.   openai" in out
    # The header text appears but is never given a selectable number.
    assert "Claude" in out and "1. Claude" not in out
    assert "Codex" in out and "3. Codex" not in out


def test_select_fallback_default_maps_to_numbered_position(
    non_tty: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty enter returns the row at *default* (mapped past headers).

    With ``default=2`` (original index of the 2nd selectable row) and a
    leading header, an empty enter must resolve to original index 2 — the
    fallback translates the original default index to its position among
    the numbered rows and back. A failure means the default wiring ignored
    header offsets and would pre-select the wrong provider.
    """
    _feed(monkeypatch, [""])
    options = ["Claude", "  anthropic", "  claude-sub", "Quit"]
    selectable = [False, True, True, True]
    result = interactive.select("Configure harness", options, selectable=selectable, default=2)
    # Empty enter → click returns the default → original index 2.
    assert result == 2


def test_select_rejects_mismatched_selectable(non_tty: None) -> None:
    """``select`` fails loud when the selectable mask length differs.

    A mismatch is a caller bug (the mask would index out of range during
    render/navigation), so it must raise rather than silently mis-mark
    rows. A failure here means the guard was dropped.
    """
    with pytest.raises(ValueError):
        interactive.select("Pick", ["a", "b", "c"], selectable=[True, False])


def test_select_rejects_all_header_rows(non_tty: None) -> None:
    """``select`` rejects a mask with no selectable row.

    There would be nothing to choose, so the function must raise rather
    than spin or return a header index.
    """
    with pytest.raises(ValueError):
        interactive.select("Pick", ["Claude", "Codex"], selectable=[False, False])


def test_step_selectable_skips_headers() -> None:
    """``_step_selectable`` glides over non-selectable rows in both directions.

    This is the arrow-key cursor arithmetic. The raw-termios ↑/↓ path is
    unreachable in a headless runner, so per the testing guide (cursor
    arithmetic → focused unit test) the skip logic is verified directly.
    Mask ``[F, T, F, T, F]`` has selectable indices {1, 3}.
    """
    mask = [False, True, False, True, False]
    # Down from 1 wraps past the trailing header(s) to 3.
    assert interactive._step_selectable(mask, 1, +1) == 3
    # Down from 3 wraps around past index 4 and 0 to 1.
    assert interactive._step_selectable(mask, 3, +1) == 1
    # Up from 1 wraps backwards past index 0 and 4 to 3.
    assert interactive._step_selectable(mask, 1, -1) == 3
    # Up from 3 skips the header at 2 to land on 1.
    assert interactive._step_selectable(mask, 3, -1) == 1


def test_first_selectable_falls_to_first_selectable() -> None:
    """``_first_selectable`` honors a selectable default, else first selectable.

    Guards the initial cursor placement when *default* points at a header.
    Mask ``[F, F, T, T]``: a default of 0 (a header) must fall to index 2;
    a default of 3 (selectable) must be kept.
    """
    mask = [False, False, True, True]
    # Default points at a header → fall to the first selectable (index 2).
    assert interactive._first_selectable(mask, 0) == 2
    # Default already selectable → keep it.
    assert interactive._first_selectable(mask, 3) == 3


def test_prompt_text_fallback_returns_typed_value(
    non_tty: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``prompt_text`` non-TTY returns the typed string verbatim.

    Feeds ``"my-gateway"``; the function must return exactly that. A
    failure means the prompt mangled the value (e.g. stripped or defaulted
    it), which would persist a wrong provider name.
    """
    _feed(monkeypatch, ["my-gateway"])
    result = interactive.prompt_text("Name for this gateway")
    # The exact typed value must round-trip — proves no default override
    # or transformation happened.
    assert result == "my-gateway"


def test_prompt_text_fallback_uses_default_on_empty(
    non_tty: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``prompt_text`` non-TTY returns the default when the user enters nothing.

    Feeds an empty line with ``default="databricks"``; the function must
    return the default. A failure means the default wiring is broken and
    an empty enter would yield ``""`` (an invalid provider name).
    """
    _feed(monkeypatch, [""])
    result = interactive.prompt_text("Name for this provider", default="databricks")
    # Empty enter falls back to the supplied default.
    assert result == "databricks"


def test_prompt_text_fallback_hidden_value_confirms_without_echoing_secret(
    non_tty: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Hidden ``prompt_text`` feedback confirms paste registration safely."""
    secret = "sk-test-hidden-secret-1234567890"
    _feed_hidden(monkeypatch, [secret])

    result = interactive.prompt_text("openai API key", hide_input=True)

    assert result == secret
    out = capsys.readouterr().out
    assert "input hidden" in out
    assert f"received ({len(secret)} characters)" in out
    assert secret not in out


def test_prompt_text_fallback_hidden_empty_input_does_not_confirm(
    non_tty: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Hidden ``prompt_text`` does not claim an accepted default was pasted."""
    default = "existing-hidden-secret"
    _feed_hidden(monkeypatch, [""])

    result = interactive.prompt_text("openai API key", default=default, hide_input=True)

    assert result == default
    out = capsys.readouterr().out
    assert "input hidden" in out
    assert "received" not in out
    assert default not in out


def test_prompt_text_fallback_visible_value_has_no_hidden_feedback(
    non_tty: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Visible ``prompt_text`` behavior stays unchanged."""
    value = "plain-provider-name"
    _feed(monkeypatch, [value])

    result = interactive.prompt_text("Name for this provider", hide_input=False)

    assert result == value
    out = capsys.readouterr().out
    assert "input hidden" not in out
    assert "received" not in out


def test_clear_screen_emits_clear_sequence_only_on_a_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """``clear_screen`` wipes the terminal on a TTY and is a no-op otherwise.

    The TTY branch must emit the full clear (screen + scrollback + home) so the
    Databricks ``+ Add`` takeover's leftover subprocess output is wiped before
    the menu redraws. The non-TTY branch must emit NOTHING — otherwise escape
    sequences would leak into piped/CI output and the numbered-fallback tests.
    """

    class _FakeStdout(io.StringIO):
        def __init__(self, tty: bool) -> None:
            super().__init__()
            self._tty = tty

        def isatty(self) -> bool:
            return self._tty

    tty = _FakeStdout(tty=True)
    monkeypatch.setattr(sys, "stdout", tty)
    interactive.clear_screen()
    # 2J = clear screen, 3J = clear scrollback, H = home cursor.
    assert tty.getvalue() == "\033[2J\033[3J\033[H"

    pipe = _FakeStdout(tty=False)
    monkeypatch.setattr(sys, "stdout", pipe)
    interactive.clear_screen()
    assert pipe.getvalue() == ""  # no escape sequences leak into non-TTY output

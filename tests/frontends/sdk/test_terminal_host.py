"""
Unit tests for :class:`omnigent_ui_sdk.terminal._host.TerminalHost`.

Focused on host-level state-management methods that don't need a
real pty (overlays, key bindings, and rendering paths are covered
by the pty-driver tests in this directory).
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterable

import pytest
from omnigent_client import child_summary_busy
from omnigent_ui_sdk.terminal._formatter import StreamingText
from omnigent_ui_sdk.terminal._host import TerminalHost
from prompt_toolkit.output import DummyOutput
from rich.text import Text


def _formatted_text_plain(fragments: Iterable[tuple[str, str]]) -> str:
    """
    Join prompt-toolkit formatted-text fragments into visible text.

    :param fragments: ``(style, text)`` fragments returned by
        :meth:`TerminalHost.build_prompt` or
        :meth:`TerminalHost.build_toolbar`.
    :returns: Concatenated text payload without style names.
    """
    return "".join(text for _style, text in fragments)


@pytest.mark.asyncio
async def test_aenter_logs_stderr_redirect_failures(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    Stderr redirect setup failures must be logged, not silently swallowed.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param caplog: Pytest log capture fixture.
    :returns: ``None``.
    """

    def _raise_redirect_failure() -> None:
        """
        Stand in for a failed diagnostics stderr redirect.

        :returns: ``None``.
        :raises RuntimeError: Always, to exercise host logging.
        """
        raise RuntimeError("redirect failed")

    monkeypatch.setattr(
        "omnigent.cli_diagnostics.redirect_stderr_to_log",
        _raise_redirect_failure,
    )
    host = TerminalHost(model_name="test")

    with caplog.at_level(logging.ERROR, logger="omnigent_ui_sdk.terminal._host"):
        async with host:
            pass

    expected = "Failed to redirect stderr to the CLI diagnostics log: redirect failed"
    assert expected in caplog.text, (
        f"TerminalHost.__aenter__ swallowed stderr redirect failures instead "
        f"of logging them: {caplog.text!r}"
    )


@pytest.mark.asyncio
async def test_aexit_logs_stderr_restore_failures(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    Stderr restore failures must be logged, not silently swallowed.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param caplog: Pytest log capture fixture.
    :returns: ``None``.
    """

    def _raise_restore_failure() -> None:
        """
        Stand in for a failed diagnostics stderr restore.

        :returns: ``None``.
        :raises RuntimeError: Always, to exercise host logging.
        """
        raise RuntimeError("restore failed")

    monkeypatch.setattr(
        "omnigent.cli_diagnostics.restore_stderr",
        _raise_restore_failure,
    )
    host = TerminalHost(model_name="test")

    with caplog.at_level(logging.ERROR, logger="omnigent_ui_sdk.terminal._host"):
        async with host:
            pass

    expected = "Failed to restore stderr from the CLI diagnostics log: restore failed"
    assert expected in caplog.text, (
        f"TerminalHost.__aexit__ swallowed stderr restore failures instead "
        f"of logging them: {caplog.text!r}"
    )


def test_clear_streamed_text_drops_unflushed_partial_buffer(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """
    ``clear_streamed_text`` must drop the unflushed partial line in
    the streaming buffer so the next ``output()`` call doesn't
    re-emit it as raw text right before the rendered Markdown.

    This is the root cause of the "raw + rendered duplication" bug
    users hit when an LLM response with code blocks ends without a
    trailing newline: the streamer holds the tail of the message in
    ``_text_buffer`` waiting for a newline / wrap; the REPL calls
    ``clear_streamed_text`` to erase the printed lines; then
    ``output(Markdown(...))`` flushes that leftover partial as raw
    text right before printing the rendered panel — so the user
    sees the same content twice. The fix discards the partial in
    ``clear_streamed_text`` since the upcoming rendered output
    already contains the full message.
    """
    host = TerminalHost(model_name="test")

    # Stream a partial line. Short text with no newline / wrap
    # stays in the host's internal buffer — nothing is printed yet.
    host.output(StreamingText(text="partial-tail-no-newline"))

    # Discard anything pytest already captured.
    capsys.readouterr()

    # Clear streamed state. Mirrors what the REPL does on
    # ``TextDone`` with ``has_code_blocks=True`` right before
    # outputting the rendered Markdown panel.
    host.clear_streamed_text()

    # Render a non-streaming item (stand-in for the Markdown
    # panel the formatter would emit). Before the fix, this call
    # would flush the leftover buffer as raw text immediately
    # before printing the rendered content.
    host.output(Text("rendered-panel"))

    captured = capsys.readouterr()
    # If the partial leaked, it would appear as raw text in the
    # captured stdout alongside (or just before) "rendered-panel".
    # Failure here means clear_streamed_text() forgot to reset
    # _text_buffer — exactly the regression this test guards.
    assert "partial-tail-no-newline" not in captured.out, (
        f"clear_streamed_text leaked unflushed buffer content "
        f"into captured stdout: {captured.out!r}. The fix in "
        f"``clear_streamed_text`` must reset ``_text_buffer`` so "
        f"the next non-streaming ``output()`` does not re-emit "
        f"the partial line as raw text alongside the rendered "
        f"Markdown panel."
    )


def test_clear_streamed_text_resets_streaming_state(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """
    After ``clear_streamed_text``, a subsequent streaming call
    must NOT inherit the prior buffer's tail.

    Concretely: stream "partial", clear, then stream "fresh\\n".
    Only "fresh" should print — never "partialfresh". Without the
    buffer reset, the new chunk would be appended to the leftover
    "partial" and printed as a single line.
    """
    host = TerminalHost(model_name="test")

    host.output(StreamingText(text="partial"))
    capsys.readouterr()

    host.clear_streamed_text()

    # Stream a complete line — newline triggers a flush.
    host.output(StreamingText(text="fresh\n"))
    captured = capsys.readouterr()

    # If the buffer was not cleared, the host would have flushed
    # "partialfresh" as one line. The presence of "partialfresh"
    # would mean ``clear_streamed_text`` left the prior partial
    # in place and the next chunk concatenated to it.
    assert "partialfresh" not in captured.out, (
        f"clear_streamed_text left partial buffer behind, so the "
        f"next streamed chunk concatenated onto it: {captured.out!r}"
    )
    # Sanity: the new line did get printed.
    assert "fresh" in captured.out, (
        f"Expected the post-clear streamed line 'fresh' to appear "
        f"in captured stdout, but got: {captured.out!r}"
    )


def test_replace_streamed_text_issues_a_single_atomic_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``replace_streamed_text`` MUST issue the cursor-up + erase escapes
    AND the rendered renderable as a single ``sys.stdout.write`` call.

    Atomicity matters: the per-line ``print`` loop in
    ``clear_streamed_text`` followed by a separate render print
    produces N+1 syscalls, and prompt-toolkit's bottom-toolbar ticker
    can sneak a redraw between them. That's the visible flicker —
    especially noticeable on plain-prose paragraphs where the rendered
    Markdown looks ~identical to the streamed raw text.

    This test stubs ``sys.stdout.write`` and asserts that exactly one
    call goes out, containing both the clear escapes and the rendered
    output. If a future refactor splits these (e.g. for "simplicity"),
    this test catches it loud.
    """
    host = TerminalHost(model_name="test")

    # Plant some streamed lines so there's something to clear.
    host.output(StreamingText(text="line1\nline2\n"))
    pre_count = host._streamed_line_count

    # Capture every sys.stdout.write call from this point on.
    writes: list[str] = []

    def _record_write(data: str) -> int:
        writes.append(data)
        return len(data)

    def _noop_flush() -> None:
        pass

    monkeypatch.setattr(sys.stdout, "write", _record_write)
    monkeypatch.setattr(sys.stdout, "flush", _noop_flush)

    host.replace_streamed_text(Text("RENDERED"))

    # Exactly one write call: clear escapes + rendered ANSI in one
    # contiguous string. Multiple writes would mean the atomicity
    # is broken — the terminal could redraw between them.
    assert len(writes) == 1, (
        f"replace_streamed_text issued {len(writes)} sys.stdout.write "
        f"calls; expected exactly 1 for atomic clear+render. Multiple "
        f"writes break atomicity and reintroduce the flicker this "
        f"method exists to prevent. Writes: {writes!r}"
    )
    combined = writes[0]
    # The single write contains the clear-line ANSI sequence repeated
    # once per cleared line — verify the count matches the lines we
    # planted, so we know we cleared the right amount, not "all" or
    # "none".
    expected_clears = "\033[A\033[2K" * pre_count
    assert combined.startswith(expected_clears), (
        f"replace_streamed_text's single write did not start with the "
        f"expected {pre_count}-line cursor-up + erase sequence "
        f"({expected_clears!r}). Got prefix: {combined[:40]!r}. If the "
        f"clear count is wrong, the terminal will either leave "
        f"orphaned lines (count too low) or eat scrollback (count too "
        f"high)."
    )
    assert "RENDERED" in combined, (
        f"replace_streamed_text's single write did not include the "
        f"rendered text 'RENDERED'. Got: {combined!r}. The renderable "
        f"must be Console-rendered into the same write as the clear "
        f"escapes — separate writes lose atomicity."
    )


def test_replace_streamed_text_resets_streaming_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    After ``replace_streamed_text``, the streaming counters must reset
    so subsequent ``StreamingText`` items start a fresh accounting.

    If ``_streamed_line_count`` weren't reset, the next paragraph's
    ``StreamReplace`` would clear too many lines (the prior paragraph's
    rendered content above the streaming region). If ``_text_buffer``
    weren't reset, an unflushed partial from before the replace would
    be re-emitted by the next non-streaming ``output()``.
    """
    host = TerminalHost(model_name="test")
    host.output(StreamingText(text="line1\nline2\npartial-tail"))

    # Pre-condition: counters reflect the streamed state.
    assert host._streamed_line_count > 0
    assert host._text_buffer != ""

    # No-op stdout so the test doesn't print junk into pytest's capture.
    monkeypatch.setattr(sys.stdout, "write", lambda data: len(data))
    monkeypatch.setattr(sys.stdout, "flush", lambda: None)

    host.replace_streamed_text(Text("REPLACED"))

    assert host._streamed_line_count == 0, (
        f"Expected _streamed_line_count == 0 after replace, got "
        f"{host._streamed_line_count}. Subsequent paragraph "
        f"StreamReplace would over-clear, eating rendered content "
        f"from prior paragraphs."
    )
    assert host._text_buffer == "", (
        f"Expected _text_buffer == '' after replace, got "
        f"{host._text_buffer!r}. The next non-streaming output() "
        f"would flush this leftover as raw text — same bug as the "
        f"clear_streamed_text + output() duplication."
    )
    assert host._last_was_streaming is False, (
        "Expected _last_was_streaming == False after replace; the "
        "screen now shows the rendered replacement, not raw stream."
    )


def test_prompt_activity_row_keeps_same_height_after_work_finishes() -> None:
    """
    Busy → idle prompt renders keep the same row count.

    This guards the user-visible ``⠸ working``-stuck regression:
    prompt-toolkit can leave stale text behind when the prompt message
    shrinks from a two-row busy header to a shorter idle header. The
    idle prompt must therefore repaint a blank activity row in the same
    slot where the busy prompt painted ``working``.
    """
    host = TerminalHost(model_name="test")

    idle_before = _formatted_text_plain(host.build_prompt())
    host.start_timer()
    busy = _formatted_text_plain(host.build_prompt())
    host.stop_timer()
    idle_after = _formatted_text_plain(host.build_prompt())

    # Busy state must still expose the activity label users rely on
    # during active turns.
    assert "working" in busy, (
        f"Expected busy prompt to include the working indicator; "
        f"got {busy!r}. If absent, active turns no longer show the "
        f"top-of-prompt progress row."
    )
    # Idle state must overwrite that same row with blank text. If
    # ``working`` survives here, the regression is still present in the
    # host state even before terminal rendering is considered.
    assert "working" not in idle_after, (
        f"Expected idle prompt to clear the working label; got "
        f"{idle_after!r}. A lingering label makes the UI claim it is "
        f"still working after the toolbar says state: sleeping."
    )
    # Same newline count proves the prompt did not shrink on
    # busy->idle. A lower idle count would let prompt-toolkit leave
    # the old busy row orphaned above the separator bar.
    assert idle_before.count("\n") == busy.count("\n") == idle_after.count("\n"), (
        f"Expected idle and busy prompts to have the same rendered "
        f"height; got idle_before={idle_before!r}, busy={busy!r}, "
        f"idle_after={idle_after!r}. A height mismatch reintroduces "
        f"the stale 'working' line."
    )


class _TitleRecordingOutput(DummyOutput):
    """
    ``DummyOutput`` subclass that records ``set_title`` /
    ``clear_title`` / ``flush`` calls.

    Subclassing the real ``DummyOutput`` (rather than rolling our
    own from-scratch stub) ensures the rest of the ``Output``
    interface — ``responds_to_cpr``, ``fileno``,
    ``write``, etc. — keeps prompt-toolkit's Renderer / Application
    happy when the host hands the same Output to ``PromptSession``.
    A bare-class stub broke prompt-toolkit's renderer with
    ``AttributeError: 'X' object has no attribute 'responds_to_cpr'``.

    Why not ``MagicMock``: a MagicMock would silently return
    MagicMock for any attribute access — if the production code
    started calling a non-existent method (e.g. ``set_titel``),
    the test would still pass. A typed stub class fails loud when
    the contract changes.
    """

    def __init__(self) -> None:
        """Initialize empty call logs."""
        super().__init__()
        self.titles_set: list[str] = []
        self.clear_count: int = 0
        self.flush_count: int = 0

    def set_title(self, title: str) -> None:
        """Record a ``set_title`` call.

        :param title: The title that would be sent as the
            ``OSC 0 ; <title> BEL`` sequence.
        """
        self.titles_set.append(title)

    def clear_title(self) -> None:
        """Record a ``clear_title`` call."""
        self.clear_count += 1

    def flush(self) -> None:
        """Record a ``flush`` call."""
        self.flush_count += 1


def _patch_create_output(monkeypatch: pytest.MonkeyPatch, output: object) -> None:
    """
    Replace the SDK's ``create_output`` factory so the next
    :class:`TerminalHost` construction picks up *output*.

    Tests need to observe ``set_title`` / ``clear_title`` calls
    against the host's ``Output``, but those happen inside
    ``__aenter__`` / ``__aexit__`` against a private attribute.
    Reaching in to assign ``host._output = stub`` violates the
    project's "tests must not poke private attributes" rule;
    swapping the module-level factory at the seam where the host
    constructs its output is the publicly-supported alternative
    (``monkeypatch.setattr`` is the canonical pattern for this).

    :param monkeypatch: pytest's monkeypatch fixture.
    :param output: The replacement object — typically a
        :class:`_TitleRecordingOutput` or similar stub.
    """
    monkeypatch.setattr(
        "omnigent_ui_sdk.terminal._host.create_output",
        lambda: output,
    )


@pytest.mark.asyncio
async def test_window_title_set_on_aenter_and_cleared_on_aexit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Entering the host's async context with ``window_title="X"``
    sets the terminal title to ``"X"``; exiting clears it.

    What this proves: a user running ``omnigent run agent.yaml``
    sees the agent name in their terminal tab bar instead
    of the generic shell name. If ``set_title`` isn't called on
    enter, the tab bar stays as ``"$SHELL"`` and concurrent
    sessions are indistinguishable. If ``clear_title`` isn't
    called on exit, the title sticks after the REPL terminates.
    """
    stub = _TitleRecordingOutput()
    _patch_create_output(monkeypatch, stub)
    host = TerminalHost(model_name="test", window_title="agent-A")

    async with host:
        # ``__aenter__`` must have driven set_title before the
        # body runs — otherwise multi-tab UX breaks for the
        # session's entire lifetime.
        assert stub.titles_set == ["agent-A"], (
            f"Expected set_title to be called once with 'agent-A' on "
            f"__aenter__; got {stub.titles_set!r}. If [], __aenter__ "
            f"never invoked set_title — terminal tab bar stays as "
            f"'$SHELL' and multiple AP-mode sessions are indistinguishable."
        )
        # Clear must NOT have happened yet — that would mean
        # __aexit__ ran during __aenter__, a logic error.
        assert stub.clear_count == 0, (
            f"clear_title was called {stub.clear_count} time(s) before "
            f"__aexit__ ran — it should fire exactly once on exit, "
            f"never on enter."
        )

    # After exit: the title is cleared so the user's terminal
    # reverts to its original tab name.
    assert stub.clear_count == 1, (
        f"Expected clear_title to be called exactly once on __aexit__; "
        f"got {stub.clear_count}. If 0, the title persists after the "
        f"REPL exits and pollutes the user's tab bar."
    )


@pytest.mark.asyncio
async def test_window_title_none_skips_title_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    With ``window_title=None`` (the default), neither ``set_title``
    nor ``clear_title`` fires — the host leaves the user's terminal
    title untouched.

    What this proves: SDK consumers that don't pass a title (e.g.
    sample apps in ``examples/``, or any future caller that doesn't
    care about tab labeling) don't accidentally clobber the
    terminal title that the user's shell or terminal emulator set
    earlier. This is the "best-effort, opt-in" contract documented
    on the ``window_title`` parameter.
    """
    stub = _TitleRecordingOutput()
    _patch_create_output(monkeypatch, stub)
    host = TerminalHost(model_name="test")  # window_title defaults to None

    async with host:
        pass

    assert stub.titles_set == [], (
        f"Expected no set_title calls when window_title is None; "
        f"got {stub.titles_set!r}. A non-empty list means the host "
        f"is overriding terminal titles even when the caller didn't "
        f"opt in — violating the documented opt-in contract."
    )
    assert stub.clear_count == 0, (
        f"Expected no clear_title calls when window_title is None; "
        f"got {stub.clear_count}. The host should not touch the "
        f"terminal title at all in this case."
    )


def test_default_history_file_matches_legacy_omnigent_path() -> None:
    """
    With no ``history_file`` override, the host's prompt session
    persists input history to ``~/.omnigent_history`` — the
    same path the legacy ``omnigent run`` CLI uses
    (``omnigent/inner/cli.py:_cli_history_file_path``).

    What this proves: a user who flips between
    ``omnigent run agent.yaml`` (legacy) and
    ``omnigent run agent.yaml`` sees the same ↑ / Ctrl+R
    recall in both, instead of two divergent histories. If the
    default drifts (e.g. back to ``~/.omnigent-history``,
    or to a fresh location), this test fails loud at the SDK
    boundary so the divergence doesn't ship silently. The
    history file's location is part of the Omnigent mode-vs-legacy
    parity contract documented in
    ``designs/RUN_OMNIGENT_REPL_PARITY.md``.
    """
    import os

    host = TerminalHost(model_name="test")

    expected = os.path.expanduser("~/.omnigent_history")
    actual = host._prompt.history.filename
    assert actual == expected, (
        f"Default history_file resolved to {actual!r}; expected "
        f"{expected!r}. If the path is ``~/.omnigent-history`` "
        f"(the SDK's pre-unification default), the unification "
        f"with the legacy CLI was reverted — users flipping "
        f"between legacy and --omnigent would lose ↑ / Ctrl+R recall "
        f"again."
    )


@pytest.mark.asyncio
async def test_window_title_swallows_set_title_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    If ``set_title`` raises (e.g. an Output backend without title
    support), ``__aenter__`` still completes cleanly — the host
    enters the context as if the title path had succeeded.

    What this proves: the documented "best-effort" contract
    holds. A future Output backend that doesn't support OSC 0
    won't break ``omnigent run`` — the REPL still boots,
    just without the tab-label nicety.
    """

    class _RaisingOutput(DummyOutput):
        """Output subclass whose ``set_title`` always raises.

        Subclasses ``DummyOutput`` so the rest of the
        prompt-toolkit ``Output`` contract (``responds_to_cpr``,
        ``fileno``, ``write``, etc.) is satisfied — only
        ``set_title`` is overridden to simulate a title-
        unsupported terminal.
        """

        def set_title(self, title: str) -> None:
            """Raise to simulate a title-unsupported Output.

            :param title: Ignored — the override raises
                unconditionally.
            """
            del title
            raise OSError("simulated: terminal does not support OSC 0")

    _patch_create_output(monkeypatch, _RaisingOutput())
    host = TerminalHost(model_name="test", window_title="agent-A")

    # Should not raise; the host enters and exits cleanly.
    async with host:
        pass


def test_output_dispatches_stream_replace_to_replace_live_region(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``host.output(StreamReplace(renderable))`` must route to
    ``_replace_live_region(commit=True)`` rather than the generic
    non-streaming branch (which would flush ``_text_buffer`` as raw
    text first — re-introducing the duplication the replace method
    exists to avoid).

    Verifies the dispatch by checking state after the call:
    ``_live_line_count`` and ``_streamed_line_count`` must both be 0
    (committed), and the renderable content must appear in stdout.
    """
    from omnigent_ui_sdk.terminal._formatter import StreamReplace

    host = TerminalHost(model_name="test")

    writes: list[str] = []

    def _record_write(data: str) -> int:
        writes.append(data)
        return len(data)

    monkeypatch.setattr(sys.stdout, "write", _record_write)
    monkeypatch.setattr(sys.stdout, "flush", lambda: None)

    rendered = Text("from-stream-replace")
    host.output(StreamReplace(renderable=rendered))

    # Verify the renderable content made it to stdout.
    combined = "".join(writes)
    assert "from-stream-replace" in combined, (
        f"Expected 'from-stream-replace' in stdout output, got: "
        f"{combined!r}. The dispatch likely fell through to the "
        f"non-streaming branch."
    )
    # Verify committed state: both counters at 0.
    assert host._live_line_count == 0, (
        f"Expected _live_line_count == 0 after StreamReplace (commit), "
        f"got {host._live_line_count}."
    )
    assert host._streamed_line_count == 0, (
        f"Expected _streamed_line_count == 0 after StreamReplace, got {host._streamed_line_count}."
    )


def test_output_wraps_urls_in_osc_8_hyperlink(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """
    URLs in non-streaming Rich content emerge from ``output()``
    wrapped in OSC 8 hyperlink escape sequences, so terminals
    that support shell integration (iTerm2, Ghostty, kitty,
    etc.) render them as ⌘-clickable links.

    Pins the wiring between :meth:`TerminalHost.output` and
    :func:`omnigent_ui_sdk.terminal._linkify.linkify_ansi`.
    The detection logic itself is tested in
    ``tests/frontends/sdk/test_linkify.py``; this test only
    confirms the post-render hook is actually called on the
    output path.

    Failure mode caught: someone removes the ``linkify_ansi``
    call from ``output()`` (or swaps it for a no-op), and
    URLs in tool result panels / agent text stop being
    clickable. The OSC 8 byte sequence
    (``\\x1b]8;;<url>\\x1b\\``) doesn't show up by accident —
    Rich does NOT auto-emit it for plain URLs in pre-built
    Text/Panel/Group renderables, only when the URL is
    explicitly wrapped in ``[link=...]...[/link]`` markup.
    So if this assertion fails, the post-render linkify hook
    is broken.
    """
    host = TerminalHost(model_name="test")
    host.output(Text("Visit https://example.com here"))
    captured = capsys.readouterr()
    # Pin the EXACT byte sequence — this is the wire format
    # terminals consume. If it drifts, every ⌘-click breaks.
    assert "\x1b]8;;https://example.com\x1b\\https://example.com\x1b]8;;\x1b\\" in captured.out, (
        f"Expected OSC 8 hyperlink wrapping around the URL in "
        f"output(); got {captured.out!r}. Likely cause: the "
        f"``linkify_ansi(buf.getvalue())`` call in TerminalHost.output "
        f"was removed or replaced with a no-op."
    )


# ── Overlay sidebar viewport scrolling ──────────────────────


def test_compute_sidebar_scroll_offset_no_change_when_selection_in_view() -> None:
    """
    Selection inside the visible window leaves the offset alone.

    Reproduces the no-op case: user has 38 targets, viewport
    shows rows 5..32 (offset=5, height=28), tabbing to target 10
    keeps the offset at 5 — no gratuitous re-anchoring.

    On regression (always re-anchor): the viewport jumps every
    Tab even when the selection is already comfortably visible,
    which the user perceives as the sidebar twitching.
    """
    from omnigent_ui_sdk.terminal._host import _compute_sidebar_scroll_offset

    new_offset = _compute_sidebar_scroll_offset(
        selected_index=10,
        current_offset=5,
        visible_height=28,
    )
    # Selection at row 10 is within rows [5, 33) → no scroll.
    assert new_offset == 5, (
        f"expected offset to stay at 5 when selection is "
        f"comfortably inside the viewport; got {new_offset}. "
        f"A non-5 value means the function gratuitously "
        f"re-anchors and the sidebar twitches on every Tab."
    )


def test_compute_sidebar_scroll_offset_snaps_down_when_past_bottom() -> None:
    """
    Selection past the visible bottom snaps the viewport down so
    the selection lands on the last visible row.

    Reproduces the user-reported 2026-04-30 symptom: 38 targets
    in the sidebar, viewport bounded to ~28 rows, user
    tab-navigates to s36 (index 35). Without the snap, the
    selection cursor (▸) walked invisibly off the bottom.

    Snap policy: new_offset = selected_index - visible + 1,
    so selected_index lands on the last visible row
    (offset + visible - 1 == selected_index).

    On regression (no snap): the function returns
    current_offset unchanged, this test fails with
    ``new_offset == 0``, and the symptom returns.
    """
    from omnigent_ui_sdk.terminal._host import _compute_sidebar_scroll_offset

    new_offset = _compute_sidebar_scroll_offset(
        selected_index=35,
        current_offset=0,
        visible_height=28,
    )
    # selection at row 35 should sit on the last visible row.
    # last_visible = offset + height - 1 = 8 + 28 - 1 = 35. ✓
    assert new_offset == 8, (
        f"expected offset to snap to 8 (so row 35 lands on "
        f"the last visible row, since 8 + 28 - 1 = 35); got "
        f"{new_offset}. A 0 value means the snap-down branch "
        f"didn't fire and the selection walks off the bottom "
        f"of the viewport invisibly — exactly the user-reported "
        f"2026-04-30 symptom."
    )


def test_compute_sidebar_scroll_offset_snaps_up_when_above_viewport() -> None:
    """
    Selection above the viewport snaps the offset down to match.

    Models s-tab from a scrolled-down state back toward the top:
    user tabbed down to row 30 (offset 5), then s-tab back up
    past the visible top (selection at row 3, viewport at
    rows [5, 33)). The selection must scroll into view.

    Snap policy: new_offset = selected_index, so selection lands
    on the first visible row.

    Also covers the wrap-around case: tabbing from the LAST
    target wraps selected_index to 0 via ``(idx + 1) % len``.
    With current_offset > 0 from prior scrolling, selection=0
    triggers this branch and snaps the viewport back to the
    top — matching the user's expectation that wrapping returns
    to a top-anchored sidebar.
    """
    from omnigent_ui_sdk.terminal._host import _compute_sidebar_scroll_offset

    # Above-viewport case.
    above = _compute_sidebar_scroll_offset(
        selected_index=3,
        current_offset=5,
        visible_height=28,
    )
    assert above == 3, (
        f"expected snap-up to land selection on the first "
        f"visible row (offset == selected_index); got {above}."
    )
    # Wrap-around case — equivalent to "user tabbed past the
    # last entry and wrapped to 0" while the viewport was
    # scrolled down to row 7.
    wrapped = _compute_sidebar_scroll_offset(
        selected_index=0,
        current_offset=7,
        visible_height=28,
    )
    assert wrapped == 0, (
        f"expected wrap-to-top to snap viewport back to row 0; "
        f"got {wrapped}. If non-zero, the wrap-around case "
        f"leaves the viewport stranded and the user sees a "
        f"sidebar showing rows 7..34 with the selection "
        f"invisibly at row 0."
    )


def test_compute_sidebar_scroll_offset_handles_short_viewport() -> None:
    """
    Tiny viewports (height=1) still keep the selection visible.

    Catches a regression where the snap arithmetic
    (``selected_index - visible_height + 1``) underflows to a
    negative offset on small viewports. With visible_height=1
    and selection=0, the snap-down branch shouldn't fire because
    selection is already inside the [0, 1) window.
    """
    from omnigent_ui_sdk.terminal._host import _compute_sidebar_scroll_offset

    # selection inside the 1-row viewport — no scroll.
    assert (
        _compute_sidebar_scroll_offset(
            selected_index=0,
            current_offset=0,
            visible_height=1,
        )
        == 0
    )
    # selection past the 1-row viewport — snap so selection
    # lands on the only visible row.
    assert (
        _compute_sidebar_scroll_offset(
            selected_index=37,
            current_offset=0,
            visible_height=1,
        )
        == 37
    )


# ── StreamLive / live-region tests ───────────────────────────


def test_output_stream_live_replaces_live_region(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``output(StreamLive(renderable))`` clears the current live region
    and renders the new content without committing. After the call,
    ``_live_line_count`` reflects the rendered height so the next
    ``StreamLive`` can erase it.

    This is the core mechanism for incremental markdown rendering:
    each token re-renders the unstable tail via ``StreamLive``,
    erasing the previous tail and painting the updated one.
    """
    from omnigent_ui_sdk.terminal._formatter import StreamLive

    host = TerminalHost(model_name="test")

    writes: list[str] = []

    def _record_write(data: str) -> int:
        writes.append(data)
        return len(data)

    monkeypatch.setattr(sys.stdout, "write", _record_write)
    monkeypatch.setattr(sys.stdout, "flush", lambda: None)

    host.output(StreamLive(renderable=Text("live tail")))

    # At least one write must have occurred.
    assert len(writes) >= 1, (
        f"Expected at least 1 sys.stdout.write call from StreamLive output, got {len(writes)}."
    )
    combined = "".join(writes)
    assert "live tail" in combined, (
        f"Expected 'live tail' in the rendered output, got: {combined!r}."
    )

    # _live_line_count must be > 0 (tracks rendered height for next erase).
    assert host._live_line_count > 0, (
        f"Expected _live_line_count > 0 after StreamLive output, got "
        f"{host._live_line_count}. If 0, the next StreamLive won't know "
        f"how many lines to erase."
    )
    # _streamed_line_count stays at 0 — StreamLive doesn't affect
    # the raw streaming counter.
    assert host._streamed_line_count == 0, (
        f"Expected _streamed_line_count == 0 after StreamLive, got {host._streamed_line_count}."
    )


def test_stream_live_then_replace_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A ``StreamReplace`` after a ``StreamLive`` commits the content:
    the live region is cleared and replaced, and ``_live_line_count``
    resets to 0.

    This is the commit step: when the formatter finds a stable
    boundary, it emits ``StreamReplace`` which clears the live
    region and commits the content permanently.
    """
    from omnigent_ui_sdk.terminal._formatter import StreamLive, StreamReplace

    host = TerminalHost(model_name="test")

    monkeypatch.setattr(sys.stdout, "write", lambda data: len(data))
    monkeypatch.setattr(sys.stdout, "flush", lambda: None)

    # Plant a live region.
    host.output(StreamLive(renderable=Text("tail v1")))
    assert host._live_line_count > 0, "pre-condition: live region exists"

    # Commit via StreamReplace.
    host.output(StreamReplace(renderable=Text("committed paragraph")))

    assert host._live_line_count == 0, (
        f"Expected _live_line_count == 0 after StreamReplace commit, "
        f"got {host._live_line_count}. If non-zero, the live region "
        f"was not committed and subsequent StreamLive would erase "
        f"committed content."
    )
    assert host._streamed_line_count == 0, (
        f"Expected _streamed_line_count == 0 after commit, got {host._streamed_line_count}."
    )


def test_live_line_count_tracks_rendered_height(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``_live_line_count`` after a ``StreamLive`` matches the number of
    newlines in the rendered output, so the next erase clears exactly
    the right number of lines.

    Uses a multi-line renderable to verify the count isn't hardcoded
    to 1.
    """
    from omnigent_ui_sdk.terminal._formatter import StreamLive

    host = TerminalHost(model_name="test")

    rendered_output: list[str] = []

    def _capture_write(data: str) -> int:
        rendered_output.append(data)
        return len(data)

    monkeypatch.setattr(sys.stdout, "write", _capture_write)
    monkeypatch.setattr(sys.stdout, "flush", lambda: None)

    # A multi-line Text renderable.
    host.output(StreamLive(renderable=Text("line 1\nline 2\nline 3")))

    combined = "".join(rendered_output)
    expected_lines = combined.count("\n")
    # _live_line_count should match the rendered newline count.
    assert host._live_line_count == expected_lines, (
        f"Expected _live_line_count == {expected_lines} (matching rendered "
        f"newline count), got {host._live_line_count}. Mismatch means the "
        f"next StreamLive will erase too many or too few lines."
    )


def test_live_region_capped_to_viewport_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    When a ``StreamLive`` render exceeds the viewport ceiling,
    ``_live_line_count`` is capped so cursor-up can always reach
    every line on the next clear.

    Without the cap, lines that scroll into the terminal's scrollback
    buffer can't be erased — each re-render leaves stale content in
    scrollback, producing the "repeated bullet list" duplication bug.

    This test fakes a tiny viewport (10 rows, ceiling=5 after
    reserved rows) and renders content taller than 5 lines. The cap
    must truncate to the ceiling.
    """
    from omnigent_ui_sdk.terminal._formatter import StreamLive

    host = TerminalHost(model_name="test")

    monkeypatch.setattr(sys.stdout, "write", lambda data: len(data))
    monkeypatch.setattr(sys.stdout, "flush", lambda: None)
    # Fake a tiny terminal: 10 rows → ceiling = 10 - 5 = 5.
    monkeypatch.setattr(
        "omnigent_ui_sdk.terminal._host._term_height",
        lambda: 10,
    )

    # Render 20 lines — well above the ceiling of 5.
    long_text = "\n".join(f"line {i}" for i in range(20))
    host.output(StreamLive(renderable=Text(long_text)))

    # _live_line_count must be capped at the ceiling (5), not the
    # full rendered height (~20). If uncapped, the next StreamLive's
    # cursor-up would try to clear 20 lines but only reach 5 — the
    # other 15 would be stranded in scrollback as stale duplicates.
    ceiling = 10 - 5  # _term_height() - _BOTTOM_RESERVED_ROWS
    assert host._live_line_count <= ceiling, (
        f"Expected _live_line_count <= {ceiling} (viewport ceiling), "
        f"got {host._live_line_count}. Uncapped live regions cause "
        f"the scrollback duplication bug — cursor-up can't reach "
        f"lines that scrolled past the viewport top."
    )


_CURSOR_UP_ERASE = "\033[A\033[2K"


def test_growing_live_region_erase_count_matches_prior_render(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Simulate a growing bullet list streamed via successive
    ``StreamLive`` calls. On each call, the number of cursor-up
    erase sequences must equal the ``_live_line_count`` set by
    the PREVIOUS call — never more (would eat committed content
    above), never fewer (would leave stale duplicates).

    This is the exact scenario from the user-reported scrollback
    duplication bug: a 50-state bullet list where each chunk adds
    a few states. Without the viewport cap, the live region grows
    past the viewport, cursor-up can't reach the top lines, and
    every re-render leaves a stale copy in scrollback.

    Uses a fake 15-row terminal (ceiling=10) and grows the list
    from 2 to 20 lines across 10 steps.
    """
    from omnigent_ui_sdk.terminal._formatter import StreamLive

    host = TerminalHost(model_name="test")

    writes: list[str] = []

    def _capture_write(data: str) -> int:
        writes.append(data)
        return len(data)

    monkeypatch.setattr(sys.stdout, "write", _capture_write)
    monkeypatch.setattr(sys.stdout, "flush", lambda: None)
    # Fake terminal: 15 rows → ceiling = 15 - 5 = 10.
    monkeypatch.setattr(
        "omnigent_ui_sdk.terminal._host._term_height",
        lambda: 15,
    )
    ceiling = 10

    prev_live_count = 0
    for step in range(10):
        num_lines = 2 + step * 2  # 2, 4, 6, ... 20
        bullet_list = "\n".join(f"• State {i} — Capital {i}" for i in range(num_lines))
        writes.clear()
        host.output(StreamLive(renderable=Text(bullet_list)))

        combined = "".join(writes)

        # Count how many cursor-up+erase sequences were emitted.
        actual_erases = combined.count(_CURSOR_UP_ERASE)

        # The erase count must match the _live_line_count from the
        # PREVIOUS render — that's exactly what the host should
        # clear before painting the new content.
        assert actual_erases == prev_live_count, (
            f"Step {step} (rendering {num_lines} lines): expected "
            f"{prev_live_count} erase sequences (matching prior "
            f"_live_line_count), got {actual_erases}. "
            f"If higher, the host is over-erasing (would eat "
            f"committed content above). If lower, stale lines "
            f"from the prior render leak into scrollback."
        )

        # _live_line_count must never exceed the ceiling —
        # otherwise the next call's cursor-up can't reach all lines.
        assert host._live_line_count <= ceiling, (
            f"Step {step}: _live_line_count={host._live_line_count} "
            f"exceeds ceiling={ceiling}. Lines beyond the ceiling "
            f"scroll into scrollback where cursor-up can't reach "
            f"them — the next erase will leave stale duplicates."
        )

        prev_live_count = host._live_line_count


def test_stream_replace_after_overflowing_live_region(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A ``StreamReplace`` (commit) after an overflowing live region
    correctly clears only the capped portion and commits the full
    content permanently.

    This tests the transition from a viewport-capped live region to
    a commit: the erase count must match ``_live_line_count`` (which
    was capped), and after commit both counters reset to 0.
    """
    from omnigent_ui_sdk.terminal._formatter import StreamLive, StreamReplace

    host = TerminalHost(model_name="test")

    writes: list[str] = []

    def _capture_write(data: str) -> int:
        writes.append(data)
        return len(data)

    monkeypatch.setattr(sys.stdout, "write", _capture_write)
    monkeypatch.setattr(sys.stdout, "flush", lambda: None)
    # Tiny viewport: ceiling = 8 - 5 = 3.
    monkeypatch.setattr(
        "omnigent_ui_sdk.terminal._host._term_height",
        lambda: 8,
    )
    ceiling = 3

    # Plant a live region that overflows (15 lines > ceiling of 3).
    big_text = "\n".join(f"line {i}" for i in range(15))
    host.output(StreamLive(renderable=Text(big_text)))
    capped_count = host._live_line_count
    assert capped_count <= ceiling, "pre-condition: live region was capped"

    # Now commit via StreamReplace.
    writes.clear()
    host.output(StreamReplace(renderable=Text("committed content")))

    combined = "".join(writes)
    actual_erases = combined.count(_CURSOR_UP_ERASE)

    # Erase count must match the capped live count — not the full
    # 15 lines that were rendered (and partly scrolled off).
    assert actual_erases == capped_count, (
        f"Expected {capped_count} erases (matching capped "
        f"_live_line_count), got {actual_erases}. If {actual_erases} "
        f"> {capped_count}, the commit is trying to erase lines "
        f"that already scrolled into unreachable scrollback."
    )

    # After commit, both counters must be 0.
    assert host._live_line_count == 0, (
        f"Expected _live_line_count == 0 after commit, got {host._live_line_count}."
    )
    assert host._streamed_line_count == 0

    # The committed content must appear in the output — StreamReplace
    # is NOT capped (permanent content doesn't need clearing).
    assert "committed content" in combined, (
        "Committed content missing from output — StreamReplace "
        "should write the full renderable without viewport cap."
    )


def test_set_model_name_updates_toolbar_and_window_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``set_model_name`` swaps the toolbar label and re-emits the title.

    What this proves: when the session's bound agent changes mid-run
    (in-place agent switch made from another client), the REPL can
    rename the bottom-toolbar label and the terminal tab without
    rebuilding the host. If the toolbar still reads the old name, the
    user is told they're talking to an agent that is no longer bound.
    """
    stub = _TitleRecordingOutput()
    _patch_create_output(monkeypatch, stub)
    host = TerminalHost(model_name="nessie", window_title="nessie")

    before = _formatted_text_plain(host.build_toolbar())
    assert "nessie" in before, (
        f"Toolbar should start with the construction-time label; got "
        f"{before!r}. If missing, the test setup is wrong, not the setter."
    )

    host.set_model_name("claude native ui")

    after = _formatted_text_plain(host.build_toolbar())
    # The new label replaces the old one on the next toolbar repaint —
    # a surviving "nessie" means the setter mutated the wrong slot.
    assert "claude native ui" in after
    assert "nessie" not in after
    # A configured window title is re-emitted immediately with the new
    # name (the host hasn't entered its context, so this is the only
    # set_title call). [] means the title path was skipped and the tab
    # bar keeps the stale agent name.
    assert stub.titles_set == ["claude native ui"]


def test_set_model_name_without_window_title_skips_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host launched without a window title stays untitled on rename.

    ``window_title=None`` means the caller opted out of tab-title
    management (e.g. embedded hosts); the rename must not start
    emitting titles it was never configured to own.
    """
    stub = _TitleRecordingOutput()
    _patch_create_output(monkeypatch, stub)
    host = TerminalHost(model_name="nessie")

    host.set_model_name("claude native ui")

    # Toolbar still updates...
    assert "claude native ui" in _formatted_text_plain(host.build_toolbar())
    # ...but no title escape is emitted for an opted-out host.
    assert stub.titles_set == []


# ── Sub-agent tree: state badge + ↓ menu ───────────────────────────


def _busy(status: str = "in_progress") -> dict[str, object]:
    """A partial child payload for a busy sub-agent."""
    return {"busy": True, "current_task_status": status}


def _done(status: str = "completed") -> dict[str, object]:
    """A partial child payload for a finished sub-agent."""
    return {"busy": False, "current_task_status": status}


def test_subagent_badge_replaces_sleeping_while_active() -> None:
    """A busy sub-agent flips the idle ``sleeping`` badge to a running count.

    The top-level agent is idle here (no stream timer, no handler tasks), so
    without sub-agent awareness the toolbar would read ``state: sleeping``.
    """
    host = TerminalHost(model_name="test")
    assert "state: sleeping" in _formatted_text_plain(host.build_toolbar())

    host.upsert_subagent("conv_c1", parent_id="conv_main", child={"tool": "reviewer", **_busy()})

    toolbar = _formatted_text_plain(host.build_toolbar())
    assert "state: sleeping" not in toolbar
    assert "1 agent running" in toolbar
    assert "↓ agents" in toolbar  # the gesture hint is advertised


def test_subagent_badge_pluralizes_and_returns_to_sleeping() -> None:
    """The count pluralizes, and the badge returns to ``sleeping`` once the
    last sub-agent reaches a terminal status."""
    host = TerminalHost(model_name="test")
    host.upsert_subagent("conv_c1", parent_id="conv_main", child={"tool": "a", **_busy()})
    host.upsert_subagent("conv_c2", parent_id="conv_main", child={"tool": "b", **_busy()})

    assert "2 agents running" in _formatted_text_plain(host.build_toolbar())
    assert host.active_subagent_count() == 2
    assert host.has_active_subagents() is True

    host.upsert_subagent("conv_c1", parent_id="conv_main", child=_done())
    host.upsert_subagent("conv_c2", parent_id="conv_main", child=_done("failed"))

    # Just-finished agents are debounced: they still count (and the badge
    # still reads "running") until they've been terminal for the linger
    # window — this is what stops the badge flickering between turns.
    assert host.active_subagent_count() == 2
    # Backdate both completions past the linger window -> they settle out of
    # the RUNNING count, but stay retained in the selector (web parity).
    for sid in ("conv_c1", "conv_c2"):
        host._subagents[sid].done_at = host._monotonic() - 100.0
    assert host.active_subagent_count() == 0
    assert host.has_active_subagents() is False
    toolbar = _formatted_text_plain(host.build_toolbar())
    assert "state: sleeping" in toolbar
    # The ↓ hint stays: finished sub-agents are retained in the selector so the
    # user can revisit / chat with them, so the gesture remains advertised.
    assert host.has_any_subagents() is True
    assert "↓ agents" in toolbar


@pytest.mark.parametrize(
    ("busy", "status", "pending"),
    [
        (True, None, 0),
        (True, "completed", 0),
        (False, "in_progress", 0),
        (False, "queued", 0),
        (False, "launching", 0),
        (False, "completed", 1),  # awaiting input outranks a terminal status
        (False, None, 2),
        (False, "completed", 0),  # terminal → not busy
        (False, "failed", 0),
        (False, "cancelled", 0),
        (False, None, 0),  # warm-idle
    ],
)
def test_subagent_active_matches_sdk_predicate(
    busy: bool, status: str | None, pending: int
) -> None:
    """The CLI's per-node running decision equals the shared SDK predicate.

    Both consumers must agree on what "busy" means, mapped through their own
    field names (node ``status``/``pending_elicitations`` vs the summary's
    ``current_task_status``/``pending_elicitations_count``). The host adds a
    UI-only linger on top, so we isolate the non-terminal decision by clearing
    ``done_at`` before comparing. A divergence here is exactly the CLI/SDK
    drift this shared predicate exists to prevent.
    """
    host = TerminalHost(model_name="test")
    summary = {
        "busy": busy,
        "current_task_status": status,
        "pending_elicitations_count": pending,
    }
    host.upsert_subagent("conv_c1", parent_id="conv_main", child=dict(summary))
    node = host._subagents["conv_c1"]
    node.done_at = None  # isolate the non-terminal branch from the linger window

    assert host._subagent_active(node, host._monotonic()) == child_summary_busy(summary)


def test_subagent_partial_update_preserves_label() -> None:
    """A status-only delta must not blank the label/parent set by an
    earlier full payload (payloads are partial)."""
    host = TerminalHost(model_name="test")
    host.upsert_subagent(
        "conv_c1",
        parent_id="conv_main",
        child={"tool": "reviewer", "title": "reviewer:auth", **_busy("launching")},
    )
    # A later delta carrying only the status — no tool/title.
    host.upsert_subagent("conv_c1", child={"current_task_status": "in_progress"})

    rows = host.subagent_menu_rows()
    assert len(rows) == 1
    sid, label = rows[0]
    assert sid == "conv_c1"
    assert "reviewer:auth" in label  # label survived the status-only delta


def test_subagent_status_label_pending_elicitation_outranks_busy() -> None:
    """A sub-agent parked on an approval shows ``Needs response`` even while
    ``busy`` — mirroring the web ``childStatus`` priority."""
    host = TerminalHost(model_name="test")
    host.upsert_subagent(
        "conv_c1",
        parent_id="conv_main",
        child={"tool": "reviewer", "pending_elicitations_count": 1, **_busy()},
    )
    _sid, label = host.subagent_menu_rows()[0]
    assert "Needs response" in label
    assert "Working" not in label


def test_subagent_tree_reconstructs_hierarchy_in_preorder() -> None:
    """A grandchild nests under its parent; the badge counts the whole tree.

    Models a Claude sub-agent (``conv_child``) that spawns its own child
    (``conv_grand``). ``seed_subagent_tree`` carries each node's
    ``parent_id`` so the host can rebuild the hierarchy.
    """
    host = TerminalHost(model_name="test")
    host.seed_subagent_tree(
        "conv_main",
        [
            {"id": "conv_child", "parent_id": "conv_main", "tool": "coder", **_busy()},
            {"id": "conv_grand", "parent_id": "conv_child", "tool": "reviewer", **_busy()},
        ],
    )

    tree = host.subagent_tree()
    assert [(n.session_id, depth) for n, depth in tree] == [
        ("conv_child", 1),
        ("conv_grand", 2),
    ]
    # A busy grandchild counts toward the badge across the whole tree.
    assert host.active_subagent_count() == 2

    rows = host.subagent_menu_rows()
    # The grandchild row is indented one level deeper than its parent.
    assert rows[0][1].startswith("coder") or rows[0][1].lstrip().startswith("coder")
    assert rows[1][1].startswith("  ")  # depth-2 indent


def test_subagent_tree_depth_count_with_finished_parent() -> None:
    """A just-finished parent and its running grandchild both count and
    render (parent debounced within its linger); once the parent settles past
    the linger it drops out of the RUNNING count, but stays VISIBLE in the
    selector (retention / web parity), leaving the running grandchild counted."""
    host = TerminalHost(model_name="test")
    host.seed_subagent_tree(
        "conv_main",
        [
            {"id": "conv_child", "parent_id": "conv_main", **_done()},
            {"id": "conv_grand", "parent_id": "conv_child", **_busy()},
        ],
    )
    assert host.active_subagent_count() == 2
    assert {n.session_id for n, _ in host.subagent_tree()} == {"conv_child", "conv_grand"}
    # Settle the finished parent past its linger: it drops out of the running
    # count, but BOTH nodes remain in the selector tree (finished children are
    # retained, not hidden).
    host._subagents["conv_child"].done_at = host._monotonic() - 100.0
    assert host.active_subagent_count() == 1
    assert {n.session_id for n, _ in host.subagent_tree()} == {"conv_child", "conv_grand"}


def test_subagent_tree_cycle_guard() -> None:
    """A cyclic parent_id graph must not infinite-loop the pre-order walk."""
    host = TerminalHost(model_name="test")
    host.upsert_subagent("conv_a", parent_id="conv_b", child=_busy())
    host.upsert_subagent("conv_b", parent_id="conv_a", child=_busy())
    # Root is neither a nor b, so both are orphans surfaced once each.
    host._subagent_root = "conv_main"
    ids = [n.session_id for n, _ in host.subagent_tree()]
    assert sorted(ids) == ["conv_a", "conv_b"]


def test_subagent_seed_keeps_unsent_node_that_vanishes() -> None:
    """An unsent (still-active) sub-agent stays searchable even when a later
    snapshot omits it — it persists until it delivers its result."""
    host = TerminalHost(model_name="test")
    host.seed_subagent_tree(
        "conv_main",
        [{"id": "conv_child", "parent_id": "conv_main", **_busy()}],
    )
    assert host.active_subagent_count() == 1
    # A poll snapshot races the child and omits it; because the child hasn't
    # sent its response (no terminal status), it must NOT be dropped.
    host.seed_subagent_tree("conv_main", [])
    assert host.active_subagent_count() == 1
    assert [n.session_id for n, _ in host.subagent_tree()] == ["conv_child"]


def test_subagent_seed_drops_terminal_node_gone_from_snapshot() -> None:
    """A finished sub-agent is dropped only once the server STOPS listing it —
    i.e. it's gone from the snapshot. No linger wait is needed: retained nodes
    are the ones the server keeps reporting."""
    host = TerminalHost(model_name="test")
    host.seed_subagent_tree(
        "conv_main",
        [{"id": "conv_child", "parent_id": "conv_main", **_done()}],
    )
    # Terminal AND absent from the next snapshot -> dropped immediately (no
    # linger backdating required).
    host.seed_subagent_tree("conv_main", [])
    assert host.subagent_tree() == []


def test_subagent_seed_keeps_finished_node_still_in_snapshot() -> None:
    """A finished sub-agent the server STILL lists is retained in the selector
    indefinitely (web parity) — never pruned by a linger timer."""
    host = TerminalHost(model_name="test")
    host.seed_subagent_tree(
        "conv_main",
        [{"id": "conv_child", "parent_id": "conv_main", **_done()}],
    )
    # Backdate well past any linger window, then re-seed with the node STILL
    # present: it stays visible (the server hasn't forgotten it).
    host._subagents["conv_child"].done_at = host._monotonic() - 1000.0
    host.seed_subagent_tree(
        "conv_main",
        [{"id": "conv_child", "parent_id": "conv_main", **_done()}],
    )
    assert [n.session_id for n, _ in host.subagent_tree()] == ["conv_child"]
    assert host.active_subagent_count() == 0  # retained but not "running"


def test_subagent_badge_spinner_animates() -> None:
    """The running badge carries a spinner frame so it animates."""
    from omnigent_ui_sdk.terminal._host import _SPINNER_FRAMES

    host = TerminalHost(model_name="test")
    host.upsert_subagent("conv_c1", parent_id="conv_main", child=_busy())
    toolbar = _formatted_text_plain(host.build_toolbar())
    assert any(frame in toolbar for frame in _SPINNER_FRAMES)


def test_clear_subagents_empties_the_tree() -> None:
    """``clear_subagents`` resets the registry (used on reset/exit)."""
    host = TerminalHost(model_name="test")
    host.upsert_subagent("conv_c1", parent_id="conv_main", child=_busy())
    assert host.has_active_subagents() is True
    host.clear_subagents()
    assert host.has_active_subagents() is False
    assert host.subagent_tree() == []


# ── Inline ↓ sub-agents menu ───────────────────────────────────────


def _seed_busy_tree(host: TerminalHost, n: int = 2, root: str = "conv_main") -> None:
    """Seed *n* busy direct children under *root* so the menu can open."""
    host.seed_subagent_tree(
        root,
        [
            {"id": f"conv_c{i}", "parent_id": root, "tool": f"agent{i}", **_busy()}
            for i in range(n)
        ],
    )


def test_subagent_menu_opens_when_any_node_exists() -> None:
    """The inline menu opens whenever ANY sub-agent node exists — active OR
    finished — so finished children stay reachable for revisit / chat (web
    parity). It's a no-op only when the tree is genuinely empty."""
    host = TerminalHost(model_name="test")
    host._open_subagent_menu()
    assert host._subagent_menu_open is False  # empty tree -> no-op

    # A busy child opens it.
    _seed_busy_tree(host, 2)
    host._open_subagent_menu()
    assert host._subagent_menu_open is True


def test_subagent_menu_opens_with_only_finished_nodes() -> None:
    """The menu opens even when EVERY child has finished — the selector retains
    finished sub-agents so the user can dive back in and chat with them."""
    host = TerminalHost(model_name="test")
    host.seed_subagent_tree(
        "conv_main",
        [{"id": "conv_c1", "parent_id": "conv_main", "tool": "reviewer", **_done()}],
    )
    # Settle past the linger so nothing is "running"...
    host._subagents["conv_c1"].done_at = host._monotonic() - 100.0
    assert host.has_active_subagents() is False
    # ...but the finished child is retained, so the menu still opens.
    assert host.has_any_subagents() is True
    host._open_subagent_menu()
    assert host._subagent_menu_open is True
    menu_text = _formatted_text_plain(host.build_subagent_menu())
    assert "reviewer" in menu_text
    assert "Done" in menu_text


def test_subagent_menu_renders_inline_with_main_and_agents() -> None:
    """An open menu renders the ``main`` row + agent rows + the key hint in
    the inline menu Window (below the toolbar, not a fullscreen overlay)."""
    host = TerminalHost(model_name="test")
    _seed_busy_tree(host, 2)
    host._open_subagent_menu()
    menu_text = _formatted_text_plain(host.build_subagent_menu())
    assert "main" in menu_text
    assert "agent0" in menu_text and "agent1" in menu_text
    assert "select" in menu_text and "open" in menu_text  # the key hint
    # The selection marker sits on the first row (index 0 == "main").
    assert "▸" in menu_text
    # The menu does NOT render in the prompt (which sits above the input);
    # it lives in its own Window below the toolbar.
    assert "agent0" not in _formatted_text_plain(host.build_prompt())


def test_subagent_menu_navigation_moves_selection() -> None:
    """Up/Down move the selection across rows (main + agents), wrapping."""
    host = TerminalHost(model_name="test")
    _seed_busy_tree(host, 2)
    host._open_subagent_menu()
    # Rows: [main, conv_c0, conv_c1]; index starts at 0 (main).
    assert host._selected_subagent_id() == "conv_main"
    host._move_subagent_selection(1)
    assert host._selected_subagent_id() == "conv_c0"
    host._move_subagent_selection(1)
    assert host._selected_subagent_id() == "conv_c1"
    host._move_subagent_selection(1)  # wraps back to main
    assert host._selected_subagent_id() == "conv_main"
    host._move_subagent_selection(-1)  # wraps to last
    assert host._selected_subagent_id() == "conv_c1"


def test_subagent_menu_caps_visible_rows_at_five() -> None:
    """Long trees scroll within a 5-row window so the input bar can't lift
    more than that many lines up the terminal."""
    from omnigent_ui_sdk.terminal._host import _SUBAGENT_MENU_MAX_ROWS

    host = TerminalHost(model_name="test")
    _seed_busy_tree(host, 10)  # 10 agents + the "main" row = 11 rows
    host._open_subagent_menu()
    frags = host._render_subagent_menu_fragments()
    assert len(frags) == _SUBAGENT_MENU_MAX_ROWS == 5
    # Selecting far down scrolls the window so the selection stays visible.
    for _ in range(8):
        host._move_subagent_selection(1)
    frags = host._render_subagent_menu_fragments()
    assert len(frags) == 5
    selected_id = host._selected_subagent_id()
    assert selected_id is not None
    # The selected row is within the rendered window.
    rendered = "".join(text for _style, text in frags)
    label = dict(host._subagent_menu_display_rows())[selected_id]
    assert label in rendered


def test_subagent_menu_closed_renders_nothing() -> None:
    """A closed menu contributes no rows and zero height."""
    host = TerminalHost(model_name="test")
    _seed_busy_tree(host, 2)
    assert host._render_subagent_menu_fragments() == []
    assert list(host.build_subagent_menu()) == []
    assert host._subagent_menu_line_count() == 0


def test_subagent_menu_auto_closes_when_tree_empties() -> None:
    """If the tree drains while the menu is open, it auto-closes on render."""
    host = TerminalHost(model_name="test")
    _seed_busy_tree(host, 1)
    host._open_subagent_menu()
    assert host._subagent_menu_open is True
    host.clear_subagents()
    assert host._render_subagent_menu_fragments() == []
    assert host._subagent_menu_open is False


def test_subagent_menu_enter_selects_then_closes() -> None:
    """Selecting a row (the id ``run`` would switch to) and closing works."""
    host = TerminalHost(model_name="test")
    _seed_busy_tree(host, 2)
    host._open_subagent_menu()
    host._move_subagent_selection(1)
    chosen = host._selected_subagent_id()
    assert chosen == "conv_c0"
    host._close_subagent_menu()
    assert host._subagent_menu_open is False


# ── Left-arrow: back to the top-level session ──────────────────────


def test_is_inside_subagent_tracks_active_vs_root() -> None:
    """``_is_inside_subagent`` is True only when the active session differs
    from the tree root (and both are known)."""
    host = TerminalHost(model_name="test")
    # No getter / no root yet.
    assert host._is_inside_subagent() is False

    _seed_busy_tree(host, 1)  # sets _subagent_root = "conv_main"
    active = {"id": "conv_main"}
    host.active_session_id_getter = lambda: active["id"]
    # Active == root -> on the top-level session, not "inside".
    assert host._is_inside_subagent() is False
    # Dived into the sub-agent -> inside.
    active["id"] = "conv_c0"
    assert host._is_inside_subagent() is True


def test_back_hint_shows_only_inside_subagent() -> None:
    """The ``← back`` toolbar hint appears only while inside a sub-agent."""
    host = TerminalHost(model_name="test")
    _seed_busy_tree(host, 1)
    active = {"id": "conv_main"}
    host.active_session_id_getter = lambda: active["id"]

    assert "← back" not in _formatted_text_plain(host.build_toolbar())
    active["id"] = "conv_c0"
    assert "← back" in _formatted_text_plain(host.build_toolbar())


def test_subagent_count_debounces_busy_and_terminal_blips() -> None:
    """The running count stays steady through the status oscillation that
    used to flicker the badge — both ``busy`` dropping between turns and a
    brief terminal blip mid-run."""
    host = TerminalHost(model_name="test")
    host.upsert_subagent("conv_c1", parent_id="conv_main", child=_busy())
    assert host.active_subagent_count() == 1

    # busy drops between turns (status still non-terminal) -> still counts.
    host.upsert_subagent("conv_c1", child={"busy": False, "current_task_status": "in_progress"})
    assert host.active_subagent_count() == 1

    # a terminal blip (idle->completed between turns) then a resume -> the
    # debounce keeps it counted across the flip; the badge never flickers off.
    host.upsert_subagent("conv_c1", child=_done())
    assert host.active_subagent_count() == 1
    host.upsert_subagent("conv_c1", child=_busy())
    assert host.active_subagent_count() == 1

    # Truly finished + linger elapsed -> it finally settles out.
    host.upsert_subagent("conv_c1", child=_done())
    host._subagents["conv_c1"].done_at = host._monotonic() - 100.0
    assert host.active_subagent_count() == 0
    assert "state: sleeping" in _formatted_text_plain(host.build_toolbar())


def test_subagent_poll_null_status_does_not_resurrect_terminal() -> None:
    """The REST poll reports ``current_task_status=None``; re-seeding a node
    the SSE already marked terminal must NOT clear ``done_at`` and resurrect
    it (which would stick the badge on 'N agents running')."""
    host = TerminalHost(model_name="test")
    host.upsert_subagent(
        "conv_c1",
        parent_id="conv_main",
        child={"tool": "a", "busy": False, "current_task_status": "completed"},
    )
    done_at = host._subagents["conv_c1"].done_at
    assert done_at is not None
    # A 2 s poll row: null status (+ busy False). Must not clear done_at.
    host.upsert_subagent(
        "conv_c1",
        parent_id="conv_main",
        child={"id": "conv_c1", "busy": False, "current_task_status": None},
    )
    assert host._subagents["conv_c1"].done_at == done_at  # unchanged → not resurrected


def test_subagent_poll_only_node_busy_false_is_warm_idle_not_done() -> None:
    """A poll-only node (no SSE terminal status) whose ``busy`` flag drops to
    False is treated as WARM/IDLE, NOT terminal — mirroring the web, which
    shows ``Idle`` (not ``Done``) for a child whose loop is quiet with no
    terminal task status. It drops out of the running count but is NOT stamped
    terminal (``done_at`` stays None) and stays retained + chattable."""
    host = TerminalHost(model_name="test")
    host.upsert_subagent(
        "conv_g1",
        parent_id="conv_child",
        child={"id": "conv_g1", "tool": "x", "busy": True, "current_task_status": None},
    )
    assert host.active_subagent_count() == 1  # running while busy
    node = host._subagents["conv_g1"]
    assert host._subagent_status_label(node) == "Working"

    # busy drops with NO terminal status -> warm/idle, not Done.
    host.upsert_subagent(
        "conv_g1", child={"id": "conv_g1", "busy": False, "current_task_status": None}
    )
    assert host.active_subagent_count() == 0  # not running
    assert node.done_at is None  # NOT treated as terminal (web parity: B)
    assert host._subagent_status_label(node) == "Idle"  # warm-idle, NOT "Done"
    # Retained + chattable (its session is open).
    assert {n.session_id for n, _ in host.subagent_tree()} == {"conv_g1"}
    assert host.is_subagent_chattable("conv_g1") is True


def test_subagent_finished_node_kept_visible_in_selector() -> None:
    """A finished-past-linger node drops out of the running BADGE but stays
    VISIBLE in the ↓ selector indefinitely (web parity) — so the user can dive
    back in and chat with it. (Retention: it leaves only when the server stops
    listing it or the tree is cleared.)"""
    host = TerminalHost(model_name="test")
    host.upsert_subagent(
        "conv_c1",
        parent_id="conv_main",
        child={"tool": "a", "busy": False, "current_task_status": "completed"},
    )
    host._subagents["conv_c1"].done_at = host._monotonic() - 100.0
    assert host.active_subagent_count() == 0  # dropped from the running badge
    # Visible in the selector tree (NOT hidden) and kept in the registry.
    assert [n.session_id for n, _ in host.subagent_tree()] == ["conv_c1"]
    assert "conv_c1" in host._subagents
    node = host._subagents["conv_c1"]
    assert host._subagent_visible(node, host._monotonic()) is True
    assert host._subagent_status_label(node) == "Done"

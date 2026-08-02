"""Tests for the turn-end elapsed-time footer.

The REPL starts a per-turn timer at turn start (``format_response_start`` +
the live status-bar timer) but its event loop never called the matching
``format_response_end`` — so the persistent ``  Xs`` footer that
:class:`TimedFormatter` renders was silently dropped for every turn (the
"no response time printed" report). ``_emit_turn_elapsed_footer`` closes that
gap.

The footer is shown only once a turn is slow enough to be worth a number
(default 5s, tunable via ``OMNIGENT_ELAPSED_FOOTER_MIN_S``); quick turns stay
uncluttered. These tests pin that contract without driving the full
``run_repl`` closure.
"""

from __future__ import annotations

import re
import time

import pytest
from omnigent_client import BlockContext, ResponseStartBlock

from omnigent.repl._repl import (
    _ELAPSED_FOOTER_MIN_ENV,
    _ELAPSED_FOOTER_MIN_S,
    TimedFormatter,
    _emit_turn_elapsed_footer,
    _format_elapsed,
    _resolve_elapsed_footer_min_s,
)

# An elapsed footer looks like ``   12.3s`` or ``   213s`` — digits, optional
# decimal, then ``s``. Pinned as a regex so the test is robust to the muted
# styling.
_ELAPSED = re.compile(r"\d+(?:\.\d+)?s")


class _RecordingHost:
    """Minimal terminal host that records rendered items.

    Concrete (not a mock) so any unexpected attribute access fails loudly;
    only ``output`` — the surface the footer helper touches — is implemented.
    """

    def __init__(self) -> None:
        self.outputs: list[object] = []

    def output(self, renderable: object, *, soft_wrap: bool = False) -> None:
        """Record a rendered item.

        :param renderable: Any Rich-renderable object.
        :param soft_wrap: Ignored — matches the real host signature.
        """
        self.outputs.append(renderable)


def _rendered_text(host: _RecordingHost) -> str:
    """Best-effort plain text of everything the host rendered."""
    return "".join(getattr(item, "plain", str(item)) for item in host.outputs)


def _started_formatter(elapsed_s: float) -> TimedFormatter:
    """A :class:`TimedFormatter` whose captured start is ``elapsed_s`` ago.

    The footer helper builds its own end block stamped at ``time.monotonic()``,
    so backdating ``_start_time`` makes the turn read as ``elapsed_s`` long.

    :param elapsed_s: Simulated turn duration in seconds.
    :returns: A formatter primed as if a turn started ``elapsed_s`` ago.
    """
    fmt = TimedFormatter()
    fmt.format_response_start(
        ResponseStartBlock(
            model="polly",
            response_id="",
            ctx=BlockContext(
                agent="polly",
                depth=0,
                turn=0,
                timestamp=time.monotonic() - elapsed_s,
            ),
        )
    )
    return fmt


def test_emits_elapsed_footer_after_a_slow_turn() -> None:
    """A turn past the threshold emits a muted ``Xs`` elapsed footer.

    Regression target: the REPL never calls ``format_response_end``, so the
    elapsed footer never reaches the host — the user sees no response time.
    """
    fmt = _started_formatter(elapsed_s=30.0)  # well past the 5s default

    host = _RecordingHost()
    _emit_turn_elapsed_footer(fmt, host, agent_name="polly")

    assert _ELAPSED.search(_rendered_text(host)), (
        f"expected an elapsed-time footer, got: {_rendered_text(host)!r}"
    )


def test_no_footer_for_fast_turn_below_threshold() -> None:
    """A quick turn (under the threshold) prints no footer — no ``0.1s`` noise."""
    fmt = _started_formatter(elapsed_s=0.0)  # effectively instant

    host = _RecordingHost()
    _emit_turn_elapsed_footer(fmt, host, agent_name="polly")

    assert not _ELAPSED.search(_rendered_text(host)), (
        f"fast turn should print no footer, got: {_rendered_text(host)!r}"
    )


def test_threshold_env_override_shows_fast_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """``OMNIGENT_ELAPSED_FOOTER_MIN_S=0`` restores always-on footer behavior."""
    monkeypatch.setenv(_ELAPSED_FOOTER_MIN_ENV, "0")
    fmt = _started_formatter(elapsed_s=0.2)

    host = _RecordingHost()
    _emit_turn_elapsed_footer(fmt, host, agent_name="polly")

    assert _ELAPSED.search(_rendered_text(host)), (
        f"threshold 0 should print every turn, got: {_rendered_text(host)!r}"
    )


def test_no_footer_when_turn_never_started() -> None:
    """No start time (e.g. a setup-phase failure before the LLM stream) → no
    footer. ``TimedFormatter`` guards on a captured start, so the helper is a
    safe no-op rather than printing a bogus ``Xs``.
    """
    fmt = TimedFormatter()  # no format_response_start call
    host = _RecordingHost()
    _emit_turn_elapsed_footer(fmt, host, agent_name="polly")

    assert not _ELAPSED.search(_rendered_text(host)), (
        f"unexpected footer with no started turn: {_rendered_text(host)!r}"
    )


# ── Formatting + threshold resolution ────────────────


def test_format_elapsed_sub_minute_keeps_decimal() -> None:
    """Below a minute, one decimal of precision (Nielsen's sub-second scale)."""
    assert _format_elapsed(4.24) == "4.2s"
    assert _format_elapsed(13.75) == "13.8s"
    assert _format_elapsed(59.8) == "59.8s"


def test_format_elapsed_minutes_and_seconds() -> None:
    """1min–1hr shows minutes + integer seconds, unpadded (Go/kubectl style)."""
    assert _format_elapsed(60.0) == "1m0s"
    assert _format_elapsed(63.4) == "1m3s"
    assert _format_elapsed(326.0) == "5m26s"  # the old unreadable "326s"
    assert _format_elapsed(3599.0) == "59m59s"


def test_format_elapsed_hours_and_minutes() -> None:
    """At/above an hour, hours + minutes only — seconds dropped at this scale."""
    assert _format_elapsed(3600.0) == "1h0m"
    assert _format_elapsed(3900.0) == "1h5m"
    assert _format_elapsed(9420.0) == "2h37m"


def test_resolve_threshold_defaults_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_ELAPSED_FOOTER_MIN_ENV, raising=False)
    assert _resolve_elapsed_footer_min_s() == _ELAPSED_FOOTER_MIN_S


def test_resolve_threshold_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_ELAPSED_FOOTER_MIN_ENV, "10")
    assert _resolve_elapsed_footer_min_s() == 10.0


@pytest.mark.parametrize("bad", ["abc", "-1", ""])
def test_resolve_threshold_falls_back_on_bad_value(
    monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    """A non-numeric, negative, or empty value falls back to the default rather
    than silently disabling the footer."""
    monkeypatch.setenv(_ELAPSED_FOOTER_MIN_ENV, bad)
    assert _resolve_elapsed_footer_min_s() == _ELAPSED_FOOTER_MIN_S

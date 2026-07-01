"""Tests for the turn-end elapsed-time footer.

The REPL starts a per-turn timer at turn start (``format_response_start`` +
the live status-bar timer) but its event loop never called the matching
``format_response_end`` — so the persistent ``  Xs`` footer that
:class:`TimedFormatter` renders was silently dropped for every turn (the
"no response time printed" report). ``_emit_turn_elapsed_footer`` closes that
gap; these tests pin its contract without driving the full ``run_repl``
closure.
"""

from __future__ import annotations

import re
import time

from omnigent_client import BlockContext, ResponseStartBlock

from omnigent.repl._repl import TimedFormatter, _emit_turn_elapsed_footer

# An elapsed footer looks like ``   12.3s`` — a number with one decimal,
# then ``s``. Pinned as a regex so the test is robust to the muted styling.
_ELAPSED = re.compile(r"\d+\.\d+s")


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


def test_emits_elapsed_footer_after_a_started_turn() -> None:
    """After a turn start, the helper emits a muted ``Xs`` elapsed footer.

    Regression target: the REPL never calls ``format_response_end``, so the
    elapsed footer never reaches the host — the user sees no response time.
    """
    fmt = TimedFormatter()
    # Capture a start time the way the REPL does on the "running" status.
    fmt.format_response_start(
        ResponseStartBlock(
            model="polly",
            response_id="",
            ctx=BlockContext(agent="polly", depth=0, turn=0, timestamp=time.monotonic()),
        )
    )

    host = _RecordingHost()
    _emit_turn_elapsed_footer(fmt, host, agent_name="polly")

    assert _ELAPSED.search(_rendered_text(host)), (
        f"expected an elapsed-time footer, got: {_rendered_text(host)!r}"
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

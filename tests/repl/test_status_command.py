"""Unit tests for the ``/status`` slash command's pure helpers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from omnigent.repl._repl import (
    _STATUS_CONTEXT_BAR_W,
    _STATUS_GREEN,
    _STATUS_RED,
    _STATUS_YELLOW,
    _build_status_overview,
    _format_uptime,
    _status_connection_health,
    _status_context_bar,
    _status_runner_line,
)

# ---------------------------------------------------------------------------
# _format_uptime
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0.4, "<1s"),
        (0, "<1s"),
        (1, "1s"),
        (45, "45s"),
        (60, "1m"),
        (95, "1m 35s"),
        (3600, "1h"),
        (3660, "1h 1m"),
        (7325, "2h 2m"),
    ],
)
def test_format_uptime(seconds: float, expected: str) -> None:
    assert _format_uptime(seconds) == expected


# ---------------------------------------------------------------------------
# _status_connection_health
# ---------------------------------------------------------------------------


def test_connection_health_disconnected_when_recovery_error() -> None:
    """A recorded recovery error pins the state red, even with a runner bound."""
    session = SimpleNamespace(
        _last_runner_recovery_error_key=("OmnigentError", "", "500", "boom"),
        _bound_runner_id="runner_1",
    )
    assert _status_connection_health(session) == (_STATUS_RED, "Disconnected")


def test_connection_health_connected_when_bound_and_driving() -> None:
    session = SimpleNamespace(_bound_runner_id="runner_1")
    assert _status_connection_health(session) == (_STATUS_GREEN, "Connected")


def test_connection_health_observing_when_readonly() -> None:
    session = SimpleNamespace(_bound_runner_id="runner_1", _readonly_view=True)
    assert _status_connection_health(session) == (_STATUS_YELLOW, "Observing")


def test_connection_health_observing_when_interactive_child() -> None:
    session = SimpleNamespace(_bound_runner_id="runner_1", _interactive_child=True)
    assert _status_connection_health(session) == (_STATUS_YELLOW, "Observing")


def test_connection_health_observing_when_attach_only() -> None:
    """An --attach session is observing, not driving, even with a runner bound.

    Regression guard: attach hydrates ``_bound_runner_id`` but leaves
    ``_readonly_view`` false, so a health check that only inspects
    readonly/interactive would wrongly report green "Connected" next to the
    Runner row's "· attached".
    """
    session = SimpleNamespace(_bound_runner_id="runner_1", _attach_only=True)
    assert _status_connection_health(session) == (_STATUS_YELLOW, "Observing")


def test_connection_health_connecting_when_nothing_bound() -> None:
    session = SimpleNamespace()
    assert _status_connection_health(session) == (_STATUS_YELLOW, "Connecting…")


def test_connection_health_reads_real_adapter_attributes() -> None:
    """The private attrs the health check reads must exist on the real adapter.

    ``_status_connection_health`` uses ``getattr(..., default)`` over
    adapter-private names, so a rename on ``_SessionsChatReplAdapter`` would
    silently degrade to the default (dot stuck yellow) with every
    SimpleNamespace-stubbed test still green. Pin the contract to the class.
    """
    import inspect

    from omnigent.repl._repl import _SessionsChatReplAdapter

    src = inspect.getsource(_SessionsChatReplAdapter.__init__)
    for attr in (
        "_bound_runner_id",
        "_last_runner_recovery_error_key",
        "_readonly_view",
        "_interactive_child",
        "_attach_only",
    ):
        assert f"self.{attr}" in src, f"{attr} missing from adapter __init__"


# ---------------------------------------------------------------------------
# _status_context_bar
# ---------------------------------------------------------------------------


def _bar_fill(used: int, window: int) -> tuple[str, str]:
    """Return the (used-run, free-run) glyph segments of the rendered bar."""
    markup = _status_context_bar(used, window, "magenta", "grey50")
    used_run = markup.count("█")
    free_run = markup.count("░")
    return used_run, free_run


def test_context_bar_width_is_constant() -> None:
    """Used + free glyphs always sum to the bar width, regardless of fill."""
    for used in (0, 50_000, 150_000, 200_000):
        used_run, free_run = _bar_fill(used, 200_000)
        assert used_run + free_run == _STATUS_CONTEXT_BAR_W


def test_context_bar_color_healthy() -> None:
    """Below 70% the used segment renders in the accent color."""
    markup = _status_context_bar(50_000, 200_000, "magenta", "grey50")
    assert "[magenta]" in markup
    assert f"[{_STATUS_YELLOW}]" not in markup
    assert f"[{_STATUS_RED}]" not in markup


def test_context_bar_color_yellow_at_pressure() -> None:
    """At/above 70% the used segment escalates to yellow."""
    markup = _status_context_bar(150_000, 200_000, "magenta", "grey50")
    assert f"[{_STATUS_YELLOW}]" in markup


def test_context_bar_color_red_when_nearly_full() -> None:
    """At/above 90% the used segment escalates to red."""
    markup = _status_context_bar(185_000, 200_000, "magenta", "grey50")
    assert f"[{_STATUS_RED}]" in markup


def test_context_bar_tier_boundaries_are_inclusive() -> None:
    """Exactly 70% is yellow and exactly 90% is red (the ``>=`` boundaries).

    Pins the boundary so a ``>=`` → ``>`` regression is caught; the other
    tier tests sit comfortably inside each band and would not notice.
    """
    at_70 = _status_context_bar(140_000, 200_000, "magenta", "grey50")
    assert f"[{_STATUS_YELLOW}]" in at_70 and f"[{_STATUS_RED}]" not in at_70
    at_90 = _status_context_bar(180_000, 200_000, "magenta", "grey50")
    assert f"[{_STATUS_RED}]" in at_90


def test_context_bar_color_wraps_the_used_run_not_the_free_run() -> None:
    """The tier color must wrap the used (█) glyphs, not the free (░) ones.

    Guards against a used/free segment swap that a glyph *count* check
    misses: the markup order must be ``[color]███…[/color][muted]░░░…``.
    """
    markup = _status_context_bar(50_000, 200_000, "magenta", "grey50")
    assert "[magenta]█" in markup
    assert "[grey50]░" in markup


def test_context_bar_clamps_when_over_window() -> None:
    """Usage past the window fills the whole bar without overflowing it."""
    used_run, free_run = _bar_fill(250_000, 200_000)
    assert used_run == _STATUS_CONTEXT_BAR_W
    assert free_run == 0


def test_context_bar_caption_reports_context_and_free() -> None:
    """Caption names the load as context and shows free tokens in thousands."""
    markup = _status_context_bar(150_000, 200_000, "magenta", "grey50")
    assert "75% context" in markup
    assert "50k free" in markup


# ---------------------------------------------------------------------------
# _status_runner_line
# ---------------------------------------------------------------------------


def test_runner_line_none_bound() -> None:
    assert _status_runner_line(SimpleNamespace()) == "(none bound) · driving"


def test_runner_line_driving() -> None:
    assert _status_runner_line(SimpleNamespace(_bound_runner_id="r1")) == "r1 · driving"


def test_runner_line_attached() -> None:
    session = SimpleNamespace(_bound_runner_id="r1", _attach_only=True)
    assert _status_runner_line(session) == "r1 · attached"


def test_runner_line_readonly_beats_attached() -> None:
    session = SimpleNamespace(_bound_runner_id="r1", _readonly_view=True, _attach_only=True)
    assert _status_runner_line(session) == "r1 · read-only view"


# ---------------------------------------------------------------------------
# _build_status_overview (smoke)
# ---------------------------------------------------------------------------


def test_build_status_overview_renders_without_raising() -> None:
    """The full assembler returns a Panel on a realistic stub without raising.

    Catches import errors, attribute typos, and markup breakage that the
    leaf-helper tests never exercise. Uses pre-resolved credential_config /
    cli_version, mirroring how run_repl passes them in.
    """
    from omnigent_ui_sdk import RichBlockFormatter
    from rich.panel import Panel

    session = SimpleNamespace(
        session_id="conv_abc",
        model="claude-agent",
        harness="claude-sdk",
        model_override=None,
        llm_model="claude-sonnet-5",
        reasoning_effort=None,
        context_window=200_000,
        _bound_runner_id="runner_1",
    )
    host = SimpleNamespace(tokens_used=50_000)
    client = SimpleNamespace(_base_url="http://127.0.0.1:6767")

    panel = _build_status_overview(
        session=session,
        client=client,
        host=host,
        fmt=RichBlockFormatter(),
        server_version="1.2.3",
        started_at=0.0,
        credential_config={},
        cli_version="1.2.3",
    )
    assert isinstance(panel, Panel)


def test_build_status_overview_survives_missing_fields() -> None:
    """A bare session (no ids, no window, None configs) still renders a Panel."""
    from omnigent_ui_sdk import RichBlockFormatter
    from rich.panel import Panel

    session = SimpleNamespace(model="agent")
    host = SimpleNamespace(tokens_used=None)
    client = SimpleNamespace(_base_url=None)

    panel = _build_status_overview(
        session=session,
        client=client,
        host=host,
        fmt=RichBlockFormatter(),
        server_version=None,
        started_at=0.0,
        credential_config=None,
        cli_version=None,
    )
    assert isinstance(panel, Panel)

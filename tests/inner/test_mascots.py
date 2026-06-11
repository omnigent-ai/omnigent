"""Tests for static terminal mascots."""

from __future__ import annotations

from omnigent.inner.mascots import (
    MASCOT_ART_COL_WIDTH,
    MASCOT_ART_COLOR,
    MASCOT_ART_LINES,
    mascot_payload_for_identity,
    random_mascot_color,
    random_mascot_lines,
)


def test_random_mascot_lines_returns_compact_octopus() -> None:
    """The TUI mascot uses the compact two-line octopus art."""

    lines = random_mascot_lines()

    assert lines == [
        "( ◉ ‿ ◉ )",
        " ~)|)|(~ ",
    ]
    assert MASCOT_ART_COL_WIDTH == 9
    assert all(len(line) == MASCOT_ART_COL_WIDTH for line in lines)


def test_mascot_art_color_matches_panel_border_token() -> None:
    """The mascot color matches the startup panel accent."""

    assert random_mascot_color() == MASCOT_ART_COLOR
    assert MASCOT_ART_COLOR == "#F43BA6"


def test_mascot_payload_for_identity_stable() -> None:
    """The identity payload is deterministic for the same seed."""

    seed = "demo\x00tool_a,tool_b\x00You are a helper."
    assert mascot_payload_for_identity(seed) == mascot_payload_for_identity(seed)


def test_mascot_payload_color_is_six_digit_hex_for_many_identities() -> None:
    """The identity payload color stays valid across many seeds."""

    for index in range(256):
        payload = mascot_payload_for_identity(f"agent-{index}")
        assert payload["color"].startswith("#")
        assert len(payload["color"]) == 7
        int(payload["color"][1:], 16)


def test_mascot_payload_for_identity_shape() -> None:
    """The identity payload carries static art and a valid hex color."""

    payload = mascot_payload_for_identity("unique-seed-for-shape-test")

    assert payload["lines"] == list(MASCOT_ART_LINES)
    assert payload["color"].startswith("#")
    assert len(payload["color"]) == 7
    int(payload["color"][1:], 16)

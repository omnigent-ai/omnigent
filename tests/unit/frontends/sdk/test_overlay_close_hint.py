"""
Unit tests for ``_build_close_hint`` and ``_abbreviate_key`` in
``omnigent_ui_sdk.terminal._host``.

Covers the close-hint rendering bug from Kasey's bug report:
auto-generated footer mixed long names (``escape``), single
letters (``q``), and prompt-toolkit shorthand (``c-i``)
arbitrarily, with no override hook.

The fix added (a) a key-name normalizer for consistency,
(b) a ``close_hint`` field on :class:`Overlay` for full
customization, and (c) replaced the hardcoded ``"esc"`` literal
in the rendering site (which assumed the first close_key was
``"escape"`` regardless of what the caller passed).
"""

from __future__ import annotations

from typing import Any

import pytest
from omnigent_ui_sdk.terminal._host import (
    Overlay,
    _abbreviate_key,
    _build_close_hint,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("escape", "esc"),
        ("Escape", "esc"),  # case-insensitive
        ("ESCAPE", "esc"),
        ("enter", "↵"),
        ("tab", "tab"),
        ("Tab", "tab"),
        ("q", "q"),
        ("c-i", "c-i"),
        ("c-o", "c-o"),
        ("c-c", "c-c"),  # no special abbreviation; stays as-is
        ("/", "/"),
    ],
)
def test_abbreviate_key_consistent(raw: str, expected: str) -> None:
    """
    ``_abbreviate_key`` produces consistent short forms.

    Without this normalizer, the auto-generated footer mixed
    ``escape``/``q``/``c-i`` arbitrarily — visually inconsistent
    and reportedly confusing per the bug report.
    """
    assert _abbreviate_key(raw) == expected


def _make_overlay(**overrides: Any) -> Overlay:
    """Helper: minimal :class:`Overlay` with supplied overrides."""

    async def _builder(target: Any) -> str:
        return "ignored"

    defaults: dict[str, Any] = {
        "trigger": "c-o",
        "builder": _builder,
    }
    defaults.update(overrides)
    return Overlay(**defaults)


def test_build_close_hint_default_close_keys_render_consistently() -> None:
    """
    Default ``close_keys=("escape", "q")`` + ``trigger="c-o"``
    renders as ``"esc/q/c-o close"`` — every key normalized.

    Pre-fix the rendering was the same shape but the FIRST key
    was hardcoded as the literal string ``"esc"`` regardless of
    what the caller passed (see the next test).
    """
    overlay = _make_overlay()
    assert _build_close_hint(overlay) == "esc/q/c-o close"


def test_build_close_hint_custom_close_keys_use_actual_keys() -> None:
    """
    A caller passing ``close_keys=("c-c", "q")`` gets a hint
    that reflects the actual keys, not a hardcoded ``"esc"``.

    Pre-fix the rendering was
    ``"esc/q/c-o close"`` — wrong, because ``escape`` wasn't
    a registered close key. Operators who set custom close
    bindings would see a hint that lied about what closed the
    overlay.
    """
    overlay = _make_overlay(close_keys=("c-c", "q"))
    hint = _build_close_hint(overlay)
    # The hardcoded "esc" is gone — actual keys come through.
    assert "esc" not in hint
    assert "c-c" in hint
    assert "q" in hint


def test_build_close_hint_explicit_override_wins() -> None:
    """
    When ``close_hint`` is set, the caller's literal string is
    used verbatim — no auto-gen.
    """
    overlay = _make_overlay(close_hint="press q to close")
    assert _build_close_hint(overlay) == "press q to close"


def test_build_close_hint_explicit_empty_string_is_honored() -> None:
    """
    An explicit empty string (``close_hint=""``) suppresses the
    hint entirely. ``None`` and ``""`` are distinct: ``None``
    falls back to auto-gen, ``""`` produces no hint.
    """
    overlay = _make_overlay(close_hint="")
    assert _build_close_hint(overlay) == ""


def test_build_close_hint_includes_trigger() -> None:
    """
    Auto-generated hint must include the trigger key, since the
    host treats the trigger as a close-key by convention. Users
    learn this from the hint or never.
    """
    overlay = _make_overlay(trigger="c-d")
    hint = _build_close_hint(overlay)
    assert "c-d" in hint

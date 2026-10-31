"""Unit tests for project color assignment."""

from __future__ import annotations

from omnigent.project_colors import least_used_project_color
from omnigent.theme_palettes_generated import THEME_PALETTE_IDS


def test_least_used_color_follows_palette_order_and_ignores_non_palette_values() -> None:
    assert least_used_project_color([None, "#ff0000"]) == THEME_PALETTE_IDS[0]
    assert least_used_project_color(THEME_PALETTE_IDS[:2]) == THEME_PALETTE_IDS[2]
    assert (
        least_used_project_color([*THEME_PALETTE_IDS, THEME_PALETTE_IDS[0]])
        == (THEME_PALETTE_IDS[1])
    )

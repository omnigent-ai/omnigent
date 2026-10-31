"""Unit tests for project color auto-assignment."""

from __future__ import annotations

from omnigent.project_colors import pick_distinct_project_color, with_auto_project_color
from omnigent.project_colors_generated import PROJECT_COLOR_ACCENTS

PALETTE_IDS = list(PROJECT_COLOR_ACCENTS)


def test_first_project_gets_the_first_palette_color() -> None:
    assert pick_distinct_project_color([]) == "omni"
    assert pick_distinct_project_color([None, "#ff0000", 3]) == "omni"


def test_every_palette_color_is_used_once_before_any_repeats() -> None:
    used: list[str] = []
    for _ in PALETTE_IDS:
        used.append(pick_distinct_project_color(used))
    assert sorted(used) == sorted(PALETTE_IDS)


def test_prefers_a_far_color_over_a_near_one() -> None:
    # GitHub and Solarized are both blue.
    assert pick_distinct_project_color(["github"]) not in {"github", "solarized"}


def test_repeats_the_least_used_color_once_every_palette_is_taken() -> None:
    all_but_nord_and_omni_twice = [*(c for c in PALETTE_IDS if c != "nord"), "omni"]
    assert pick_distinct_project_color(all_but_nord_and_omni_twice) == "nord"
    assert pick_distinct_project_color([*PALETTE_IDS, "omni"]) != "omni"


def test_assigns_a_color_only_when_neither_emoji_nor_color_is_set() -> None:
    existing = [{"color": "omni"}, {"icon": "🔥"}]
    assigned = with_auto_project_color({"host_id": "h"}, existing)
    assert assigned["host_id"] == "h"
    assert assigned["color"] in PALETTE_IDS
    assert assigned["color"] != "omni"
    assert with_auto_project_color({"icon": "🚀"}, existing) == {"icon": "🚀"}
    assert with_auto_project_color({"color": "nord"}, existing) == {"color": "nord"}


def test_replaces_a_color_that_is_not_a_palette_id() -> None:
    assert with_auto_project_color({"color": "#ff0000"}, [])["color"] == "omni"

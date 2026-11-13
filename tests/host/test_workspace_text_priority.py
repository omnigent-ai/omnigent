"""Which folders a priority text search looks at before the rest."""

from __future__ import annotations

from omnigent.host.workspace_text_priority import (
    MAX_CHANGED_DIRECTORIES,
    ordered_directories,
    weighted_change_directories,
)


def test_changed_folders_come_heaviest_first_by_lines_plus_one_per_file() -> None:
    changes = [("a/x.py", 10), ("b/y.py", 4), ("b/z.py", 4), ("c/w.py", 0)]
    # Eight lines each, but b has two files.
    even = [("a/x.py", 8), ("b/y.py", 4), ("b/z.py", 4)]

    # a weighs 11, b weighs 10, c weighs 1.
    assert weighted_change_directories(changes) == ["a", "b", "c"]
    # a weighs 9, b weighs 10.
    assert weighted_change_directories(even) == ["b", "a"]


def test_ties_go_to_path_order_and_the_root_is_left_out() -> None:
    assert weighted_change_directories([("top.py", 99), ("z/a", 1), ("m/a", 1)]) == ["m", "z"]


def test_a_folder_beneath_a_heavier_one_is_left_out() -> None:
    changes = [("src/a.py", 50), ("src/lib/b.py", 5), ("srcx/c.py", 1)]

    assert weighted_change_directories(changes) == ["src", "srcx"]


def test_at_most_the_cap_of_changed_folders() -> None:
    changes = [(f"d{index:03}/f.py", index) for index in range(100)]

    folders = weighted_change_directories(changes)

    assert len(folders) == MAX_CHANGED_DIRECTORIES
    assert folders[0] == "d099"


def test_ordered_folders_drop_the_root_repeats_and_what_was_searched() -> None:
    assert ordered_directories(["", "a/b", "a/b", "a/b/c", "a", "d"], ["d"]) == ["a/b", "a"]
